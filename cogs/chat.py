"""Answer questions with an LLM when the bot is @mentioned (any channel) or DMed."""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "คุณคือผู้ช่วย AI ในเซิร์ฟเวอร์ Discord ที่ฉลาด เป็นกันเอง และตอบได้ทุกเรื่อง "
    "ตอบเป็นภาษาเดียวกับที่ผู้ใช้ถาม กระชับ ตรงประเด็น ใช้ Markdown ของ Discord ได้ "
    "(Discord แสดง LaTeX และตารางไม่ได้ ให้เขียนสูตรเป็นข้อความธรรมดา เช่น 6CO₂ + 6H₂O → C₆H₁₂O₆ + 6O₂) "
    "ข้อความจากผู้ใช้จะขึ้นต้นด้วยชื่อผู้ส่งในรูปแบบ 'ชื่อ: ข้อความ' เพราะในห้องมีหลายคน "
    "ถ้าไม่แน่ใจหรือเป็นข้อมูลที่อาจเปลี่ยนแปลงหลังจากที่คุณถูกฝึก ให้บอกตรงๆ"
)
DISCORD_LIMIT = 2000
THAI_TZ = timezone(timedelta(hours=7))


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Split long text into Discord-sized chunks, preferring newlines and keeping code blocks closed."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunk, text = text[:cut], text[cut:].lstrip("\n")
        # If we cut inside a ``` block, close it here and reopen it in the next chunk
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            text = "```\n" + text
        chunks.append(chunk)
    if text.strip():
        chunks.append(text)
    return chunks


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        models = [m.strip() for m in os.getenv("LLM_MODEL", "").split(",") if m.strip()]
        self.llm = LLMClient(
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=os.getenv("LLM_API_KEY", ""),
            models=models or ["openai/gpt-oss-120b"],
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1500")),
            web_search=os.getenv("LLM_WEB_SEARCH", "true").strip().lower() not in ("0", "false", "no", "off"),
        )
        self.system_prompt = os.getenv("SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT
        turns = int(os.getenv("CHAT_HISTORY_TURNS", "6"))
        # channel id -> recent messages (user/assistant pairs)
        self.history: dict[int, deque] = defaultdict(lambda: deque(maxlen=turns * 2))

    async def cog_unload(self) -> None:
        await self.llm.close()

    def _system_message(self, where: str) -> dict:
        now = datetime.now(THAI_TZ)
        content = (
            f"{self.system_prompt}\n\n"
            f"วันและเวลาปัจจุบัน: {now:%Y-%m-%d %H:%M} (UTC+7) ตรงกับปี พ.ศ. {now.year + 543} — "
            "นี่คือ 'วันนี้' จริง ไม่ใช่อนาคต\n"
            f"สถานที่: {where}"
        )
        if self.llm.search_available:
            content += (
                "\n\nคุณค้นเว็บได้ด้วยเครื่องมือ browser_search ให้ค้นเมื่อคำถามต้องใช้ข้อมูลล่าสุดหรือข้อเท็จจริงที่อาจเปลี่ยนแปลง "
                "เช่น ข่าว ราคา ผลกีฬา สภาพอากาศ เหตุการณ์ปัจจุบัน หรือเรื่องที่คุณไม่แน่ใจ "
                "ถ้าผลค้นหาไม่มีเนื้อหาพอ ให้เปิดหน้าเว็บนั้นอ่านก่อนตอบ ไม่ต้องค้นสำหรับคำถามทั่วไปที่ตอบได้อยู่แล้ว"
            )
        return {"role": "system", "content": content}

    async def ask(self, channel_id: int, where: str, author: str, question: str) -> str:
        history = self.history[channel_id]
        user_msg = {"role": "user", "content": f"{author}: {question}"}
        messages = [self._system_message(where), *history, user_msg]
        reply = await self.llm.chat(messages)
        history.append(user_msg)
        history.append({"role": "assistant", "content": reply.text})
        if not reply.sources:
            return reply.text
        links = " • ".join(
            f"[{title[:60].replace('[', '(').replace(']', ')')}](<{url}>)" for title, url in reply.sources[:3]
        )
        return f"{reply.text}\n\n-# 🔎 ค้นจากเว็บ: {links}"

    def _clean_content(self, message: discord.Message) -> str:
        me = self.bot.user
        text = re.sub(rf"<@!?{me.id}>", "", message.content)
        if message.guild and message.guild.self_role:
            text = text.replace(f"<@&{message.guild.self_role.id}>", "")
        # Turn remaining user/role/channel mentions into readable names
        for user in message.mentions:
            text = re.sub(rf"<@!?{user.id}>", f"@{user.display_name}", text)
        for role in message.role_mentions:
            text = text.replace(f"<@&{role.id}>", f"@{role.name}")
        for ch in message.channel_mentions:
            text = text.replace(f"<#{ch.id}>", f"#{ch.name}")
        return text.strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None:
            return
        is_dm = message.guild is None
        # Respond to a direct @bot mention, or to the bot's own auto-created role (people often pick that
        # by mistake). @everyone / @here are ignored.
        if not is_dm:
            bot_role = message.guild.self_role
            if self.bot.user not in message.mentions and (bot_role is None or bot_role not in message.role_mentions):
                return

        question = self._clean_content(message)

        # Include the message being replied to, so "@bot explain this" works on a reply
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message) and ref.content:
            who = "คุณ (บอท)" if ref.author == self.bot.user else ref.author.display_name
            question = f"[กำลังตอบกลับข้อความของ {who}: \"{ref.content[:1500]}\"]\n{question}"

        # Include text file attachments (code, logs, etc.)
        for att in message.attachments[:3]:
            if att.size < 100_000 and (att.content_type or "").startswith("text"):
                try:
                    body = (await att.read()).decode("utf-8", errors="replace")
                    question += f"\n\n[ไฟล์แนบ {att.filename}]\n{body[:8000]}"
                except discord.HTTPException:
                    pass

        if not question.strip():
            await message.reply("สวัสดีครับ 👋 มีอะไรให้ช่วยไหม? พิมพ์คำถามต่อท้ายการ mention ได้เลย", mention_author=False)
            return

        where = "ข้อความส่วนตัว (DM)" if is_dm else f"เซิร์ฟเวอร์ {message.guild.name} ห้อง #{message.channel.name}"
        try:
            async with message.channel.typing():
                answer = await self.ask(message.channel.id, where, message.author.display_name, question)
        except LLMError as e:
            log.error("LLM error: %s", e)
            await message.reply("ขอโทษครับ ตอนนี้ AI ตอบไม่ได้ (อาจโดน rate limit ของโมเดลฟรี) ลองใหม่อีกครั้งในอีกสักครู่นะครับ 🙏", mention_author=False)
            return

        no_pings = discord.AllowedMentions.none()
        chunks = split_message(answer)
        await message.reply(chunks[0], mention_author=False, allowed_mentions=no_pings)
        for chunk in chunks[1:]:
            await message.channel.send(chunk, allowed_mentions=no_pings)

    @app_commands.command(name="ask", description="ถามคำถาม AI")
    @app_commands.describe(question="คำถามของคุณ")
    async def ask_cmd(self, interaction: discord.Interaction, question: str) -> None:
        await interaction.response.defer(thinking=True)
        where = f"เซิร์ฟเวอร์ {interaction.guild.name}" if interaction.guild else "DM"
        try:
            answer = await self.ask(interaction.channel_id, where, interaction.user.display_name, question)
        except LLMError as e:
            log.error("LLM error: %s", e)
            await interaction.followup.send("ขอโทษครับ ตอนนี้ AI ตอบไม่ได้ ลองใหม่อีกครั้งนะครับ 🙏")
            return
        no_pings = discord.AllowedMentions.none()
        header = f"> **{interaction.user.display_name}:** {question[:300]}\n\n"
        chunks = split_message(header + answer)
        for chunk in chunks:
            await interaction.followup.send(chunk, allowed_mentions=no_pings)

    @app_commands.command(name="reset", description="ล้างความจำบทสนทนาของ AI ในห้องนี้")
    async def reset_cmd(self, interaction: discord.Interaction) -> None:
        self.history.pop(interaction.channel_id, None)
        await interaction.response.send_message("🧹 ล้างความจำบทสนทนาในห้องนี้แล้ว")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
