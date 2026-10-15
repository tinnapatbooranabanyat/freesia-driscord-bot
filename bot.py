"""Discord bot: LLM chat on @mention + music player with slash commands."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.storage import Storage

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

EXTENSIONS = ("cogs.chat", "cogs.music", "cogs.quiz", "cogs.utility")
NOTIFY_COOLDOWN = 15 * 60  # seconds between owner DMs about the same problem

HELP_SECTIONS = [
    ("🤖 คุยกับ AI", (
        "• `@บอท คำถาม` — ถามได้ทุกห้อง (+ DM) ค้นเว็บเองเมื่อถามเรื่องล่าสุด แนบรูป / ข้อความเสียงได้\n"
        "• reply ข้อความบอท = คุยต่อไม่ต้อง @ (ตอบด้วยข้อความเสียงก็ได้) • reply ข้อความคนอื่นแล้ว @บอท = ให้อธิบาย\n"
        "• `/ask` ถาม • `/imagine` วาดรูป • `/summarize` สรุปแชท • `/reset` ล้างความจำ\n"
        "• คลิกขวาที่ข้อความ → Apps → **แปลภาษา** / **สรุป / อธิบาย** / **ถอดข้อความเสียง**\n"
        "• `/persona` ตั้งบุคลิก AI (แอดมิน)"
    )),
    ("🎵 เพลง", (
        "• `/play` ลิงก์ YouTube / YT Music / Spotify / เพลย์ลิสต์ / Mix หรือชื่อเพลง • `/search` ค้นแล้วเลือกเอง\n"
        "• `@บอท เปิดเพลงเศร้าๆ ให้หน่อย` หรือ `/dj` — AI เลือกเพลงให้\n"
        "• `/pause` `/resume` `/skip` (โหวต) `/previous` `/replay` `/skipto` `/seek 1:30` `/stop` `/leave`\n"
        "• `/queue` `/move` `/remove` `/clear` `/shuffle` `/loop` • `/nowplaying` `/lyrics` `/history`\n"
        "• `/filter` เอฟเฟกต์เสียง • `/autoplay` • `/volume` • `/playlist save|load|list|delete`"
    )),
    ("🗣️ เสียง & เกม", (
        "• `/say` ให้บอทพูด • `/talk` ให้บอทอ่านคำตอบ AI ออกเสียง\n"
        "• `/quiz start` เกมทายเพลง (เลือกหมวด / ใช้เพลย์ลิสต์ตัวเอง) • `/quiz stop`"
    )),
    ("🧰 อื่นๆ", (
        "• `/remind set|list|cancel` ตั้งเตือน • `/welcome` การ์ดต้อนรับสมาชิกใหม่ (แอดมิน) • `/status`\n"
        "-# ยศชื่อ **DJ** / แอดมิน / คนที่อยู่คนเดียวในห้อง: ข้ามเพลงได้ทันที ใช้ /stop /clear /skipto ได้"
    )),
]


class Bot(commands.Bot):
    def __init__(self, members_intent: bool) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read message text / replies (enable in Developer Portal)
        intents.voice_states = True
        intents.members = members_intent  # welcome cards (privileged: "Server Members Intent")
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/help • @mention เพื่อถาม AI"),
        )
        self._web_runner: web.AppRunner | None = None
        self.storage = Storage()
        self.started_at = time.time()
        self._notified: dict[str, float] = {}
        self._owner: discord.User | None = None
        self._add_global_commands()

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        if dev_guild:
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

        await self._start_health_server()

    async def on_message(self, message: discord.Message) -> None:
        # No prefix commands: the cogs' listeners handle messages. (The default implementation tried to parse
        # "@bot hello" as a command named "hello" and logged CommandNotFound errors.)
        return

    def _add_global_commands(self) -> None:
        @self.tree.command(name="help", description="ดูวิธีใช้บอท")
        async def help_cmd(interaction: discord.Interaction) -> None:
            embed = discord.Embed(title="วิธีใช้บอท", color=discord.Color.blurple())
            for name, value in HELP_SECTIONS:
                embed.add_field(name=name, value=value, inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            log.exception("Slash command error", exc_info=error)
            command = interaction.command.qualified_name if interaction.command else "?"
            await self.notify_owner(f"command:{command}", f"💥 คำสั่ง `/{command}` error: `{str(error)[:300]}`")
            msg = "❌ เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass

    async def notify_owner(self, key: str, text: str) -> None:
        """DM the bot owner about a problem (at most once per NOTIFY_COOLDOWN for the same key)."""
        now = time.time()
        if now - self._notified.get(key, 0) < NOTIFY_COOLDOWN:
            return
        self._notified[key] = now
        try:
            if self._owner is None:
                owner_id = os.getenv("OWNER_ID", "").strip()
                if owner_id:
                    self._owner = await self.fetch_user(int(owner_id))
                else:
                    app = await self.application_info()
                    self._owner = app.team.owner if app.team and app.team.owner else app.owner
            await self._owner.send(f"🤖 **แจ้งเตือนจากบอท**\n{text}"[:1900])
        except Exception as e:
            log.warning("Could not notify owner: %r", e)

    async def _start_health_server(self) -> None:
        """Some free hosts (Render, Koyeb, ...) require an HTTP port; answer health checks there."""
        port = os.getenv("PORT", "").strip()
        if not port:
            return

        async def health(_: web.Request) -> web.Response:
            return web.Response(text="OK")

        app = web.Application()
        app.router.add_get("/", health)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        await web.TCPSite(self._web_runner, "0.0.0.0", int(port)).start()
        log.info("Health check server listening on :%s", port)

    async def close(self) -> None:
        if self._web_runner:
            await self._web_runner.cleanup()
        await super().close()
        self.storage.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s) in %d servers", self.user, self.user.id, len(self.guilds))


async def run(token: str) -> None:
    want_members = os.getenv("MEMBERS_INTENT", "true").strip().lower() not in ("0", "false", "no", "off")
    for members in ((True, False) if want_members else (False,)):
        bot = Bot(members_intent=members)
        try:
            async with bot:
                await bot.start(token)
            return
        except discord.PrivilegedIntentsRequired:
            log.warning("Server Members Intent is not enabled in the Developer Portal: "
                        "starting without it (welcome cards for new members are disabled)")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("ยังไม่ได้ตั้งค่า DISCORD_TOKEN ในไฟล์ .env")
    try:
        asyncio.run(run(token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
