"""AI chat: answers @mentions / replies / DMs, /ask, /summarize, /persona, right-click translate & explain,
and reads images through a vision model (Gemini) when one is configured."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import random
import re
import time
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.llm import LLMClient, LLMError, LLMReply

log = logging.getLogger(__name__)

BASE_RULES = (
    "คุณคือผู้ช่วย AI ในเซิร์ฟเวอร์ Discord ที่ตอบได้ทุกเรื่อง "
    "ตอบเป็นภาษาเดียวกับที่ผู้ใช้ถาม ใช้ Markdown ของ Discord ได้ "
    "(Discord แสดง LaTeX และตารางไม่ได้ ให้เขียนสูตรเป็นข้อความธรรมดา เช่น 6CO₂ + 6H₂O → C₆H₁₂O₆ + 6O₂) "
    "ข้อความจากผู้ใช้จะขึ้นต้นด้วยชื่อผู้ส่งในรูปแบบ 'ชื่อ: ข้อความ' เพราะในห้องมีหลายคน "
    "ถ้าไม่แน่ใจหรือเป็นข้อมูลที่อาจเปลี่ยนแปลงหลังจากที่คุณถูกฝึก ให้บอกตรงๆ"
)
DEFAULT_PERSONA = "ฉลาด เป็นกันเอง สุภาพ ตอบกระชับตรงประเด็น"
THAI_TZ = timezone(timedelta(hours=7))
NO_PINGS = discord.AllowedMentions.none()
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq Whisper upload limit
# "@bot เปิดเพลงเศร้าๆ ให้หน่อย", "ขอเพลงแนว lofi", "play some jazz" -> AI DJ (questions about how to play don't count)
_DJ_INTENT = re.compile(
    r"^\s*(?:ช่วย|ขอ|อยาก|ไหน|ลอง)?\s*(?:เปิด|เล่น|ขอ|หา|อยากฟัง)\s*เพลง|เปิด.{1,40}(?:ให้ฟัง|หน่อย|ที|เลย)|^\s*(?:play|dj)\b",
    re.I,
)
_DJ_NOT = re.compile(r"ยังไง|อย่างไร|วิธี|ทำไม|ได้ไหม\s*\?|how\b|why\b", re.I)
IMAGE_SIZES = {"square": (1024, 1024), "landscape": (1344, 768), "portrait": (768, 1344)}
LLM_UNAVAILABLE = "ขอโทษครับ ตอนนี้ AI ตอบไม่ได้ (อาจโดน rate limit ของโมเดลฟรี) ลองใหม่อีกครั้งในอีกสักครู่นะครับ 🙏"


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


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def _models(var: str, default: str) -> list[str]:
    return [m.strip() for m in (os.getenv(var) or default).split(",") if m.strip()]


class RateLimiter:
    """At most `limit` uses per `window` seconds per key (sliding window)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[int, deque] = defaultdict(deque)

    def retry_after(self, key: int) -> float:
        """0 if allowed now (and counts the use), otherwise seconds to wait."""
        if self.limit <= 0:
            return 0.0
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.window - (now - hits[0])
        hits.append(now)
        return 0.0


class Chat(commands.Cog):
    persona = app_commands.Group(
        name="persona", description="ตั้งบุคลิกของ AI ในเซิร์ฟเวอร์นี้",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = bot.storage
        self.llm = LLMClient(
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=os.getenv("LLM_API_KEY", ""),
            models=_models("LLM_MODEL", "openai/gpt-oss-120b"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1500")),
            web_search=_env_flag("LLM_WEB_SEARCH"),
        )
        # Optional second client for images (Groq's free text models can't see pictures)
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.vision = LLMClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key=gemini_key,
            models=_models("VISION_MODEL", "gemini-3.8-flash,gemini-3.5-flash-lite"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1500")),
        ) if gemini_key else None
        self.default_persona = os.getenv("SYSTEM_PROMPT", "").strip() or DEFAULT_PERSONA
        self.history_len = int(os.getenv("CHAT_HISTORY_TURNS", "6")) * 2
        self.limiter = RateLimiter(int(os.getenv("AI_USER_LIMIT", "5")), float(os.getenv("AI_USER_WINDOW", "60")))
        self._ai_messages: OrderedDict[int, None] = OrderedDict()  # ids of AI answers, so replies continue the chat

        self.context_menus = [
            app_commands.ContextMenu(name="แปลภาษา", callback=self.translate_message),
            app_commands.ContextMenu(name="สรุป / อธิบาย", callback=self.explain_message),
            app_commands.ContextMenu(name="ถอดข้อความเสียง", callback=self.transcribe_message),
        ]
        for menu in self.context_menus:
            bot.tree.add_command(menu)

    async def cog_unload(self) -> None:
        for menu in self.context_menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)
        await self.llm.close()
        if self.vision:
            await self.vision.close()

    # ---------- prompt building ----------

    async def _system_message(self, where: str, guild_id: int | None, search: bool = True) -> dict:
        persona = (await self.storage.get_setting(guild_id, "persona")) if guild_id else None
        now = datetime.now(THAI_TZ)
        content = (
            f"{BASE_RULES}\n\nบุคลิกและสไตล์การพูดของคุณ: {persona or self.default_persona}\n\n"
            f"วันและเวลาปัจจุบัน: {now:%Y-%m-%d %H:%M} (UTC+7) ตรงกับปี พ.ศ. {now.year + 543} — "
            "นี่คือ 'วันนี้' จริง ไม่ใช่อนาคต\n"
            f"สถานที่: {where}"
        )
        if search and self.llm.search_available:
            content += (
                "\n\nคุณค้นเว็บได้ด้วยเครื่องมือ browser_search ให้ค้นเมื่อคำถามต้องใช้ข้อมูลล่าสุดหรือข้อเท็จจริงที่อาจเปลี่ยนแปลง "
                "เช่น ข่าว ราคา ผลกีฬา สภาพอากาศ เหตุการณ์ปัจจุบัน หรือเรื่องที่คุณไม่แน่ใจ "
                "ถ้าผลค้นหาไม่มีเนื้อหาพอ ให้เปิดหน้าเว็บนั้นอ่านก่อนตอบ ไม่ต้องค้นสำหรับคำถามทั่วไปที่ตอบได้อยู่แล้ว"
            )
        return {"role": "system", "content": content}

    @staticmethod
    def _format(reply: LLMReply) -> str:
        if not reply.sources:
            return reply.text
        links = " • ".join(
            f"[{title[:60].replace('[', '(').replace(']', ')')}](<{url}>)" for title, url in reply.sources[:3]
        )
        return f"{reply.text}\n\n-# 🔎 ค้นจากเว็บ: {links}"

    async def ask(self, channel_id: int, guild_id: int | None, where: str, author: str, question: str,
                  images: list[str] | None = None) -> str:
        """Answer with the channel's chat memory; images (data URLs) go to the vision model."""
        history = await self.storage.get_history(channel_id, self.history_len)
        text = f"{author}: {question}"
        if images and self.vision:
            system = await self._system_message(where, guild_id, search=False)
            parts = [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
            reply = await self.vision.chat([system, *history, {"role": "user", "content": parts}], allow_search=False)
            stored = f"{text}\n[แนบรูป {len(images)} รูป]"
        else:
            if images:  # no vision model configured
                text += "\n[ผู้ใช้แนบรูปมาด้วย แต่คุณมองไม่เห็นรูป ให้บอกผู้ใช้ว่าตอนนี้ยังอ่านรูปไม่ได้ (แอดมินต้องตั้ง GEMINI_API_KEY)]"
            system = await self._system_message(where, guild_id)
            reply = await self.llm.chat([system, *history, {"role": "user", "content": text}])
            stored = text
        # Some models copy the "Name: message" format we use for users and start with "Name:" themselves
        reply.text = re.sub(rf"^\s*\**\s*{re.escape(author)}\s*\**\s*:\s*\**\s*", "", reply.text).strip() or reply.text
        await self.storage.add_history(
            channel_id, [{"role": "user", "content": stored}, {"role": "assistant", "content": reply.text}],
            keep=self.history_len,
        )
        return self._format(reply)

    async def one_shot(self, instruction: str, text: str, search: bool = False) -> str:
        """A single request without chat memory (translate / explain / summarize)."""
        system = {"role": "system", "content": f"{BASE_RULES}\n\n{instruction}"}
        reply = await self.llm.chat([system, {"role": "user", "content": text}], allow_search=search)
        return self._format(reply)

    # ---------- message handling ----------

    def _remember(self, message: discord.Message) -> None:
        self._ai_messages[message.id] = None
        while len(self._ai_messages) > 2000:
            self._ai_messages.popitem(last=False)

    def _addressed(self, message: discord.Message) -> bool:
        """DM, @bot (or the bot's role), or a reply to one of the bot's chat answers."""
        if message.guild is None:
            return True
        me = self.bot.user
        bot_role = message.guild.self_role
        if me in message.mentions or (bot_role and bot_role in message.role_mentions):
            return True
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message) and ref.author.id == me.id:
            # AI answers are plain text messages; music cards / slash-command replies are not
            return ref.id in self._ai_messages or (bool(ref.content) and not ref.embeds and ref.interaction_metadata is None)
        return False

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
            text = text.replace(f"<#{ch.id}>", f"#{getattr(ch, 'name', ch.id)}")
        return text.strip()

    @staticmethod
    async def _images(*messages: discord.Message | None) -> list[str]:
        urls: list[str] = []
        for msg in messages:
            if not isinstance(msg, discord.Message):
                continue
            for att in msg.attachments:
                if len(urls) >= 3:
                    return urls
                ctype = (att.content_type or "").split(";")[0]
                if ctype in IMAGE_TYPES and att.size <= MAX_IMAGE_BYTES:
                    try:
                        data = await att.read()
                    except discord.HTTPException:
                        continue
                    urls.append(f"data:{ctype};base64,{base64.b64encode(data).decode()}")
        return urls

    async def _reply(self, message: discord.Message, text: str) -> None:
        chunks = split_message(text)
        sent = await message.reply(chunks[0], mention_author=False, allowed_mentions=NO_PINGS)
        self._remember(sent)
        for chunk in chunks[1:]:
            self._remember(await message.channel.send(chunk, allowed_mentions=NO_PINGS))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None or not self._addressed(message):
            return

        wait = self.limiter.retry_after(message.author.id)
        if wait:
            await message.reply(f"⏳ ใจเย็นๆ นะ ถามได้อีกครั้งในอีก {wait:.0f} วินาที", mention_author=False, delete_after=8)
            return

        question = self._clean_content(message)
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message) and ref.content and ref.id not in self._ai_messages:
            # Include the message being replied to, so "@bot explain this" works on a reply.
            # (Skipped for the bot's own answers: those are already in the chat memory.)
            who = "คุณ (บอท)" if ref.author == self.bot.user else ref.author.display_name
            question = f"[กำลังตอบกลับข้อความของ {who}: \"{ref.content[:1500]}\"]\n{question}"

        # Text file attachments (code, logs, ...)
        for att in message.attachments[:3]:
            if att.size < 100_000 and (att.content_type or "").startswith("text"):
                try:
                    body = (await att.read()).decode("utf-8", errors="replace")
                    question += f"\n\n[ไฟล์แนบ {att.filename}]\n{body[:8000]}"
                except discord.HTTPException:
                    pass

        is_dm = message.guild is None
        # Voice messages / audio files: speech to text first
        transcript = None
        if self._audio(message):
            try:
                async with message.channel.typing():
                    transcript = await self._transcribe(message)
            except LLMError as e:
                log.error("Transcription error: %s", e)
                await message.reply("ขอโทษครับ ถอดข้อความเสียงไม่ได้ ลองใหม่อีกครั้งนะ 🙏", mention_author=False)
                return
            if transcript:
                question = f"{question}\n{transcript}".strip()

        images = await self._images(message, ref)
        if not question.strip() and not images:
            await message.reply("สวัสดีครับ 👋 มีอะไรให้ช่วยไหม? พิมพ์คำถามต่อท้ายการ mention ได้เลย", mention_author=False)
            return
        if not question.strip():
            question = "ช่วยอธิบายรูปนี้หน่อย"

        # "@bot เปิดเพลงเศร้าๆ ให้หน่อย" -> AI DJ picks songs and queues them
        music = self.bot.get_cog("Music")
        request = transcript or self._clean_content(message)
        if music and not is_dm and not images and _DJ_INTENT.search(request) and not _DJ_NOT.search(request):
            try:
                async with message.channel.typing():
                    result = await music.dj_request(message.author, message.channel, request)
            except LLMError as e:
                log.error("DJ LLM error: %s", e)
                await message.reply(LLM_UNAVAILABLE, mention_author=False)
                return
            if isinstance(result, dict):  # banner card with the picked songs
                await message.reply(mention_author=False, allowed_mentions=NO_PINGS, **result)
            else:
                await self._reply(message, (f"> 🎙️ {transcript[:300]}\n" if transcript else "") + result)
            return

        where = "ข้อความส่วนตัว (DM)" if is_dm else f"เซิร์ฟเวอร์ {message.guild.name} ห้อง #{message.channel.name}"
        try:
            async with message.channel.typing():
                answer = await self.ask(message.channel.id, message.guild and message.guild.id, where,
                                        message.author.display_name, question, images)
        except LLMError as e:
            log.error("LLM error: %s", e)
            await message.reply(LLM_UNAVAILABLE, mention_author=False)
            return
        await self._reply(message, (f"> 🎙️ *{transcript[:300]}*\n\n" if transcript else "") + answer)

        # Talk mode: also read the answer aloud in the asker's voice channel
        if music and not is_dm:
            gender = await self.storage.get_setting(message.guild.id, "talk")
            voice = message.author.voice
            vc = message.guild.voice_client
            if gender and voice and voice.channel and (vc is None or vc.channel == voice.channel):
                asyncio.create_task(self._speak(music, message, answer, gender))

    async def _speak(self, music, message: discord.Message, text: str, gender: str) -> None:
        error = await music.speak_text(message.author, message.channel, text, gender)
        if error:
            log.info("Talk mode could not speak: %s", error)

    @staticmethod
    def _audio(message: discord.Message) -> discord.Attachment | None:
        for att in message.attachments:
            ctype = (att.content_type or "").split(";")[0]
            if (att.is_voice_message() or ctype.startswith("audio/")) and att.size <= MAX_AUDIO_BYTES:
                return att
        return None

    async def _transcribe(self, message: discord.Message) -> str | None:
        att = self._audio(message)
        if not att:
            return None
        data = await att.read()
        ctype = (att.content_type or "audio/ogg").split(";")[0]
        return await self.llm.transcribe(data, att.filename or "voice.ogg", ctype)

    # ---------- slash commands ----------

    async def _check_limit(self, interaction: discord.Interaction) -> bool:
        wait = self.limiter.retry_after(interaction.user.id)
        if wait:
            await interaction.response.send_message(f"⏳ ใจเย็นๆ นะ ใช้ได้อีกครั้งในอีก {wait:.0f} วินาที", ephemeral=True)
            return False
        return True

    @app_commands.command(name="ask", description="ถามคำถาม AI")
    @app_commands.describe(question="คำถามของคุณ", image="แนบรูปให้ AI ดู (ไม่บังคับ)")
    async def ask_cmd(self, interaction: discord.Interaction, question: str, image: discord.Attachment | None = None) -> None:
        if not await self._check_limit(interaction):
            return
        await interaction.response.defer(thinking=True)
        images = []
        if image and (image.content_type or "").split(";")[0] in IMAGE_TYPES and image.size <= MAX_IMAGE_BYTES:
            images = [f"data:{image.content_type.split(';')[0]};base64,{base64.b64encode(await image.read()).decode()}"]
        where = f"เซิร์ฟเวอร์ {interaction.guild.name}" if interaction.guild else "DM"
        try:
            answer = await self.ask(interaction.channel_id, interaction.guild_id, where,
                                    interaction.user.display_name, question, images)
        except LLMError as e:
            log.error("LLM error: %s", e)
            await interaction.followup.send(LLM_UNAVAILABLE)
            return
        header = f"> **{interaction.user.display_name}:** {question[:300]}\n\n"
        for chunk in split_message(header + answer):
            self._remember(await interaction.followup.send(chunk, allowed_mentions=NO_PINGS, wait=True))

    @app_commands.command(name="reset", description="ล้างความจำบทสนทนาของ AI ในห้องนี้")
    async def reset_cmd(self, interaction: discord.Interaction) -> None:
        await self.storage.clear_history(interaction.channel_id)
        await interaction.response.send_message("🧹 ล้างความจำบทสนทนาในห้องนี้แล้ว")

    @app_commands.command(name="summarize", description="ให้ AI สรุปแชทล่าสุดในห้องนี้")
    @app_commands.describe(count="จำนวนข้อความล่าสุดที่จะสรุป (10-200)")
    async def summarize_cmd(self, interaction: discord.Interaction, count: app_commands.Range[int, 10, 200] = 50) -> None:
        if not await self._check_limit(interaction):
            return
        await interaction.response.defer(thinking=True)
        lines: list[str] = []
        try:
            async for msg in interaction.channel.history(limit=count):
                text = msg.clean_content or (msg.embeds[0].description if msg.embeds else "") or ""
                if msg.attachments:
                    text += f" [แนบไฟล์ {len(msg.attachments)} ไฟล์]"
                if text.strip():
                    stamp = msg.created_at.astimezone(THAI_TZ).strftime("%H:%M")
                    lines.append(f"[{stamp}] {msg.author.display_name}: {text[:400]}")
        except discord.Forbidden:
            await interaction.followup.send("❌ บอทไม่มีสิทธิ์อ่านประวัติข้อความในห้องนี้")
            return
        # newest first from Discord; keep as many recent lines as fit, then restore chronological order
        transcript, size = [], 0
        for line in lines:
            if size + len(line) > 9000:
                break
            transcript.append(line)
            size += len(line)
        if not transcript:
            await interaction.followup.send("ไม่มีข้อความให้สรุป")
            return
        transcript.reverse()
        try:
            summary = await self.one_shot(
                "สรุปบทสนทนาในห้องแชทต่อไปนี้เป็นภาษาไทยแบบกระชับ: หัวข้อหลักที่คุยกัน ข้อสรุป/สิ่งที่ตกลงกัน "
                "และคำถามที่ยังค้างอยู่ ใช้ bullet point และระบุชื่อคนเมื่อจำเป็น",
                "\n".join(transcript),
            )
        except LLMError as e:
            log.error("LLM error: %s", e)
            await interaction.followup.send(LLM_UNAVAILABLE)
            return
        header = f"📝 **สรุป {len(transcript)} ข้อความล่าสุด**\n\n"
        for chunk in split_message(header + summary):
            await interaction.followup.send(chunk, allowed_mentions=NO_PINGS)

    # ---------- right-click (message context menu) ----------

    @staticmethod
    def _message_text(message: discord.Message) -> str:
        text = message.content
        if not text and message.embeds:
            e = message.embeds[0]
            text = "\n".join(x for x in (e.title, e.description) if x)
        return text.strip()

    async def _context_action(self, interaction: discord.Interaction, message: discord.Message,
                              instruction: str, search: bool) -> None:
        if not await self._check_limit(interaction):
            return
        text = self._message_text(message)
        if not text:
            await interaction.response.send_message("ข้อความนี้ไม่มีตัวอักษรให้ประมวลผล", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.one_shot(instruction, text[:6000], search=search)
        except LLMError as e:
            log.error("LLM error: %s", e)
            await interaction.followup.send(LLM_UNAVAILABLE, ephemeral=True)
            return
        for chunk in split_message(f"-# จากข้อความของ {message.author.display_name}: {message.jump_url}\n{result}"):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_PINGS)

    async def translate_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self._context_action(
            interaction, message,
            "แปลข้อความที่ผู้ใช้ส่งมา: ถ้าเป็นภาษาไทยให้แปลเป็นภาษาอังกฤษ ถ้าเป็นภาษาอื่นให้แปลเป็นภาษาไทย "
            "ตอบเฉพาะคำแปลที่เป็นธรรมชาติ ไม่ต้องอธิบายเพิ่ม",
            search=False,
        )

    async def explain_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self._context_action(
            interaction, message,
            "สรุปหรืออธิบายข้อความที่ผู้ใช้ส่งมาเป็นภาษาไทยให้เข้าใจง่าย ถ้าข้อความยาวให้สรุปใจความสำคัญ "
            "ถ้าสั้นหรือมีศัพท์เฉพาะ/มุก/โค้ด ให้อธิบายความหมาย",
            search=True,
        )

    async def transcribe_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not self._audio(message):
            await interaction.response.send_message("ข้อความนี้ไม่มีข้อความเสียงหรือไฟล์เสียง", ephemeral=True)
            return
        if not await self._check_limit(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self._transcribe(message)
        except LLMError as e:
            log.error("Transcription error: %s", e)
            await interaction.followup.send("ขอโทษครับ ถอดข้อความเสียงไม่ได้ ลองใหม่อีกครั้งนะ 🙏", ephemeral=True)
            return
        for chunk in split_message(f"🎙️ **{message.author.display_name}** พูดว่า:\n>>> {text or '(ไม่ได้ยินคำพูด)'}"):
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=NO_PINGS)

    # ---------- /imagine ----------

    @app_commands.command(name="imagine", description="ให้ AI วาดรูปจากข้อความ")
    @app_commands.describe(prompt="อยากได้รูปอะไร พิมพ์ไทยได้เลย", shape="รูปทรงภาพ")
    @app_commands.choices(shape=[app_commands.Choice(name="⬜ สี่เหลี่ยมจัตุรัส", value="square"),
                                 app_commands.Choice(name="🖼️ แนวนอน", value="landscape"),
                                 app_commands.Choice(name="📱 แนวตั้ง", value="portrait")])
    async def imagine(self, interaction: discord.Interaction, prompt: app_commands.Range[str, 2, 500],
                      shape: app_commands.Choice[str] | None = None) -> None:
        if not await self._check_limit(interaction):
            return
        await interaction.response.defer(thinking=True)
        # Image models understand English prompts far better: let the LLM translate and enrich the request
        try:
            english = await self.one_shot(
                "Rewrite the user's image request as one vivid, detailed English prompt for an AI image generator "
                "(subject, style, lighting, composition). Keep it under 60 words. Reply with the prompt only.",
                prompt,
            )
            english = english.strip().strip('"')[:450]
        except LLMError:
            english = prompt
        width, height = IMAGE_SIZES[shape.value if shape else "square"]
        url = (f"https://image.pollinations.ai/prompt/{quote(english)}"
               f"?width={width}&height={height}&nologo=true&safe=true&seed={random.randint(1, 10**9)}")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150)) as session:
                async with session.get(url) as r:
                    data = await r.read() if r.status == 200 and r.content_type.startswith("image/") else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Image generation failed: %r", e)
            data = None
        if not data:
            await interaction.followup.send("😢 สร้างรูปไม่สำเร็จ (บริการฟรีอาจคนใช้เยอะ) ลองใหม่อีกครั้งนะ")
            return
        embed = discord.Embed(title=f"🎨 {prompt[:250]}", color=discord.Color.blurple())
        embed.set_image(url="attachment://imagine.jpg")
        embed.set_footer(text=f"วาดโดย Pollinations.ai • ขอโดย {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(data), filename="imagine.jpg"))

    # ---------- /persona ----------

    @persona.command(name="set", description="ตั้งบุคลิก/สไตล์การพูดของ AI ในเซิร์ฟเวอร์นี้")
    @app_commands.describe(text="เช่น 'พูดเหมือนแมวน้อย ลงท้ายด้วยเมี้ยว' หรือ 'สุภาพ ทางการ'")
    async def persona_set(self, interaction: discord.Interaction, text: app_commands.Range[str, 3, 1000]) -> None:
        await self.storage.set_setting(interaction.guild_id, "persona", text)
        await interaction.response.send_message(f"🎭 ตั้งบุคลิก AI แล้ว:\n> {text}")

    @persona.command(name="reset", description="กลับไปใช้บุคลิกเริ่มต้น")
    async def persona_reset(self, interaction: discord.Interaction) -> None:
        await self.storage.set_setting(interaction.guild_id, "persona", None)
        await interaction.response.send_message("🎭 กลับไปใช้บุคลิกเริ่มต้นแล้ว")

    @persona.command(name="show", description="ดูบุคลิก AI ปัจจุบัน")
    async def persona_show(self, interaction: discord.Interaction) -> None:
        current = await self.storage.get_setting(interaction.guild_id, "persona")
        text = current or f"(ค่าเริ่มต้น) {self.default_persona}"
        await interaction.response.send_message(f"🎭 บุคลิก AI ตอนนี้:\n> {text}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
