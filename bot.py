"""Discord bot: LLM chat on @mention + music player with slash commands."""
from __future__ import annotations

import asyncio
import logging
import os

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.storage import Storage

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HELP_TEXT = """**🤖 คุยกับ AI**
• `@บอท คำถาม` — ถามได้ทุกห้อง (+ DM) AI ค้นเว็บเองเมื่อถามเรื่องล่าสุด แนบรูปให้ดูได้
• reply ข้อความของบอท = คุยต่อได้เลยไม่ต้อง @ • reply ข้อความคนอื่นแล้ว @บอท = ให้อธิบายข้อความนั้น
• `/ask` ถาม (แนบรูปได้) • `/summarize` สรุปแชทล่าสุด • `/reset` ล้างความจำในห้องนี้
• คลิกขวาที่ข้อความ → Apps → **แปลภาษา** / **สรุป / อธิบาย**
• `/persona` ตั้งบุคลิก AI ของเซิร์ฟเวอร์ (แอดมิน)

**🎵 เพลง** (ลิงก์ YouTube / YT Music / Spotify / เพลย์ลิสต์ / Mix หรือพิมพ์ชื่อเพลง)
• `/play` เปิดเพลง • `/pause` `/resume` • `/skip` ข้าม (คนอื่นต้องโหวต) • `/stop` `/leave`
• `/queue` คิว • `/nowplaying` • `/lyrics` เนื้อเพลง • `/seek 1:30` • `/volume`
• `/filter` เอฟเฟกต์ (bassboost, nightcore, slowed…) • `/autoplay` เล่นเพลงแนวเดียวกันต่อเมื่อคิวหมด
• `/loop` `/shuffle` `/remove` `/clear` • `/playlist save|load|list|delete` เพลย์ลิสต์ส่วนตัว
-# ยศชื่อ **DJ** / แอดมิน / คนที่อยู่คนเดียวในห้อง: ข้ามเพลงได้ทันที และใช้ /stop /clear ได้"""


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read message text / replies (enable in Developer Portal)
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/help • @mention เพื่อถาม AI"),
        )
        self._web_runner: web.AppRunner | None = None
        self.storage = Storage()

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.chat")
        await self.load_extension("cogs.music")

        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        if dev_guild:
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

        await self._start_health_server()

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


bot = Bot()


@bot.tree.command(name="help", description="ดูวิธีใช้บอท")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="วิธีใช้บอท", description=HELP_TEXT, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Slash command error", exc_info=error)
    msg = "❌ เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("ยังไม่ได้ตั้งค่า DISCORD_TOKEN ในไฟล์ .env")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
