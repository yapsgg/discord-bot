import asyncio
import logging
import os
import re
import subprocess
import tempfile

import discord
import imageio_ffmpeg
import yt_dlp
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import ai
import opencode as oc

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yapsgg-bot")

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip("\"'").strip()
PREFIX = os.getenv("PREFIX", "!")
GUILD_ID = (os.getenv("GUILD_ID") or "").strip()
ENABLE_MESSAGE_CONTENT = (
    os.getenv("ENABLE_MESSAGE_CONTENT", "false").lower() == "true"
)
PORT = os.getenv("PORT")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024
COOKIES_FILE = os.getenv("COOKIES_FILE")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

X_LINK_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/]+/status/\d+",
    re.IGNORECASE,
)
IG_LINK_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/"
    r"(?:[A-Za-z0-9_.]+/)?reels?/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


class YapsGGBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = ENABLE_MESSAGE_CONTENT
        super().__init__(command_prefix=PREFIX, intents=intents)

    async def setup_hook(self):
        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Synced commands to guild %s", GUILD_ID)
                return
            except (ValueError, discord.Forbidden, discord.HTTPException) as error:
                log.error(
                    "Guild sync to %s failed (%s). Check GUILD_ID is a server "
                    "the bot has joined, and invite it with the "
                    "applications.commands scope. Falling back to global sync.",
                    GUILD_ID,
                    error,
                )
        await self.tree.sync()
        log.info("Synced global commands")


bot = YapsGGBot()

OC_SESSIONS = {}


class HealthServer:
    def __init__(self, port):
        self.port = int(port)
        self.runner = None

    async def handle(self, request):
        return web.json_response(
            {"status": "ok", "user": str(bot.user) if bot.user else None}
        )

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.handle)
        app.router.add_get("/health", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Health server listening on port %s", self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


health = HealthServer(PORT) if PORT else None


def download_video(url, outdir):
    opts = {
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "format": (
            "bestvideo[height<=720]+bestaudio/"
            "best[height<=720]/bestvideo+bestaudio/best"
        ),
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else None
        if not path or not os.path.exists(path):
            path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        raise RuntimeError("download produced no file")
    return path, info


def compress_video(src, dst, height, duration):
    src_size = os.path.getsize(src)
    video_bits = MAX_UPLOAD_BYTES * 8 * 0.85
    audio_bps = 96_000
    if duration and duration > 0:
        fit = max(150_000, int(video_bits / duration) - audio_bps)
        source = int(src_size * 8 / duration)
        bitrate = max(150_000, min(fit, source))
    else:
        bitrate = 700_000

    args = [FFMPEG, "-y", "-i", src]
    if height and height > 720:
        args += ["-vf", "scale=-2:720"]
    args += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", str(bitrate),
        "-maxrate", str(int(bitrate * 1.5)),
        "-bufsize", str(bitrate * 2),
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        dst,
    ]
    subprocess.run(
        args,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dst


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    log.exception("Command error", exc_info=error)
    await ctx.reply(f"Error: {error}")


@bot.command(name="ping")
async def ping(ctx):
    await ctx.reply(f"pong - {round(bot.latency * 1000)}ms")


@bot.command(name="hello")
async def hello(ctx):
    await ctx.reply(f"Hey {ctx.author.mention}!")


@bot.tree.command(name="ping", description="Check the bot latency")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"pong - {round(bot.latency * 1000)}ms"
    )


async def send_long(send, text):
    text = (text or "(no response)").strip()
    for start in range(0, len(text), 1900):
        await send(text[start : start + 1900])


async def channel_context(channel, limit=12):
    lines = []
    async for message in channel.history(limit=limit):
        if not message.content:
            continue
        who = "YapsGG" if bot.user and message.author == bot.user else message.author.display_name
        lines.append(f"{who}: {message.content}")
    if not lines:
        return []
    lines.reverse()
    return [{"role": "system", "content": "Recent channel messages:\n" + "\n".join(lines)}]


@bot.event
async def on_message(message):
    if message.author.bot or bot.user is None:
        return
    if bot.user not in message.mentions:
        await bot.process_commands(message)
        return

    prompt = message.content
    for mention in message.mentions:
        prompt = prompt.replace(f"<@{mention.id}>", "").replace(
            f"<@!{mention.id}>", ""
        )
    prompt = prompt.strip() or "Hello!"

    session_id = OC_SESSIONS.get(message.channel.id)
    if session_id:
        try:
            async with message.channel.typing():
                answer = await oc.prompt(session_id, prompt)
            await send_long(message.reply, answer)
        except oc.OpenCodeError as error:
            await message.reply(f"OpenCode error: {error}")
        except Exception:
            log.exception("OpenCode mention handler failed")
            await message.reply("Something went wrong talking to OpenCode.")
        return

    try:
        async with message.channel.typing():
            messages = [{"role": "system", "content": ai.SYSTEM_PROMPT}]
            messages += await channel_context(message.channel)
            messages.append({"role": "user", "content": prompt})
            answer = await ai.chat(messages)
        await send_long(message.reply, answer)
    except Exception:
        log.exception("AI mention handler failed")
        await message.reply("Something went wrong while thinking about that.")


@bot.tree.command(name="ask", description="Ask the AI assistant anything")
@app_commands.describe(prompt="Your question or request")
async def ask(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    messages = [
        {"role": "system", "content": ai.SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    answer = await ai.chat(messages)
    await send_long(interaction.followup.send, answer)


@bot.tree.command(
    name="summarize", description="Summarize recent messages in this channel"
)
@app_commands.describe(count="How many recent messages to summarize")
async def summarize(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 5, 200] = 50,
):
    await interaction.response.defer(thinking=True)
    lines = []
    async for message in interaction.channel.history(limit=count):
        if message.content:
            lines.append(f"{message.author.display_name}: {message.content}")
    if not lines:
        await interaction.followup.send("There is nothing to summarize.")
        return
    lines.reverse()
    transcript = "\n".join(lines)[:14000]
    messages = [
        {"role": "system", "content": ai.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Summarize this Discord conversation. Give a one-line "
                "overview then concise key points:\n\n" + transcript
            ),
        },
    ]
    answer = await ai.chat(messages)
    await send_long(interaction.followup.send, answer)


oc_group = app_commands.Group(
    name="oc", description="Control the OpenCode agent on the server"
)


@oc_group.command(
    name="start", description="Start a new OpenCode session for this channel"
)
async def oc_start(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        session_id = await oc.create_session(
            f"discord-{interaction.channel_id}"
        )
    except oc.OpenCodeError as error:
        await interaction.followup.send(f"Could not start OpenCode: {error}")
        return
    OC_SESSIONS[interaction.channel_id] = session_id
    await interaction.followup.send(
        f"OpenCode session started (`{session_id}`) using "
        f"`{oc.PROVIDER_ID}/{oc.MODEL_ID}`.\n"
        "Mention me with instructions, or use `/oc prompt`. "
        "Use `/oc stop` to abort."
    )


@oc_group.command(name="prompt", description="Send a prompt to OpenCode")
@app_commands.describe(text="What you want OpenCode to do")
async def oc_prompt(interaction: discord.Interaction, text: str):
    session_id = OC_SESSIONS.get(interaction.channel_id)
    if not session_id:
        await interaction.response.send_message(
            "No active session here. Run `/oc start` first.", ephemeral=True
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        answer = await oc.prompt(session_id, text)
    except oc.OpenCodeError as error:
        await interaction.followup.send(f"OpenCode error: {error}")
        return
    await send_long(interaction.followup.send, answer)


@oc_group.command(name="stop", description="Abort the OpenCode session")
async def oc_stop(interaction: discord.Interaction):
    session_id = OC_SESSIONS.get(interaction.channel_id)
    if not session_id:
        await interaction.response.send_message(
            "No active session here.", ephemeral=True
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        await oc.abort(session_id)
    except oc.OpenCodeError as error:
        await interaction.followup.send(f"OpenCode error: {error}")
        return
    await interaction.followup.send(
        f"Aborted session `{session_id}`. Use `/oc start` for a new one."
    )


@oc_group.command(name="status", description="Show OpenCode server status")
async def oc_status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    lines = [f"Server: `{oc.BASE_URL}`", f"Model: `{oc.PROVIDER_ID}/{oc.MODEL_ID}`"]
    try:
        info = await oc.health()
        lines.append(f"Health: `{info}`")
    except oc.OpenCodeError as error:
        lines.append(f"Health: **down** ({error})")
    session_id = OC_SESSIONS.get(interaction.channel_id)
    lines.append(f"Session: `{session_id or 'none'}`")
    await interaction.followup.send("\n".join(lines))


@oc_group.command(
    name="connect", description="Set the OpenRouter API key for OpenCode"
)
@app_commands.describe(key="Your OpenRouter API key (sk-or-v1-...)")
async def oc_connect(interaction: discord.Interaction, key: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        await oc.set_api_key(key)
    except oc.OpenCodeError as error:
        await interaction.followup.send(f"Could not set key: {error}")
        return
    await interaction.followup.send("OpenRouter API key saved.")


bot.tree.add_command(oc_group)


async def fetch_and_send(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, info = await asyncio.to_thread(download_video, link, tmp)
            size = os.path.getsize(path)

            if size > MAX_UPLOAD_BYTES:
                out = os.path.join(tmp, "compressed.mp4")
                path = await asyncio.to_thread(
                    compress_video,
                    path,
                    out,
                    info.get("height") or 0,
                    info.get("duration") or 0,
                )
                size = os.path.getsize(path)

            if size > MAX_UPLOAD_BYTES:
                await interaction.followup.send(
                    f"Video is {size / 1e6:.1f} MB, over the "
                    f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB Discord limit."
                )
                return

            await interaction.followup.send(
                file=discord.File(path, filename="video.mp4")
            )
    except Exception as error:
        log.exception("Download failed for %s", link)
        await interaction.followup.send(
            f"Could not download that video: {error}", ephemeral=True
        )


@bot.tree.command(name="x", description="Download a video from X (Twitter)")
@app_commands.describe(link="The link to the X/Twitter post")
async def x(interaction: discord.Interaction, link: str):
    if not X_LINK_RE.match(link.strip()):
        await interaction.response.send_message(
            "Provide a valid x.com or twitter.com post link.", ephemeral=True
        )
        return
    await fetch_and_send(interaction, link)


@bot.tree.command(name="ig", description="Download an Instagram reel")
@app_commands.describe(reels="The link to the Instagram reel")
async def ig(interaction: discord.Interaction, reels: str):
    if not IG_LINK_RE.match(reels.strip()):
        await interaction.response.send_message(
            "Provide a valid instagram.com reel link.", ephemeral=True
        )
        return
    await fetch_and_send(interaction, reels)


def token_looks_valid(value):
    return value.count(".") == 2 and len(value) > 50


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set")
    if not token_looks_valid(TOKEN):
        log.warning(
            "DISCORD_TOKEN does not look like a bot token "
            "(expected ~3 dot-separated parts, %d chars). Check the Render "
            "env var for extra quotes, whitespace, or a Client Secret "
            "instead of the Bot token.",
            len(TOKEN),
        )
    if health:
        await health.start()
    try:
        async with bot:
            await bot.start(TOKEN)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the token (401). On Render, re-copy the Bot "
            "token from the Developer Portal > Bot > Reset Token, paste it "
            "into the DISCORD_TOKEN env var with no quotes/spaces, and "
            "redeploy."
        )
        raise SystemExit(1)
    finally:
        if health:
            await health.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
