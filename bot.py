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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yapsgg-bot")

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip("\"'").strip()
PREFIX = os.getenv("PREFIX", "!")
GUILD_ID = os.getenv("GUILD_ID")
PORT = os.getenv("PORT")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

X_LINK_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/]+/status/\d+",
    re.IGNORECASE,
)


class YapsGGBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
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


@bot.tree.command(name="x", description="Download a video from X (Twitter)")
@app_commands.describe(link="The link to the X/Twitter post")
async def x(interaction: discord.Interaction, link: str):
    if not X_LINK_RE.match(link.strip()):
        await interaction.response.send_message(
            "Provide a valid x.com or twitter.com post link.", ephemeral=True
        )
        return

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
        log.exception("X download failed")
        await interaction.followup.send(
            f"Could not download that video: {error}", ephemeral=True
        )


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
