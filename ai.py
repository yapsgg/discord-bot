import html
import json
import logging
import os
import re
import ssl

import aiohttp
import certifi

log = logging.getLogger("yapsgg-bot.ai")

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("AI_MODEL", "deepseek/deepseek-v4-flash-0731:free")
API_KEY = os.getenv("OPENROUTER_API_KEY")
SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://github.com/yapsgg/discord-bot")
APP_NAME = os.getenv("OPENROUTER_APP_NAME", "YapsGG Bot")
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "900"))
MAX_TOOL_ROUNDS = int(os.getenv("AI_MAX_TOOL_ROUNDS", "3"))

SYSTEM_PROMPT = (
    "You are YapsGG, a helpful and friendly Discord assistant. "
    "Answer concisely and format for Discord (short paragraphs, sparing "
    "markdown). You can call the web_search and fetch_url tools to look up "
    "current information before answering. If a tool fails or returns "
    "nothing useful, say so briefly instead of making things up."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current or factual information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web page and return its readable text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The absolute URL to fetch.",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

UA = "Mozilla/5.0 (compatible; YapsGGBot/1.0)"
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw):
    raw = SCRIPT_RE.sub(" ", raw)
    raw = TAG_RE.sub(" ", raw)
    text = html.unescape(raw)
    text = WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return NL_RE.sub("\n\n", text).strip()


def new_session():
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT)
    )


async def _wiki_search(session, query):
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": 5,
    }
    try:
        async with session.get(
            "https://en.wikipedia.org/w/api.php",
            params=params,
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception:
        return []
    results = []
    for item in (data.get("query", {}).get("search") or []):
        title = item.get("title", "")
        snippet = html_to_text(item.get("snippet", ""))
        url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        results.append(f"{title}: {snippet} ({url})")
    return results


async def web_search(session, query):
    results = []
    try:
        async with session.get(
            "https://api.duckduckgo.com/",
            params={
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1,
            },
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception:
        data = None

    if data:
        if data.get("Answer"):
            results.append(str(data["Answer"]))
        if data.get("AbstractText"):
            source = data.get("AbstractURL") or ""
            results.append(f"{data['AbstractText']} {source}".strip())
        for topic in (data.get("RelatedTopics") or [])[:5]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(str(topic["Text"]))

    if not results:
        results = await _wiki_search(session, query)

    if not results:
        return "No results found. Try fetch_url with a specific URL."
    return "\n".join(results)[:4000]


async def fetch_url(session, url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with session.get(
            url,
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=25),
            allow_redirects=True,
        ) as resp:
            body = await resp.text(errors="ignore")
    except Exception as error:
        return f"Fetch failed: {error}"
    text = html_to_text(body)
    return text[:6000] if text else "Page had no readable text."


async def run_tool(session, name, arguments):
    if name == "web_search":
        return await web_search(session, str(arguments.get("query", "")))
    if name == "fetch_url":
        return await fetch_url(session, str(arguments.get("url", "")))
    return f"Unknown tool: {name}"


def _headers():
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if SITE_URL:
        headers["HTTP-Referer"] = SITE_URL
    if APP_NAME:
        headers["X-Title"] = APP_NAME
    return headers


async def chat(messages):
    if not API_KEY:
        return "AI is not configured yet (OPENROUTER_API_KEY is missing)."

    async with new_session() as session:
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            allow_tools = round_index < MAX_TOOL_ROUNDS
            payload = {
                "model": MODEL,
                "messages": messages,
                "max_tokens": MAX_TOKENS,
            }
            if allow_tools:
                payload["tools"] = TOOLS
                payload["tool_choice"] = "auto"

            try:
                async with session.post(
                    OPENROUTER_URL,
                    headers=_headers(),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=90),
                ) as resp:
                    data = await resp.json()
            except Exception as error:
                log.warning("OpenRouter request failed: %s", error)
                return f"AI request failed: {error}"

            if resp.status != 200:
                detail = (data.get("error") or {}).get("message", resp.status)
                log.warning("OpenRouter error %s: %s", resp.status, detail)
                return f"AI error: {detail}"

            choice = data["choices"][0]["message"]
            calls = choice.get("tool_calls") or []
            log.info(
                "AI round %s: tools=%s calls=%s finish=%s",
                round_index,
                allow_tools,
                [c.get("function", {}).get("name") for c in calls],
                data["choices"][0].get("finish_reason"),
            )

            if not calls or not allow_tools:
                content = choice.get("content")
                if content:
                    return content
                log.warning("AI returned no content: %s", json.dumps(choice)[:500])
                return "(no response)"

            messages.append(choice)
            for call in calls:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = await run_tool(session, function.get("name"), arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": result[:6000],
                    }
                )

    return "I could not finish that request."
