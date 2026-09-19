import asyncio
import logging
import os

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("saidai-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
GUILD_ID = os.getenv("GUILD_ID")
PORT = os.getenv("PORT")


class SaidaiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=PREFIX, intents=intents)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global commands")


bot = SaidaiBot()


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


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set")
    if health:
        await health.start()
    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        if health:
            await health.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
