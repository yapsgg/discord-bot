import base64
import json
import logging
import os

import aiohttp

log = logging.getLogger("yapsgg-bot.opencode")

BASE_URL = os.getenv("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
PROVIDER_ID = os.getenv("OPENCODE_PROVIDER", "openrouter")
MODEL_ID = os.getenv("OPENCODE_MODEL", "deepseek/deepseek-v4.1-flash")


class OpenCodeError(Exception):
    pass


def _headers():
    headers = {"Content-Type": "application/json"}
    if PASSWORD:
        token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


async def _request(method, path, body=None, timeout=60):
    url = BASE_URL + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                headers=_headers(),
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise OpenCodeError(f"{resp.status}: {text[:300]}")
                return json.loads(text) if text else None
    except aiohttp.ClientError as error:
        raise OpenCodeError(f"server unreachable ({error})") from error


async def health():
    return await _request("GET", "/global/health", timeout=10)


async def create_session(title):
    data = await _request("POST", "/session", {"title": title}, timeout=20)
    return data["id"]


async def abort(session_id):
    return await _request("POST", f"/session/{session_id}/abort", timeout=20)


async def set_api_key(key, provider="openrouter"):
    return await _request(
        "PUT", f"/auth/{provider}", {"type": "api", "key": key}, timeout=20
    )


def _format_parts(parts):
    lines = []
    for part in parts or []:
        kind = part.get("type")
        if kind == "text":
            text = (part.get("text") or "").strip()
            if text:
                lines.append(text)
        elif kind == "tool":
            state = part.get("state") or {}
            name = part.get("tool") or "tool"
            status = state.get("status") or "done"
            lines.append(f"[used {name}: {status}]")
    return "\n".join(lines).strip()


async def prompt(session_id, text, timeout=900):
    body = {
        "model": {"providerID": PROVIDER_ID, "modelID": MODEL_ID},
        "parts": [{"type": "text", "text": text}],
    }
    data = await _request(
        "POST", f"/session/{session_id}/message", body, timeout=timeout
    ) or {}
    info = data.get("info") or {}
    error = info.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise OpenCodeError(str(message))
    return _format_parts(data.get("parts")) or "(no response)"
