"""🎮 /quiz — guess-the-song game in a voice channel, with categories or your own YouTube playlist."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import discord
from discord import app_commands
from discord.ext import commands

from core import media
from core.lyrics import _variants, clean_title
from core.media import fmt_duration

log = logging.getLogger(__name__)

SNIPPET = 20  # seconds of music per round
FFMPEG_PATH = os.getenv("FFMPEG_PATH") or "ffmpeg"
NO_PINGS = discord.AllowedMentions.none()

CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "thai": ("🇹🇭 เพลงไทยฮิต", ["เพลงไทยฮิต official mv", "เพลงไทยใหม่ล่าสุด official mv", "เพลงไทย official mv 2025",
                               "เพลงไทยวัยรุ่น official mv"]),
    "thai_old": ("📼 เพลงไทยยุค 90–2000", ["เพลงไทยยุค 90 official mv", "เพลงไทยยุค 2000 official mv",
                                           "เพลงไทยเก่า แกรมมี่ official mv", "เพลงไทยยุค 2000 rs official mv"]),
    "lukthung": ("🌾 ลูกทุ่ง", ["ลูกทุ่ง official mv", "เพลงลูกทุ่งใหม่ official mv", "ลูกทุ่งอีสาน official mv"]),
    "kpop": ("🇰🇷 K-pop", ["kpop official mv", "kpop girl group official mv", "kpop boy group official mv",
                          "kpop m/v 2025"]),
    "jpop": ("🇯🇵 J-pop / อนิเมะ", ["jpop official music video", "anime song official music video",
                                   "J-POP MV 2025", "japanese band official music video"]),
    "english": ("🌎 เพลงสากลฮิต", ["pop hits official music video", "top pop songs official video 2025",
                                   "english pop song official music video"]),
    "english_old": ("💿 สากลยุค 2000", ["2000s pop hits official music video", "2000s hits official video",
                                         "2010s pop hits official music video"]),
}
CATEGORIES["mixed"] = ("🎲 รวมทุกหมวด", [q for _, qs in CATEGORIES.values() for q in qs])

# Things that are not a single, guessable song
_BAD = re.compile(
    r"mix|playlist|รวมเพลง|รวม\s|compilation|hour|ชั่วโมง|non-?stop|cover|karaoke|คาราโอเกะ|reaction|live|concert|"
    r"remix|medley|teaser|practice|behind|shorts|top ?\d+|\b8d\b|slowed|sped ?up|instrumental|เพลงฮิต|ฟังยาว|ต่อเนื่อง|"
    r"มันส์|ฟังสบาย|trailer|full album|episode",
    re.I,
)
_HAS_SEPARATOR = re.compile(r" [-–/] |['‘’\"“”「『]")  # we need to be able to tell the song name apart


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if ch.isalnum() or unicodedata.category(ch).startswith("M"))


@dataclass
class Song:
    title: str  # original video title
    url: str
    duration: float | None
    answer: str  # song name to guess
    artist: str | None
    thumbnail: str | None
    variants: list[str] = field(default_factory=list)  # normalized accepted answers

    @classmethod
    def from_entry(cls, entry: dict, strict: bool) -> Song | None:
        title, duration = entry.get("title") or "", entry.get("duration")
        if duration and not 90 <= duration <= 480:
            return None
        if strict and (not duration or _BAD.search(title) or not _HAS_SEPARATOR.search(title)):
            return None
        answer, artist = clean_title(title, entry.get("channel") or entry.get("uploader"))
        if not 2 <= len(normalize(answer)) <= 60:
            return None
        url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
        thumbs = entry.get("thumbnails") or [{}]
        variants = [normalize(v) for v in _variants(answer)]
        return cls(title, url, duration, answer, artist, entry.get("thumbnail") or thumbs[-1].get("url"),
                   [v for v in variants if v])

    def check(self, guess: str) -> bool:
        g = normalize(guess)
        if not g:
            return False
        for v in self.variants:
            if g == v or (len(v) >= 3 and v in g):  # exact (even 1-syllable answers like 춤) or contained
                return True
            if len(g) >= 3 and SequenceMatcher(None, g, v).ratio() >= 0.8:
                return True
        return False

    def hint(self) -> str:
        """First character of each word, the rest as blanks (Thai vowels/tone marks stay with their letter)."""
        out = []
        for word in self.answer.split():
            clusters: list[str] = []
            for ch in word:
                if clusters and unicodedata.category(ch).startswith("M"):
                    clusters[-1] += ch
                else:
                    clusters.append(ch)
            out.append(clusters[0] + " ".join("＿" for _ in clusters[1:]) if len(clusters) > 1 else clusters[0])
        return "   ".join(out)


def find_songs(category: str | None, playlist_url: str | None, count: int) -> list[Song]:
    """Blocking (yt-dlp): pick `count` random songs for the game."""
    if playlist_url:
        _, entries = media.extract_query(playlist_url)
        strict = False  # the user's own list: trust it, just skip things we can't name
    else:
        queries = CATEGORIES[category][1]
        entries = []
        for q in random.sample(queries, min(3, len(queries))):
            try:
                entries += media.search(q, 30)
            except Exception as e:
                log.warning("Quiz search failed for %r: %r", q, e)
        strict = True
    songs, seen = [], set()
    for entry in entries:
        song = Song.from_entry(entry, strict)
        if song and song.variants[0] not in seen:
            seen.add(song.variants[0])
            songs.append(song)
    random.shuffle(songs)
    return songs[:count]


class RoundView(discord.ui.View):
    def __init__(self, game: Game):
        super().__init__(timeout=SNIPPET + 30)
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.can_control(interaction.user):
            return True
        await interaction.response.send_message("เฉพาะคนเริ่มเกม หรือ DJ / แอดมิน ที่กดได้", ephemeral=True)
        return False

    @discord.ui.button(label="ข้ามเพลงนี้", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.game.skip_round.set()
        await interaction.response.defer()

    @discord.ui.button(label="จบเกม", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def end(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.game.stop()
        await interaction.response.defer()


class Game:
    def __init__(self, cog: Quiz, guild: discord.Guild, channel: discord.abc.Messageable,
                 voice_channel: discord.VoiceChannel, host: discord.Member, label: str, songs: list[Song]):
        self.cog = cog
        self.guild = guild
        self.channel = channel
        self.voice_channel = voice_channel
        self.host = host
        self.label = label
        self.songs = songs
        self.scores: Counter[int] = Counter()
        self.names: dict[int, str] = {}
        self.guesses: asyncio.Queue[discord.Message] = asyncio.Queue()
        self.skip_round = asyncio.Event()
        self.stopped = False
        self.task: asyncio.Task | None = None

    def can_control(self, member: discord.Member) -> bool:
        """The host, server managers or anyone with a role named DJ."""
        perms = member.guild_permissions
        return (member.id == self.host.id or perms.manage_guild or perms.manage_channels
                or any(r.name.lower() == "dj" for r in member.roles))

    def stop(self) -> None:
        self.stopped = True
        self.skip_round.set()

    async def _stream(self, song: Song) -> str | None:
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, media.extract_single, song.url)
            song.duration = song.duration or info.get("duration")
            song.thumbnail = info.get("thumbnail") or song.thumbnail
            return info["url"]
        except Exception as e:
            log.info("Quiz could not load %s: %r", song.url, e)
            return None

    async def run(self) -> None:
        music = self.cog.bot.get_cog("Music")
        volume = 0.5
        if music:
            saved = await music.storage.get_setting(self.guild.id, "volume")
            volume = int(saved) / 100 if saved else volume
        try:
            vc = self.guild.voice_client
            if vc is None:
                vc = await self.voice_channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != self.voice_channel:
                await vc.move_to(self.voice_channel)

            failures = 0
            next_stream = asyncio.create_task(self._stream(self.songs[0]))
            for i, song in enumerate(self.songs, 1):
                stream = await next_stream
                if i < len(self.songs):
                    next_stream = asyncio.create_task(self._stream(self.songs[i]))  # load the next one meanwhile
                if self.stopped:
                    break
                if not any(not m.bot for m in self.voice_channel.members):
                    await self.channel.send("👋 ไม่มีใครอยู่ในห้องเสียงแล้ว จบเกมก่อนนะ")
                    break
                if not stream:
                    failures += 1
                    if failures >= 3:
                        await self.channel.send("⚠️ YouTube ไม่ให้โหลดเพลงตอนนี้ จบเกมก่อนนะ ลองใหม่ภายหลัง")
                        break
                    continue
                failures = 0
                await self._round(vc, i, song, stream, volume)
                if not self.stopped and i < len(self.songs):
                    await asyncio.sleep(4)
            if not next_stream.done():
                next_stream.cancel()
            await self._finish()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Quiz crashed in guild %s", self.guild.id)
            await self.channel.send("💥 เกมมีปัญหา ขอจบเกมก่อนนะ")
        finally:
            self.cog.games.pop(self.guild.id, None)
            vc = self.guild.voice_client
            if vc:
                vc.stop()
                if not (music and self.guild.id in music.players):
                    await vc.disconnect(force=True)

    async def _round(self, vc: discord.VoiceClient, number: int, song: Song, stream: str, volume: float) -> None:
        duration = song.duration or 180
        start = random.uniform(0.25, 0.55) * duration
        start = max(10.0, min(start, duration - SNIPPET - 5))
        source = discord.FFmpegPCMAudio(
            stream, executable=FFMPEG_PATH,
            before_options=f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin -ss {start:.1f}",
            options=f"-vn -t {SNIPPET + 1}",
        )
        if vc.is_playing():
            vc.stop()
        vc.play(discord.PCMVolumeTransformer(source, volume=volume))

        embed = discord.Embed(
            title=f"🎵 รอบที่ {number}/{len(self.songs)} — เพลงนี้ชื่ออะไร?",
            description=f"พิมพ์ชื่อเพลงในแชทนี้ได้เลย ⏱️ **{SNIPPET} วินาที**",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"หมวด: {self.label}")
        message = await self.channel.send(embed=embed, view=RoundView(self))

        while not self.guesses.empty():  # ignore anything typed before the round started
            self.guesses.get_nowait()
        self.skip_round.clear()
        winner = await self._collect(song, message, embed)
        vc.stop()

        reveal = discord.Embed(
            title=f"✅ {winner.display_name} ตอบถูก! (+1)" if winner else "⏰ หมดเวลา!",
            description=f"เพลงคือ **[{song.answer}]({song.url})**" + (f"\nโดย {song.artist}" if song.artist else ""),
            color=discord.Color.green() if winner else discord.Color.red(),
        )
        if song.thumbnail:
            reveal.set_thumbnail(url=song.thumbnail)
        top = self.scores.most_common(3)
        if top:
            reveal.set_footer(text="คะแนน: " + "  •  ".join(f"{self.names[u]} {s}" for u, s in top))
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass
        await self.channel.send(embed=reveal)

    async def _collect(self, song: Song, message: discord.Message, embed: discord.Embed) -> discord.Member | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SNIPPET
        hint_at = loop.time() + SNIPPET / 2
        hinted = False
        while not self.skip_round.is_set():
            now = loop.time()
            if now >= deadline:
                return None
            if not hinted and now >= hint_at:
                hinted = True
                embed.description += f"\n\n💡 คำใบ้: `{song.hint()}`"
                try:
                    await message.edit(embed=embed)
                except discord.HTTPException:
                    pass
            wait = (hint_at if not hinted else deadline) - now
            get = asyncio.ensure_future(self.guesses.get())
            skip = asyncio.ensure_future(self.skip_round.wait())
            done, _ = await asyncio.wait({get, skip}, timeout=max(0.05, wait), return_when=asyncio.FIRST_COMPLETED)
            skip.cancel()
            if get not in done:
                get.cancel()
                continue
            guess = get.result()
            if song.check(guess.content):
                self.scores[guess.author.id] += 1
                self.names[guess.author.id] = guess.author.display_name
                try:
                    await guess.add_reaction("✅")
                except discord.HTTPException:
                    pass
                return guess.author
        return None

    async def _finish(self) -> None:
        embed = discord.Embed(title="🏆 จบเกมทายเพลง!", color=discord.Color.gold())
        if not self.scores:
            embed.description = "ไม่มีใครตอบถูกเลย 😅 ลองใหม่อีกรอบไหม?"
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} **{self.names[u]}** — {s} คะแนน"
                     for i, (u, s) in enumerate(self.scores.most_common(10))]
            embed.description = "\n".join(lines)
        embed.set_footer(text=f"หมวด: {self.label} • {len(self.songs)} รอบ")
        await self.channel.send(embed=embed)


class Quiz(commands.Cog):
    quiz = app_commands.Group(name="quiz", description="🎮 เกมทายเพลง", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, Game] = {}

    async def cog_unload(self) -> None:
        for game in list(self.games.values()):
            game.stop()
            if game.task:
                game.task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        game = self.games.get(message.guild.id)
        if game and message.channel.id == game.channel.id and message.content:
            game.guesses.put_nowait(message)

    @quiz.command(name="start", description="เริ่มเกมทายเพลง (ต้องอยู่ในห้องเสียง)")
    @app_commands.describe(category="หมวดเพลง", rounds="จำนวนรอบ (3-20)",
                           playlist="ลิงก์เพลย์ลิสต์ YouTube ของคุณเอง (ใส่แล้วจะใช้แทนหมวด)")
    @app_commands.choices(category=[app_commands.Choice(name=label, value=key) for key, (label, _) in CATEGORIES.items()])
    async def start(self, interaction: discord.Interaction, category: app_commands.Choice[str] | None = None,
                    rounds: app_commands.Range[int, 3, 20] = 10, playlist: str | None = None) -> None:
        member = interaction.user
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("เข้าห้องเสียงก่อนนะ แล้วค่อยเริ่มเกม 🎧", ephemeral=True)
            return
        if interaction.guild_id in self.games:
            await interaction.response.send_message("มีเกมกำลังเล่นอยู่แล้ว (จบเกมด้วย `/quiz stop`)", ephemeral=True)
            return
        music = self.bot.get_cog("Music")
        player = music.players.get(interaction.guild_id) if music else None
        if player and (player.current or player.queue):
            await interaction.response.send_message("ตอนนี้กำลังเปิดเพลงอยู่ หยุดเพลงก่อน (`/stop`) แล้วค่อยเริ่มเกมนะ", ephemeral=True)
            return
        if playlist and not playlist.startswith(("http://", "https://")):
            await interaction.response.send_message("ช่อง playlist ต้องเป็นลิงก์เพลย์ลิสต์ YouTube", ephemeral=True)
            return

        key = category.value if category else "mixed"
        label = "📃 เพลย์ลิสต์ของคุณ" if playlist else CATEGORIES[key][0]
        await interaction.response.defer(thinking=True)
        try:
            songs = await asyncio.get_running_loop().run_in_executor(None, find_songs, key, playlist, rounds)
        except Exception as e:
            log.warning("Quiz song lookup failed: %r", e)
            songs = []
        if len(songs) < 3:
            await interaction.followup.send("😢 หาเพลงสำหรับเกมไม่พอ ลองหมวดอื่น หรือเพลย์ลิสต์อื่นนะ")
            return
        if player:  # idle music player: hand the voice connection over to the game
            await music.destroy(interaction.guild)

        game = Game(self, interaction.guild, interaction.channel, member.voice.channel, member, label, songs)
        self.games[interaction.guild_id] = game
        game.task = asyncio.create_task(game.run())
        await interaction.followup.send(
            f"🎮 **เริ่มเกมทายเพลง!** หมวด **{label}** • {len(songs)} รอบ\n"
            f"ฟังเพลง {SNIPPET} วินาที แล้วพิมพ์ชื่อเพลงในแชทนี้ คนแรกที่ตอบถูกได้ 1 คะแนน (ครึ่งเวลามีคำใบ้)")

    @quiz.command(name="stop", description="จบเกมทายเพลง")
    async def stop(self, interaction: discord.Interaction) -> None:
        game = self.games.get(interaction.guild_id)
        if not game:
            await interaction.response.send_message("ไม่มีเกมที่กำลังเล่นอยู่", ephemeral=True)
            return
        if not game.can_control(interaction.user):
            await interaction.response.send_message("เฉพาะคนเริ่มเกม หรือ DJ / แอดมิน ที่จบเกมได้", ephemeral=True)
            return
        game.stop()
        await interaction.response.send_message("⏹️ จบเกมแล้ว กำลังสรุปคะแนน…")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quiz(bot))
