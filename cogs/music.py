"""Music player: /play from YouTube, YouTube Music, Spotify (matched on YouTube) or a plain song name,
with seek, audio filters, vote skip, autoplay, lyrics and saved playlists."""
from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import media
from core.card import render_card
from core.lyrics import find_lyrics
from core.media import Track, fmt_duration
from core.spotify import SpotifyError

log = logging.getLogger(__name__)

FFMPEG_PATH = os.getenv("FFMPEG_PATH") or "ffmpeg"
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT") or 180)
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME") or 50)
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
NO_PINGS = discord.AllowedMentions.none()
LOOP_LABELS = {"off": "ปิด", "track": "วนเพลงนี้", "queue": "วนทั้งคิว"}
MAX_SAVED_TRACKS = 500
MAX_PLAYLISTS_PER_USER = 25


@dataclass(frozen=True)
class AudioFilter:
    label: str
    af: str | None  # ffmpeg -af chain
    speed: float = 1.0  # playback speed, so the progress bar stays right


# Everything is resampled to 48 kHz first so asetrate's pitch/speed factor is the same for every source
FILTERS = {
    "off": AudioFilter("ปิด", None),
    "bassboost": AudioFilter("🔊 Bass Boost", "bass=g=10:f=110:w=0.6"),
    "nightcore": AudioFilter("⚡ Nightcore", "aresample=48000,asetrate=60000,aresample=48000", 1.25),
    "slowed": AudioFilter("🌙 Slowed + Reverb", "aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:60:0.35", 0.85),
    "vaporwave": AudioFilter("🌴 Vaporwave", "aresample=48000,asetrate=38400,aresample=48000", 0.8),
    "8d": AudioFilter("🎧 8D Audio", "apulsator=hz=0.09"),
}


class GuildPlayer:
    """Queue + playback loop for one server."""

    def __init__(self, cog: Music, guild: discord.Guild, channel: discord.abc.Messageable):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.channel = channel  # where "now playing" messages go
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.last_track: Track | None = None  # seed for autoplay
        self.volume = DEFAULT_VOLUME / 100
        self.loop_mode = "off"  # off | track | queue
        self.filter = "off"
        self.autoplay = False
        self.skip_votes: set[int] = set()
        self.played_ids: deque[str] = deque(maxlen=200)  # so autoplay doesn't repeat songs
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._skip = False
        self._restart_at: float | None = None  # replay the current track from here (seek / filter change)
        self._offset = 0.0  # track position the current ffmpeg process started at
        self._play_started = 0.0  # monotonic time it started, shifted forward by pauses
        self._paused_at: float | None = None
        self.task = asyncio.create_task(self._run())

    # ---------- controls ----------

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    @property
    def position(self) -> float:
        """Seconds played of the current track (in track time, so filters that change speed are accounted for)."""
        if not self.current:
            return 0.0
        elapsed = (self._paused_at or time.monotonic()) - self._play_started
        return self._offset + elapsed * FILTERS[self.filter].speed

    def pause(self) -> bool:
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        vc = self.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            if self._paused_at:
                self._play_started += time.monotonic() - self._paused_at
            self._paused_at = None
            return True
        return False

    def skip(self) -> None:
        self._skip = True
        self._restart_at = None
        self._stop_audio()

    def restart(self, at: float) -> None:
        """Play the current track again from `at` seconds (new ffmpeg process with -ss / new filter)."""
        if not self.current:
            return
        self._restart_at = max(0.0, at)
        self._stop_audio()

    def set_volume(self, volume: float) -> None:
        self.volume = volume
        vc = self.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

    def _stop_audio(self) -> None:
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    async def _send(self, content: str | None = None, **kwargs) -> None:
        try:
            await self.channel.send(content, allowed_mentions=NO_PINGS, **kwargs)
        except discord.HTTPException:
            pass

    # ---------- playback loop ----------

    async def _next_track(self) -> tuple[Track | None, float, bool]:
        """(track, start offset, is_restart_of_current)."""
        prev = self.current
        if prev and self._restart_at is not None:
            offset, self._restart_at = self._restart_at, None
            return prev, offset, True
        self.current = None
        skipped, self._skip = self._skip, False
        if prev:
            if self.loop_mode == "track" and not skipped:
                return prev, 0.0, False
            if self.loop_mode == "queue":
                self.queue.append(prev)
        if not self.queue and self.autoplay and self.last_track:
            await self._autoplay_fill()
        while not self.queue:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                return None, 0.0, False
        return self.queue.pop(0), 0.0, False

    async def _autoplay_fill(self) -> None:
        seed = self.last_track
        if not seed or not seed.video_id:
            return
        try:
            infos = await asyncio.get_running_loop().run_in_executor(
                None, media.related, seed.video_id, set(self.played_ids))
        except Exception as e:
            log.warning("Autoplay lookup failed: %r", e)
            return
        tracks = [Track.from_info(i, self.guild.me, resolved=False) for i in infos]
        if tracks:
            self.queue.extend(tracks)
            await self._send(f"🔄 **Autoplay:** เพิ่ม {len(tracks)} เพลงที่คล้ายกับ **{seed.title[:80]}**")

    def _after(self, error: Exception | None) -> None:
        if error:
            log.error("Player error in %s: %r", self.guild.id, error)
        self.bot.loop.call_soon_threadsafe(self._done.set)

    def _source(self, track: Track, offset: float) -> discord.AudioSource:
        before = FFMPEG_BEFORE + (f" -ss {offset:.2f}" if offset > 0 else "")
        af = FILTERS[self.filter].af
        options = "-vn" + (f' -af "{af}"' if af else "")
        audio = discord.FFmpegPCMAudio(track.stream_url, executable=FFMPEG_PATH, before_options=before, options=options)
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    async def _run(self) -> None:
        last_announced: Track | None = None
        failures = 0
        try:
            while True:
                track, offset, is_restart = await self._next_track()
                if track is None:
                    await self._send("💤 ไม่มีเพลงในคิวแล้ว ขอออกจากห้องเสียงก่อนนะ")
                    return
                vc = self.guild.voice_client
                if vc is None or not vc.is_connected():
                    return
                try:
                    await track.resolve()
                    failures = 0
                except Exception as e:
                    log.warning("Failed to resolve %s: %r", track.title, e)
                    failures += 1
                    err = str(e)
                    if "not a bot" in err or failures >= 3:
                        # YouTube is blocking this server's IP: don't burn through the whole queue
                        self.queue.clear()
                        await self._send(
                            "⚠️ YouTube บล็อกการดึงเพลงชั่วคราว (`Sign in to confirm you're not a bot`) "
                            "จึงล้างคิวไว้ก่อน — ลองใหม่ภายหลัง หรือให้แอดมินตั้งค่า YTDL_COOKIES (ดู README)"
                        )
                        failures = 0
                    else:
                        await self._send(f"⚠️ เล่น **{track.title}** ไม่ได้ ข้ามไปเพลงถัดไป\n`{err[:200]}`")
                    continue

                self.current = track
                self._done.clear()
                vc.play(self._source(track, offset), after=self._after)
                self._offset, self._play_started, self._paused_at = offset, time.monotonic(), None
                if not is_restart:
                    self.skip_votes.clear()
                    self.last_track = track
                    if track.video_id:
                        self.played_ids.append(track.video_id)
                    if track is not last_announced:
                        await self._send(**await self.cog.now_playing_message(self))
                        last_announced = track
                await self._done.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed in guild %s", self.guild.id)
        finally:
            if self.cog.players.get(self.guild.id) is self:
                asyncio.create_task(self.cog.destroy(self.guild))


class NowPlayingView(discord.ui.View):
    def __init__(self, cog: Music):
        super().__init__(timeout=3600)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.get_player(interaction) is not None

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players[interaction.guild_id]
        if player.resume():
            text = "▶️ เล่นต่อ"
        elif player.pause():
            text = "⏸️ หยุดชั่วคราว"
        else:
            await interaction.response.send_message("ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)
            return
        await interaction.response.send_message(f"{text} โดย {interaction.user.display_name}", allowed_mentions=NO_PINGS)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_skip(interaction, self.cog.players[interaction.guild_id])

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players[interaction.guild_id]
        player.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[player.loop_mode]
        await interaction.response.send_message(f"🔁 โหมดวนซ้ำ: **{LOOP_LABELS[player.loop_mode]}**")

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def autoplay(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.set_autoplay(interaction, None)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.require_dj(interaction):
            return
        await interaction.response.send_message(f"⏹️ หยุดเล่นและออกจากห้อง โดย {interaction.user.display_name}", allowed_mentions=NO_PINGS)
        await self.cog.destroy(interaction.guild)


class QueueView(discord.ui.View):
    """/queue with ⏮️ ◀️ [page] ▶️ ⏭️ buttons. Re-reads the live queue on every click."""

    PER_PAGE = 10

    def __init__(self, cog: Music, guild_id: int, page: int = 1):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.page = page
        self.message: discord.Message | None = None

    def render(self, player: GuildPlayer | None) -> discord.Embed:
        queue = player.queue if player else []
        pages = max(1, -(-len(queue) // self.PER_PAGE))
        self.page = max(1, min(self.page, pages))
        start = (self.page - 1) * self.PER_PAGE

        lines = []
        for i, t in enumerate(queue[start:start + self.PER_PAGE], start=start + 1):
            title = t.title if len(t.title) <= 60 else t.title[:59] + "…"
            title = title.replace("[", "(").replace("]", ")")
            lines.append(f"`{i:>2}.` [{title}]({t.url})  `{fmt_duration(t.duration)}`" if t.url else f"`{i:>2}.` {title}")
        current = player.current if player else None
        now = f"🎶 **กำลังเล่น:** {current.link}\n\n" if current else ""
        embed = discord.Embed(
            title="📜 คิวเพลง",
            description=(now + ("\n".join(lines) or "_ไม่มีเพลงถัดไป_"))[:4000],
            color=discord.Color.blurple(),
        )
        total = sum(t.duration or 0 for t in queue)
        loop = LOOP_LABELS[player.loop_mode] if player else "-"
        embed.set_footer(text=f"หน้า {self.page}/{pages} • {len(queue)} เพลง • รวม ~{fmt_duration(total)} • วนซ้ำ: {loop}")

        self.first.disabled = self.prev.disabled = self.page <= 1
        self.next.disabled = self.last.disabled = self.page >= pages
        self.counter.label = f"{self.page}/{pages}"
        return embed

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        self.page = page
        await interaction.response.edit_message(embed=self.render(self.cog.players.get(self.guild_id)), view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, 1)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, self.page - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, self.page)  # acts as "refresh"

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, self.page + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, 10**6)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PagedView(discord.ui.View):
    """◀️ [i/n] ▶️ over a fixed list of embeds (used by /lyrics)."""

    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=600)
        self.pages = pages
        self.index = 0
        self.message: discord.Message | None = None
        self._refresh()

    def _refresh(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, self.index + 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _chunk_lines(text: str, limit: int = 1800) -> list[str]:
    pages, current = [], ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit and current:
            pages.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        pages.append(current)
    return pages or [text[:limit]]


class Music(commands.Cog):
    playlist = app_commands.Group(name="playlist", description="เพลย์ลิสต์ส่วนตัวของคุณ", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = bot.storage
        self.players: dict[int, GuildPlayer] = {}
        self._alone_tasks: dict[int, asyncio.Task] = {}
        self._http: aiohttp.ClientSession | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("คำสั่งเพลงใช้ได้ในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
            return False
        return True

    async def cog_unload(self) -> None:
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.destroy(guild)
        if self._http and not self._http.closed:
            await self._http.close()

    # ---------- helpers ----------

    async def destroy(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player:
            player.queue.clear()
            if player.task is not asyncio.current_task():
                player.task.cancel()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def get_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """Return the player if the user is allowed to control it, otherwise reply with why not."""
        player = self.players.get(interaction.guild_id)
        vc = interaction.guild.voice_client
        if not player or not vc:
            await interaction.response.send_message("ตอนนี้ไม่ได้เล่นเพลงอยู่", ephemeral=True)
            return None
        voice = getattr(interaction.user, "voice", None)
        if not voice or voice.channel != vc.channel:
            await interaction.response.send_message(f"ต้องอยู่ในห้อง {vc.channel.mention} ก่อนถึงจะควบคุมเพลงได้", ephemeral=True)
            return None
        return player

    @staticmethod
    def is_dj(member: discord.Member, vc: discord.VoiceClient | None) -> bool:
        """Role named "DJ", server/channel managers, or anyone alone with the bot."""
        perms = member.guild_permissions
        if perms.manage_guild or perms.manage_channels or any(r.name.lower() == "dj" for r in member.roles):
            return True
        listeners = [m for m in vc.channel.members if not m.bot] if vc else []
        return len(listeners) <= 1

    async def require_dj(self, interaction: discord.Interaction) -> bool:
        if self.is_dj(interaction.user, interaction.guild.voice_client):
            return True
        await interaction.response.send_message(
            "🎧 คำสั่งนี้ใช้ได้เฉพาะคนที่มียศ **DJ**, แอดมิน หรือตอนที่อยู่ในห้องคนเดียว", ephemeral=True)
        return False

    async def handle_skip(self, interaction: discord.Interaction, player: GuildPlayer) -> None:
        if not player.current:
            await interaction.response.send_message("ไม่มีเพลงให้ข้าม", ephemeral=True)
            return
        member, vc = interaction.user, interaction.guild.voice_client
        title = player.current.title
        if self.is_dj(member, vc) or player.current.requester.id == member.id:
            player.skip()
            await interaction.response.send_message(f"⏭️ ข้าม **{title}** โดย {member.display_name}", allowed_mentions=NO_PINGS)
            return
        listeners = {m.id for m in vc.channel.members if not m.bot}
        player.skip_votes &= listeners
        player.skip_votes.add(member.id)
        needed = math.ceil(len(listeners) / 2)
        if len(player.skip_votes) >= needed:
            player.skip()
            await interaction.response.send_message(f"🗳️ โหวตครบ {needed} คน — ข้าม **{title}** แล้ว")
        else:
            await interaction.response.send_message(
                f"🗳️ {member.display_name} โหวตข้ามเพลง ({len(player.skip_votes)}/{needed}) — กด ⏭️ หรือ `/skip` เพื่อโหวต",
                allowed_mentions=NO_PINGS)

    async def set_autoplay(self, interaction: discord.Interaction, enabled: bool | None) -> None:
        player = self.players.get(interaction.guild_id)
        current = player.autoplay if player else (await self.storage.get_setting(interaction.guild_id, "autoplay")) == "1"
        enabled = (not current) if enabled is None else enabled
        await self.storage.set_setting(interaction.guild_id, "autoplay", "1" if enabled else "0")
        if player:
            player.autoplay = enabled
            if enabled and not player.queue and not player.current and player.last_track:
                player._wake.set()  # idle right now: let the loop pick related songs immediately
        text = "🔄 เปิด Autoplay แล้ว — พอคิวหมดจะหาเพลงแนวเดียวกันมาเล่นต่อ" if enabled else "🔄 ปิด Autoplay แล้ว"
        await interaction.response.send_message(text)

    async def _join(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """Connect / move to the user's voice channel (after defer) and return the player."""
        voice = interaction.user.voice
        vc = interaction.guild.voice_client
        try:
            if vc is None:
                await voice.channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != voice.channel:
                listeners = [m for m in vc.channel.members if not m.bot]
                if vc.is_playing() and listeners:
                    await interaction.followup.send(f"บอทกำลังเปิดเพลงอยู่ในห้อง {vc.channel.mention} นะ")
                    return None
                await vc.move_to(voice.channel)
        except Exception as e:
            log.warning("Voice connect failed: %r", e)
            await interaction.followup.send("❌ เชื่อมต่อห้องเสียงไม่ได้ ลองใหม่อีกครั้ง")
            return None
        player = self.players.get(interaction.guild_id)
        if player is None:
            player = self.players[interaction.guild_id] = GuildPlayer(self, interaction.guild, interaction.channel)
            player.autoplay = (await self.storage.get_setting(interaction.guild_id, "autoplay")) == "1"
        return player

    @staticmethod
    async def _require_voice(interaction: discord.Interaction) -> bool:
        voice = getattr(interaction.user, "voice", None)
        if voice and voice.channel:
            return True
        await interaction.response.send_message("เข้าห้องเสียงก่อนนะ แล้วค่อยสั่งเปิดเพลง 🎧", ephemeral=True)
        return False

    async def _fetch_image(self, url: str | None) -> bytes | None:
        if not url:
            return None
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self._http.get(url) as r:
                if r.status == 200 and int(r.headers.get("content-length") or 0) < 5_000_000:
                    return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.info("Thumbnail fetch failed: %r", e)
        return None

    async def now_playing_message(self, player: GuildPlayer, position: float = 0.0) -> dict:
        """Kwargs for channel.send / followup.send: an embed with a rendered banner image (falls back to a plain embed)."""
        t = player.current
        embed = discord.Embed(description=f"🎶 **{t.link}**", color=discord.Color.blurple())
        footer = []
        if player.queue:
            footer.append(f"ถัดไป: {player.queue[0].title[:70]}")
        if player.loop_mode != "off":
            footer.append(f"🔁 {LOOP_LABELS[player.loop_mode]}")
        if player.filter != "off":
            footer.append(FILTERS[player.filter].label)
        if player.autoplay:
            footer.append("🔄 Autoplay")
        if footer:
            embed.set_footer(text="  •  ".join(footer))
        kwargs: dict = {"embed": embed, "view": NowPlayingView(self)}

        card_footer = f"ขอโดย {t.requester.display_name}" + (f" • คิว {len(player.queue)} เพลง" if player.queue else "")
        try:
            thumb = await self._fetch_image(t.thumbnail)
            png, accent = await asyncio.get_running_loop().run_in_executor(
                None, render_card, thumb, t.card_title, t.artist, t.duration, position, card_footer
            )
            embed.color = discord.Color.from_rgb(*accent)
            embed.set_image(url="attachment://nowplaying.png")
            kwargs["file"] = discord.File(io.BytesIO(png), filename="nowplaying.png")
        except Exception:
            log.exception("Could not render now-playing card")
            embed.add_field(name="ความยาว", value=fmt_duration(t.duration))
            embed.add_field(name="ขอโดย", value=t.requester.mention)
            if t.thumbnail:
                embed.set_thumbnail(url=t.thumbnail)
        return kwargs

    # ---------- playback commands ----------

    @app_commands.command(name="play", description="เปิดเพลงจาก YouTube / YouTube Music / Spotify หรือพิมพ์ชื่อเพลง")
    @app_commands.describe(query="ลิงก์เพลง/เพลย์ลิสต์ หรือชื่อเพลงที่ต้องการค้นหา")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer(thinking=True)
        player = await self._join(interaction)
        if not player:
            return
        try:
            playlist, tracks = await media.load(query, interaction.user)
        except SpotifyError as e:
            await interaction.followup.send(f"❌ {e}")
            return
        except Exception as e:
            log.warning("Load failed for %r: %r", query, e)
            await interaction.followup.send(f"❌ หาเพลงไม่เจอหรือเปิดลิงก์ไม่ได้\n`{str(e)[:300]}`")
            return

        was_idle = player.current is None and not player.queue
        player.add(tracks)
        if playlist:
            await interaction.followup.send(f"📃 เพิ่ม **{len(tracks)}** เพลงจาก **{playlist}** ลงคิวแล้ว")
        elif was_idle:
            await interaction.followup.send(f"🎵 กำลังเปิด **{tracks[0].link}**")
        else:
            await interaction.followup.send(f"➕ เพิ่ม **{tracks[0].link}** ลงคิว (ลำดับที่ {len(player.queue)})")

    @app_commands.command(name="pause", description="หยุดเพลงชั่วคราว")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if player.pause():
            await interaction.response.send_message("⏸️ หยุดชั่วคราวแล้ว")
        else:
            await interaction.response.send_message("ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)

    @app_commands.command(name="resume", description="เล่นเพลงต่อ")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if player.resume():
            await interaction.response.send_message("▶️ เล่นต่อแล้ว")
        else:
            await interaction.response.send_message("เพลงไม่ได้หยุดอยู่", ephemeral=True)

    @app_commands.command(name="skip", description="ข้ามเพลง (DJ / คนขอเพลงข้ามได้ทันที คนอื่นต้องโหวต)")
    async def skip(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if player:
            await self.handle_skip(interaction, player)

    @app_commands.command(name="stop", description="หยุดเพลง ล้างคิว และออกจากห้องเสียง (DJ)")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self.get_player(interaction) or not await self.require_dj(interaction):
            return
        await interaction.response.send_message("⏹️ หยุดเล่นและออกจากห้องแล้ว บ๊ายบาย 👋")
        await self.destroy(interaction.guild)

    @app_commands.command(name="leave", description="ให้บอทออกจากห้องเสียง (DJ)")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild.voice_client is None:
            await interaction.response.send_message("บอทไม่ได้อยู่ในห้องเสียง", ephemeral=True)
            return
        if not await self.require_dj(interaction):
            return
        await interaction.response.send_message("👋 ออกจากห้องเสียงแล้ว")
        await self.destroy(interaction.guild)

    @app_commands.command(name="seek", description="กระโดดไปยังตำแหน่งในเพลง เช่น 1:30")
    @app_commands.describe(position="ตำแหน่ง เช่น 90, 1:30 หรือ 1:02:03")
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        seconds = media.parse_time(position)
        track = player.current
        if not track:
            await interaction.response.send_message("ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)
        elif seconds is None:
            await interaction.response.send_message("รูปแบบเวลาไม่ถูกต้อง ใช้แบบ `90`, `1:30` หรือ `1:02:03`", ephemeral=True)
        elif not track.duration:
            await interaction.response.send_message("เพลงนี้เป็นไลฟ์สด เลื่อนตำแหน่งไม่ได้", ephemeral=True)
        elif seconds >= track.duration:
            await interaction.response.send_message(f"เพลงนี้ยาวแค่ {fmt_duration(track.duration)}", ephemeral=True)
        else:
            player.restart(seconds)
            await interaction.response.send_message(f"⏩ ไปที่ **{fmt_duration(seconds) if seconds else '0:00'}**")

    @app_commands.command(name="filter", description="ใส่เอฟเฟกต์เสียง")
    @app_commands.describe(effect="เอฟเฟกต์ที่ต้องการ")
    @app_commands.choices(effect=[app_commands.Choice(name=f.label, value=key) for key, f in FILTERS.items()])
    async def filter(self, interaction: discord.Interaction, effect: app_commands.Choice[str]) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        position = player.position
        player.filter = effect.value
        if player.current:
            player.restart(position)
        await interaction.response.send_message(
            "🎛️ ปิดเอฟเฟกต์เสียงแล้ว" if effect.value == "off" else f"🎛️ ใส่เอฟเฟกต์ **{effect.name}** แล้ว")

    @app_commands.command(name="autoplay", description="เปิด/ปิดการหาเพลงแนวเดียวกันมาเล่นต่อเมื่อคิวหมด")
    @app_commands.describe(enabled="เปิดหรือปิด (เว้นว่าง = สลับ)")
    async def autoplay(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        await self.set_autoplay(interaction, enabled)

    @app_commands.command(name="lyrics", description="ดูเนื้อเพลงที่กำลังเล่น หรือค้นหาเนื้อเพลงอื่น")
    @app_commands.describe(song="ชื่อเพลง (เว้นว่าง = เพลงที่กำลังเล่น) เช่น 'BABYMONSTER CHOOM'")
    async def lyrics(self, interaction: discord.Interaction, song: str | None = None) -> None:
        player = self.players.get(interaction.guild_id)
        if song:
            title, artist, duration = song, None, None
        elif player and player.current:
            t = player.current
            title, artist, duration = t.title, t.artist, t.duration
        else:
            await interaction.response.send_message("ไม่มีเพลงที่กำลังเล่น ใส่ชื่อเพลงที่อยากดูเนื้อได้เลย เช่น `/lyrics song:...`", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await find_lyrics(title, artist, duration)
        if not result:
            await interaction.followup.send(f"😢 ไม่พบเนื้อเพลง **{title[:100]}** — ลองพิมพ์ชื่อเพลงเองด้วย `/lyrics song:ศิลปิน ชื่อเพลง`")
            return
        chunks = _chunk_lines(result.text)
        pages = []
        for i, chunk in enumerate(chunks, 1):
            e = discord.Embed(title=f"🎤 {result.artist} - {result.title}"[:256], description=chunk, color=discord.Color.blurple())
            e.set_footer(text=f"หน้า {i}/{len(chunks)} • เนื้อเพลงจาก lrclib.net")
            pages.append(e)
        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0])
        else:
            view = PagedView(pages)
            view.message = await interaction.followup.send(embed=pages[0], view=view, wait=True)

    # ---------- queue commands ----------

    @app_commands.command(name="queue", description="ดูคิวเพลง")
    @app_commands.describe(page="หน้าที่ต้องการดู")
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
        player = self.players.get(interaction.guild_id)
        if not player or (not player.current and not player.queue):
            await interaction.response.send_message("คิวว่างอยู่ ใช้ /play เพื่อเพิ่มเพลง", ephemeral=True)
            return
        view = QueueView(self, interaction.guild_id, page)
        await interaction.response.send_message(embed=view.render(player), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="nowplaying", description="ดูเพลงที่กำลังเล่น")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if not player or not player.current:
            await interaction.response.send_message("ตอนนี้ไม่ได้เล่นเพลงอยู่", ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.followup.send(**await self.now_playing_message(player, position=player.position))

    @app_commands.command(name="volume", description="ปรับระดับเสียง (0-150)")
    @app_commands.describe(level="ระดับเสียง 0-150")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        player.set_volume(level / 100)
        await interaction.response.send_message(f"🔊 ตั้งระดับเสียงเป็น **{level}%**")

    @app_commands.command(name="loop", description="ตั้งโหมดวนซ้ำ")
    @app_commands.describe(mode="โหมดวนซ้ำ")
    @app_commands.choices(mode=[app_commands.Choice(name=label, value=key) for key, label in LOOP_LABELS.items()])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        player.loop_mode = mode.value
        await interaction.response.send_message(f"🔁 โหมดวนซ้ำ: **{mode.name}**")

    @app_commands.command(name="shuffle", description="สลับลำดับเพลงในคิว")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        random.shuffle(player.queue)
        await interaction.response.send_message(f"🔀 สลับลำดับ {len(player.queue)} เพลงในคิวแล้ว")

    @app_commands.command(name="remove", description="ลบเพลงออกจากคิวตามลำดับ")
    @app_commands.describe(position="ลำดับเพลงในคิว (ดูได้จาก /queue)")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if position > len(player.queue):
            await interaction.response.send_message(f"ในคิวมีแค่ {len(player.queue)} เพลง", ephemeral=True)
            return
        track = player.queue[position - 1]
        if track.requester.id != interaction.user.id and not self.is_dj(interaction.user, interaction.guild.voice_client):
            await interaction.response.send_message("ลบได้เฉพาะเพลงที่คุณขอเอง (หรือต้องเป็น DJ)", ephemeral=True)
            return
        player.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ ลบ **{track.title}** ออกจากคิวแล้ว")

    @app_commands.command(name="clear", description="ล้างคิวเพลงทั้งหมด เพลงที่เล่นอยู่ยังเล่นต่อ (DJ)")
    async def clear(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player or not await self.require_dj(interaction):
            return
        count = len(player.queue)
        player.queue.clear()
        await interaction.response.send_message(f"🧹 ล้างคิว {count} เพลงแล้ว")

    # ---------- /playlist ----------

    async def _playlist_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = await self.storage.list_playlists(interaction.user.id)
        return [app_commands.Choice(name=f"{n} ({c} เพลง)"[:100], value=n)
                for n, c in names if current.lower() in n.lower()][:25]

    @playlist.command(name="save", description="บันทึกเพลงที่เล่นอยู่ + คิวทั้งหมดเป็นเพลย์ลิสต์ของคุณ")
    @app_commands.describe(name="ชื่อเพลย์ลิสต์ (ถ้าชื่อซ้ำจะเขียนทับ)")
    async def playlist_save(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]) -> None:
        player = self.players.get(interaction.guild_id)
        tracks = ([player.current] if player and player.current else []) + (player.queue if player else [])
        if not tracks:
            await interaction.response.send_message("ยังไม่มีเพลงให้บันทึก เปิดเพลงด้วย /play ก่อนนะ", ephemeral=True)
            return
        name = name.strip()
        existing = await self.storage.list_playlists(interaction.user.id)
        if len(existing) >= MAX_PLAYLISTS_PER_USER and name not in {n for n, _ in existing}:
            await interaction.response.send_message(f"มีเพลย์ลิสต์ครบ {MAX_PLAYLISTS_PER_USER} อันแล้ว ลบอันเก่าด้วย `/playlist delete` ก่อนนะ", ephemeral=True)
            return
        await self.storage.save_playlist(interaction.user.id, name, [t.to_dict() for t in tracks[:MAX_SAVED_TRACKS]])
        await interaction.response.send_message(f"💾 บันทึกเพลย์ลิสต์ **{name}** ({min(len(tracks), MAX_SAVED_TRACKS)} เพลง) แล้ว")

    @playlist.command(name="load", description="เปิดเพลย์ลิสต์ที่บันทึกไว้ (เพิ่มเข้าคิว)")
    @app_commands.describe(name="ชื่อเพลย์ลิสต์", shuffle="สุ่มลำดับเพลงด้วยไหม")
    async def playlist_load(self, interaction: discord.Interaction, name: str, shuffle: bool = False) -> None:
        data = await self.storage.get_playlist(interaction.user.id, name)
        if not data:
            await interaction.response.send_message(f"ไม่พบเพลย์ลิสต์ **{name}** ดูรายชื่อได้ด้วย `/playlist list`", ephemeral=True)
            return
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer(thinking=True)
        player = await self._join(interaction)
        if not player:
            return
        tracks = [Track.from_dict(d, interaction.user) for d in data]
        if shuffle:
            random.shuffle(tracks)
        player.add(tracks)
        await interaction.followup.send(f"📂 เพิ่ม **{len(tracks)}** เพลงจากเพลย์ลิสต์ **{name}** ลงคิวแล้ว{' (สุ่มลำดับ)' if shuffle else ''}")

    @playlist.command(name="list", description="ดูเพลย์ลิสต์ที่คุณบันทึกไว้")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        names = await self.storage.list_playlists(interaction.user.id)
        if not names:
            await interaction.response.send_message("ยังไม่มีเพลย์ลิสต์ บันทึกได้ด้วย `/playlist save`", ephemeral=True)
            return
        lines = "\n".join(f"• **{n}** — {c} เพลง" for n, c in names)
        await interaction.response.send_message(f"📂 **เพลย์ลิสต์ของคุณ**\n{lines}", ephemeral=True)

    @playlist.command(name="delete", description="ลบเพลย์ลิสต์ที่บันทึกไว้")
    @app_commands.describe(name="ชื่อเพลย์ลิสต์")
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        if await self.storage.delete_playlist(interaction.user.id, name):
            await interaction.response.send_message(f"🗑️ ลบเพลย์ลิสต์ **{name}** แล้ว", ephemeral=True)
        else:
            await interaction.response.send_message(f"ไม่พบเพลย์ลิสต์ **{name}**", ephemeral=True)

    @playlist_load.autocomplete("name")
    async def _load_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._playlist_names(interaction, current)

    @playlist_delete.autocomplete("name")
    async def _delete_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._playlist_names(interaction, current)

    # ---------- auto leave ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        guild = member.guild
        if member.id == self.bot.user.id:
            # Bot was kicked/disconnected from voice by someone
            if after.channel is None and guild.id in self.players:
                await self.destroy(guild)
            return
        vc = guild.voice_client
        if vc and not any(not m.bot for m in vc.channel.members) and guild.id not in self._alone_tasks:
            self._alone_tasks[guild.id] = asyncio.create_task(self._leave_if_alone(guild))

    async def _leave_if_alone(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(60)
            vc = guild.voice_client
            if vc and not any(not m.bot for m in vc.channel.members):
                player = self.players.get(guild.id)
                if player:
                    await player._send("👋 ไม่มีใครอยู่ในห้องเสียงแล้ว ขอออกก่อนนะ")
                await self.destroy(guild)
        finally:
            self._alone_tasks.pop(guild.id, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
