"""Reminders (/remind), welcome cards for new members (/welcome) and bot health (/status)."""
from __future__ import annotations

import asyncio
import io
import logging
import platform
import time
from datetime import datetime, timedelta

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

from core.card import render_welcome
from core.llm import LLMError
from core.storage import DB_PATH
from core.timeparse import THAI_TZ, parse_when

log = logging.getLogger(__name__)

MAX_REMINDERS_PER_USER = 25
DEFAULT_WELCOME = "ยินดีต้อนรับ {user} สู่ **{server}** 🎉"
TIME_EXAMPLES = "`30m`, `2h`, `1 ชั่วโมง 30 นาที`, `18:30`, `พรุ่งนี้ 9:00`, `25/12 20:00`, `2026-12-31 23:59`"


class Utility(commands.Cog):
    remind = app_commands.Group(name="remind", description="⏰ ตั้งเตือนความจำ")
    welcome = app_commands.Group(
        name="welcome", description="👋 ตั้งค่าการ์ดต้อนรับสมาชิกใหม่",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = bot.storage

    async def cog_load(self) -> None:
        self._reminder_loop.start()

    async def cog_unload(self) -> None:
        self._reminder_loop.cancel()

    # ---------- reminders ----------

    @tasks.loop(seconds=15)
    async def _reminder_loop(self) -> None:
        for reminder in await self.storage.pop_due_reminders(time.time()):
            try:
                await self._deliver(reminder)
            except Exception:
                log.exception("Could not deliver reminder %s", reminder["id"])

    @_reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver(self, r: dict) -> None:
        user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
        late = time.time() - r["due"]
        note = f"\n-# (ช้าไป {int(late // 60)} นาที เพราะบอทออฟไลน์อยู่)" if late > 120 else ""
        text = f"⏰ {user.mention} เตือนความจำ: **{r['text']}**{note}"
        mentions = discord.AllowedMentions(users=[user], everyone=False, roles=False)
        if not r["dm"] and r["channel_id"]:
            channel = self.bot.get_channel(r["channel_id"])
            if channel:
                try:
                    await channel.send(text, allowed_mentions=mentions)
                    return
                except discord.HTTPException:
                    pass
        try:
            await user.send(text)
        except discord.HTTPException:
            log.info("Could not DM reminder to %s", user)

    @remind.command(name="set", description="ตั้งเตือนความจำ")
    @app_commands.describe(when=f"เมื่อไหร่ เช่น 30m, 2h, 18:30, พรุ่งนี้ 9:00, 25/12 20:00",
                           text="ให้เตือนเรื่องอะไร", dm="ส่งเตือนทาง DM แทนห้องนี้")
    async def remind_set(self, interaction: discord.Interaction, when: str,
                         text: app_commands.Range[str, 1, 500], dm: bool = False) -> None:
        due = parse_when(when)
        if not due:
            await interaction.response.send_message(f"❓ ไม่เข้าใจเวลา **{when}** — ใช้แบบนี้ได้: {TIME_EXAMPLES}", ephemeral=True)
            return
        if due - datetime.now(THAI_TZ) > timedelta(days=366):
            await interaction.response.send_message("ตั้งเตือนล่วงหน้าได้ไม่เกิน 1 ปีนะ", ephemeral=True)
            return
        if len(await self.storage.list_reminders(interaction.user.id)) >= MAX_REMINDERS_PER_USER:
            await interaction.response.send_message(f"มีการเตือนค้างอยู่ครบ {MAX_REMINDERS_PER_USER} อันแล้ว ลบบางอันด้วย `/remind cancel` ก่อนนะ", ephemeral=True)
            return
        dm = dm or interaction.guild is None
        reminder_id = await self.storage.add_reminder(
            interaction.user.id, interaction.guild_id, interaction.channel_id, due.timestamp(), text, dm)
        where = "ทาง DM" if dm else "ในห้องนี้"
        await interaction.response.send_message(
            f"⏰ ตั้งเตือนแล้ว (#{reminder_id}) — {discord.utils.format_dt(due, 'F')} "
            f"({discord.utils.format_dt(due, 'R')}) {where}\n> {text}", ephemeral=True)

    @remind.command(name="list", description="ดูการเตือนที่ตั้งไว้")
    async def remind_list(self, interaction: discord.Interaction) -> None:
        rows = await self.storage.list_reminders(interaction.user.id)
        if not rows:
            await interaction.response.send_message("ยังไม่มีการเตือน ตั้งได้ด้วย `/remind set`", ephemeral=True)
            return
        lines = [f"`#{rid}` {discord.utils.format_dt(datetime.fromtimestamp(due, THAI_TZ), 'R')} — {text[:80]}"
                 for rid, due, text in rows]
        await interaction.response.send_message("⏰ **การเตือนของคุณ**\n" + "\n".join(lines), ephemeral=True)

    @remind.command(name="cancel", description="ยกเลิกการเตือน")
    @app_commands.describe(reminder="การเตือนที่จะยกเลิก")
    async def remind_cancel(self, interaction: discord.Interaction, reminder: int) -> None:
        if await self.storage.delete_reminder(interaction.user.id, reminder):
            await interaction.response.send_message(f"🗑️ ยกเลิกการเตือน #{reminder} แล้ว", ephemeral=True)
        else:
            await interaction.response.send_message(f"ไม่พบการเตือน #{reminder}", ephemeral=True)

    @remind_cancel.autocomplete("reminder")
    async def _reminder_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        rows = await self.storage.list_reminders(interaction.user.id)
        choices = []
        for rid, due, text in rows:
            label = f"#{rid} {datetime.fromtimestamp(due, THAI_TZ):%d/%m %H:%M} — {text}"[:100]
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=rid))
        return choices[:25]

    # ---------- welcome cards ----------

    async def _greeting(self, member: discord.Member, number: int) -> str | None:
        chat = self.bot.get_cog("Chat")
        if not chat:
            return None
        persona = await self.storage.get_setting(member.guild.id, "persona")
        try:
            return await chat.one_shot(
                "เขียนคำทักทายต้อนรับสมาชิกใหม่ของเซิร์ฟเวอร์ Discord 1-2 ประโยค อบอุ่น สนุก เป็นกันเอง "
                "ใส่อีโมจิได้นิดหน่อย ห้ามใส่ @mention ตอบเฉพาะคำทักทาย"
                + (f"\nบุคลิกการพูด: {persona}" if persona else ""),
                f"สมาชิกใหม่ชื่อ {member.display_name} เพิ่งเข้าร่วมเซิร์ฟเวอร์ {member.guild.name} เป็นสมาชิกคนที่ {number}",
            )
        except LLMError as e:
            log.info("Welcome greeting failed: %s", e)
            return None

    async def send_welcome(self, member: discord.Member, channel: discord.abc.Messageable) -> None:
        guild = member.guild
        number = guild.member_count or len(guild.members)
        avatar = await member.display_avatar.replace(size=256, static_format="png").read()
        background = None
        for asset in (guild.banner, guild.icon):
            if asset:
                background = await asset.replace(size=1024, static_format="png").read()
                break
        png, accent = await asyncio.get_running_loop().run_in_executor(
            None, render_welcome, avatar, background, member.display_name, member.name, guild.name, number)

        template = await self.storage.get_setting(guild.id, "welcome_message") or DEFAULT_WELCOME
        try:
            content = template.format(user=member.mention, name=member.display_name, server=guild.name, count=number)
        except (KeyError, IndexError, ValueError):
            content = template
        embed = discord.Embed(color=discord.Color.from_rgb(*accent))
        embed.set_image(url="attachment://welcome.png")
        if await self.storage.get_setting(guild.id, "welcome_ai") != "0":
            greeting = await self._greeting(member, number)
            if greeting:
                embed.description = greeting[:1000]
        await channel.send(content, embed=embed, file=discord.File(io.BytesIO(png), filename="welcome.png"),
                           allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        channel_id = await self.storage.get_setting(member.guild.id, "welcome_channel")
        channel = member.guild.get_channel(int(channel_id)) if channel_id else None
        if channel:
            try:
                await self.send_welcome(member, channel)
            except Exception:
                log.exception("Welcome card failed in guild %s", member.guild.id)

    @welcome.command(name="channel", description="ตั้งห้องที่จะส่งการ์ดต้อนรับ (เปิดใช้งาน)")
    @app_commands.describe(channel="ห้องที่จะส่งการ์ดต้อนรับ")
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files and perms.embed_links):
            await interaction.response.send_message(f"บอทไม่มีสิทธิ์ส่งข้อความ/แนบรูปใน {channel.mention}", ephemeral=True)
            return
        await self.storage.set_setting(interaction.guild_id, "welcome_channel", str(channel.id))
        note = "" if self.bot.intents.members else (
            "\n⚠️ ตอนนี้บอทยังไม่เห็นสมาชิกใหม่ ต้องเปิด **Server Members Intent** ใน Developer Portal แล้วรีสตาร์ตบอทก่อน")
        await interaction.response.send_message(f"👋 จะส่งการ์ดต้อนรับสมาชิกใหม่ใน {channel.mention} — ลองดูตัวอย่างด้วย `/welcome test`{note}", ephemeral=True)

    @welcome.command(name="message", description="ตั้งข้อความต้อนรับ ({user} {name} {server} {count})")
    @app_commands.describe(text="เช่น 'ยินดีต้อนรับ {user} คนที่ {count} ของ {server} 🎉' (เว้นว่างเพื่อใช้ค่าเริ่มต้น)")
    async def welcome_message(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 500] | None = None) -> None:
        await self.storage.set_setting(interaction.guild_id, "welcome_message", text)
        await interaction.response.send_message(f"✅ ข้อความต้อนรับ: {text or DEFAULT_WELCOME}", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @welcome.command(name="ai", description="เปิด/ปิดคำทักทายที่ AI แต่งให้บนการ์ด")
    async def welcome_ai(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.storage.set_setting(interaction.guild_id, "welcome_ai", "1" if enabled else "0")
        await interaction.response.send_message("🤖 เปิดคำทักทายจาก AI แล้ว" if enabled else "🤖 ปิดคำทักทายจาก AI แล้ว", ephemeral=True)

    @welcome.command(name="test", description="ดูตัวอย่างการ์ดต้อนรับ (ใช้รูปของคุณ)")
    async def welcome_test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = await self.storage.get_setting(interaction.guild_id, "welcome_channel")
        try:
            await self.send_welcome(interaction.user, interaction.channel)
        except Exception as e:
            log.exception("Welcome test failed")
            await interaction.followup.send(f"❌ สร้างการ์ดไม่สำเร็จ: `{str(e)[:200]}`", ephemeral=True)
            return
        where = f"<#{channel_id}>" if channel_id else "ยังไม่ได้ตั้งห้อง (ใช้ `/welcome channel`)"
        await interaction.followup.send(f"✅ ส่งตัวอย่างแล้ว • ห้องต้อนรับ: {where}", ephemeral=True)

    @welcome.command(name="off", description="ปิดการ์ดต้อนรับ")
    async def welcome_off(self, interaction: discord.Interaction) -> None:
        await self.storage.set_setting(interaction.guild_id, "welcome_channel", None)
        await interaction.response.send_message("🔕 ปิดการ์ดต้อนรับแล้ว", ephemeral=True)

    # ---------- /status ----------

    @app_commands.command(name="status", description="ดูสถานะของบอท")
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        uptime = timedelta(seconds=int(time.time() - getattr(bot, "started_at", time.time())))
        chat, music, quiz = bot.get_cog("Chat"), bot.get_cog("Music"), bot.get_cog("Quiz")
        try:
            import yt_dlp.version
            ytdlp = yt_dlp.version.__version__
        except Exception:
            ytdlp = "?"
        rss = psutil.Process().memory_info().rss / 1024 / 1024
        db = DB_PATH.stat().st_size / 1024 if DB_PATH.exists() else 0

        embed = discord.Embed(title="📊 สถานะบอท", color=discord.Color.green())
        embed.add_field(name="⏱️ ออนไลน์มาแล้ว", value=str(uptime))
        embed.add_field(name="📶 Ping", value=f"{bot.latency * 1000:.0f} ms")
        embed.add_field(name="🏠 เซิร์ฟเวอร์", value=str(len(bot.guilds)))
        embed.add_field(name="🎵 กำลังเปิดเพลง", value=f"{len(music.players) if music else 0} เซิร์ฟเวอร์")
        embed.add_field(name="🎮 เกมทายเพลง", value=f"{len(quiz.games) if quiz else 0} เกม")
        embed.add_field(name="💾 RAM / DB", value=f"{rss:.0f} MB / {db:.0f} KB")
        if chat:
            embed.add_field(name="🤖 โมเดล AI", value="\n".join(f"`{m}`" for m in chat.llm.models), inline=False)
            features = [
                f"🔎 ค้นเว็บ: {'เปิด' if chat.llm.search_available else 'ปิด'}",
                f"👁️ อ่านรูป: {'เปิด' if chat.vision else 'ปิด (ไม่มี GEMINI_API_KEY)'}",
                f"🎙️ ถอดเสียง: `{chat.llm.whisper_model}`",
                f"⏳ จำกัด: {chat.limiter.limit} ครั้ง / {chat.limiter.window:.0f} วินาที ต่อคน",
            ]
            embed.add_field(name="✨ ความสามารถ", value="\n".join(features), inline=False)
        embed.set_footer(text=f"yt-dlp {ytdlp} • discord.py {discord.__version__} • Python {platform.python_version()}"
                              f" • Members intent: {'เปิด' if bot.intents.members else 'ปิด'}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
