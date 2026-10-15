"""Music player: /play from YouTube, YouTube Music, Spotify (matched on YouTube) or a plain song name,
with seek, audio filters, vote skip, autoplay, lyrics and saved playlists."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import media
from core.card import render_card
from core.llm import LLMError
from core.lyrics import find_lyrics
from core.media import Track, fmt_duration
from core.spotify import SpotifyError
from core.tts import TTSError, synthesize

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
        self.voice_channel_id: int | None = guild.voice_client.channel.id if guild.voice_client else None
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.last_track: Track | None = None  # seed for autoplay
        self.history: deque[Track] = deque(maxlen=50)  # tracks that started playing, newest last
        self.volume = DEFAULT_VOLUME / 100
        self.loop_mode = "off"  # off | track | queue
        self.filter = "off"
        self.autoplay = False
        self.skip_votes: set[int] = set()
        self.played_ids: deque[str] = deque(maxlen=200)  # so autoplay doesn't repeat songs
        self.leaving = False  # set by Music.destroy: an intentional disconnect, don't reconnect
        self._last_reconnect = 0.0
        self._interrupts: deque[bytes] = deque()  # TTS clips to play before continuing the music
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._skip = False
        self._jump = False  # like skip, but never re-queue / repeat the current track (previous, skipto)
        self._restart_at: float | None = None  # replay the current track from here (seek / filter / TTS / reconnect)
        self._resume_offset: float | None = None  # start the next track here (restored after a bot restart)
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

    def jump(self) -> None:
        """Move on to queue[0] right away, without loop-mode repeats (used by previous / skipto)."""
        self._jump = True
        self._restart_at = None
        self._stop_audio()
        self._wake.set()

    def play_previous(self) -> Track | None:
        # history[-1] is the current track (if one is playing), history[-2] the one before it
        older = list(self.history)[:-1] if self.current else list(self.history)
        if not older:
            return None
        prev = older[-1]
        if self.current:
            self.queue.insert(0, self.current)
            self.history.pop()
        self.history.pop() if self.history and self.history[-1] is prev else None
        self.queue.insert(0, prev)
        self.jump()
        return prev

    def restart(self, at: float) -> None:
        """Play the current track again from `at` seconds (new ffmpeg process with -ss / new filter)."""
        if not self.current:
            return
        self._restart_at = max(0.0, at)
        self._stop_audio()

    def speak(self, audio: bytes) -> None:
        """Play a TTS clip now. Music in progress is paused and continues from the same spot afterwards."""
        self._interrupts.append(audio)
        vc = self.guild.voice_client
        if self.current and vc and (vc.is_playing() or vc.is_paused()):
            self.restart(self.position)
        else:
            self._wake.set()

    def set_volume(self, volume: float) -> None:
        self.volume = volume
        vc = self.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

    def _stop_audio(self) -> None:
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def state(self) -> dict | None:
        """Snapshot saved to the database so playback can continue after a bot restart."""
        if not self.current and not self.queue:
            return None

        def dump(t: Track) -> dict:
            return {**t.to_dict(), "requester_id": t.requester.id}

        return {
            "voice": self.voice_channel_id,
            "text": getattr(self.channel, "id", None),
            "current": dump(self.current) if self.current else None,
            "position": self.position,
            "queue": [dump(t) for t in self.queue[:MAX_SAVED_TRACKS]],
            "loop": self.loop_mode,
            "filter": self.filter,
        }

    async def _send(self, content: str | None = None, **kwargs) -> None:
        try:
            await self.channel.send(content, allowed_mentions=NO_PINGS, **kwargs)
        except discord.HTTPException:
            pass

    # ---------- playback loop ----------

    async def _next_track(self) -> tuple[Track | None, float, bool]:
        """(track, start offset, is_restart_of_current). Track is None when idle for too long."""
        prev = self.current
        if prev and self._restart_at is not None:
            offset, self._restart_at = self._restart_at, None
            return prev, offset, True
        self.current = None
        skipped, self._skip = self._skip, False
        jumped, self._jump = self._jump, False
        if prev and not jumped:
            if self.loop_mode == "track" and not skipped:
                return prev, 0.0, False
            if self.loop_mode == "queue":
                self.queue.append(prev)
        if not self.queue and self.autoplay and self.last_track:
            await self._autoplay_fill()
        while not self.queue:
            if self._interrupts:
                return _INTERRUPT, 0.0, False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                return None, 0.0, False
        offset, self._resume_offset = self._resume_offset or 0.0, None
        return self.queue.pop(0), offset, False

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

    async def _play_interrupts(self) -> None:
        while self._interrupts:
            clip = self._interrupts.popleft()
            vc = self.guild.voice_client
            if not vc or not vc.is_connected():
                self._interrupts.clear()
                return
            self._done.clear()
            source = discord.FFmpegPCMAudio(io.BytesIO(clip), pipe=True, executable=FFMPEG_PATH, options="-vn")
            vc.play(discord.PCMVolumeTransformer(source, volume=1.0), after=self._after)
            await self._done.wait()

    def _voice_lost(self) -> bool:
        vc = self.guild.voice_client
        return not self.leaving and (vc is None or not vc.is_connected())

    async def _reconnect(self) -> bool:
        """The voice connection dropped (network hiccup, Discord voice server change...): get back in."""
        if time.monotonic() - self._last_reconnect < 120:
            return False  # dropped again right after reconnecting: probably kicked on purpose
        self._last_reconnect = time.monotonic()
        await self._send("🔌 หลุดจากห้องเสียง กำลังเชื่อมต่อใหม่…")
        for attempt in range(3):
            await asyncio.sleep(3 + attempt * 5)
            if self.leaving:
                return False
            vc = self.guild.voice_client
            if vc and vc.is_connected():
                return True  # discord.py reconnected by itself
            channel = self.guild.get_channel(self.voice_channel_id) if self.voice_channel_id else None
            if not channel or not any(not m.bot for m in channel.members):
                return False
            try:
                if vc:
                    await vc.disconnect(force=True)
                await channel.connect(self_deaf=True, timeout=20)
                return True
            except Exception as e:
                log.warning("Voice reconnect attempt %d failed: %r", attempt + 1, e)
        return False

    async def _run(self) -> None:
        last_announced: Track | None = None
        failures = 0
        try:
            while True:
                await self._play_interrupts()
                track, offset, is_restart = await self._next_track()
                if track is _INTERRUPT:
                    continue
                if track is None:
                    await self._send("💤 ไม่มีเพลงในคิวแล้ว ขอออกจากห้องเสียงก่อนนะ")
                    return
                vc = self.guild.voice_client
                if vc is None or not vc.is_connected():
                    if self.leaving or not await self._reconnect():
                        return
                    vc = self.guild.voice_client
                try:
                    await track.resolve()
                    failures = 0
                except Exception as e:
                    log.warning("Failed to resolve %s: %r", track.title, e)
                    failures += 1
                    err = str(e)
                    blocked = "not a bot" in err
                    if failures >= 3:
                        # Several songs in a row failed: YouTube is probably blocking us, don't burn through the queue
                        self.queue.clear()
                        await self._send(
                            "⚠️ เปิดเพลงไม่ได้ติดกัน 3 เพลง"
                            + (" (YouTube ขอให้ยืนยันว่าไม่ใช่บอท)" if blocked else "")
                            + " จึงล้างคิวไว้ก่อน — ลองใหม่ภายหลัง หรือให้แอดมินตั้งค่า YTDL_COOKIES (ดู README)"
                        )
                        await self.cog.notify("youtube-block", f"⚠️ เปิดเพลงไม่ได้ติดกันในเซิร์ฟเวอร์ **{self.guild.name}**\n`{err[:300]}`")
                        failures = 0
                    elif blocked:
                        await self._send(f"⚠️ YouTube ไม่ให้เปิด **{track.title}** (ขอให้ยืนยันว่าไม่ใช่บอท) ข้ามไปเพลงถัดไป")
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
                    if not self.history or self.history[-1] is not track:
                        self.history.append(track)
                    if track.video_id:
                        self.played_ids.append(track.video_id)
                    if track is not last_announced:
                        await self._send(**await self.cog.now_playing_message(self, position=offset))
                        last_announced = track
                await self._done.wait()

                # The stream died right away (YouTube sometimes answers 403 for a stream URL, or it expired):
                # fetch a fresh URL and try the song once more instead of silently skipping it
                played = time.monotonic() - self._play_started
                if (played < 3 and not self._skip and not self._jump and self._restart_at is None
                        and not self._voice_lost() and (track.duration or 0) > 15 and not track.retried):
                    log.info("Stream for %s ended after %.1fs, retrying with a fresh URL", track.title, played)
                    track.retried = True
                    track.stream_url = None
                    self._restart_at = offset
                    continue

                if self._voice_lost() and self.current:
                    position = self.position
                    if await self._reconnect():
                        self._restart_at = position
                        await self._send(f"✅ กลับมาแล้ว เล่นต่อจาก {fmt_duration(position)}")
                    else:
                        await self._send("❌ เชื่อมต่อห้องเสียงใหม่ไม่ได้ ใช้ /play เพื่อเริ่มใหม่ได้เลย")
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed in guild %s", self.guild.id)
            await self.cog.notify("player-crash", f"💥 ระบบเพลงล่มในเซิร์ฟเวอร์ **{self.guild.name}** (ดู logs/bot.log)")
        finally:
            if self.cog.players.get(self.guild.id) is self:
                asyncio.create_task(self.cog.destroy(self.guild, forget=not self._voice_lost()))


_INTERRUPT = Track(title="<interrupt>", requester=None)  # sentinel returned by _next_track when only TTS is pending


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


class TrackSelectView(discord.ui.View):
    """A dropdown of tracks; picking one adds it to the queue (used by /search and /history)."""

    def __init__(self, cog: Music, owner_id: int, tracks: list[Track], placeholder: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.tracks = tracks[:25]
        self.message: discord.Message | None = None
        self.select = discord.ui.Select(placeholder=placeholder, options=[
            discord.SelectOption(
                label=f"{i}. {t.title}"[:100], value=str(i - 1),
                description=" • ".join(x for x in (t.artist, fmt_duration(t.duration) if t.duration else None) if x)[:100] or None,
            )
            for i, t in enumerate(self.tracks, 1)
        ])
        self.select.callback = self._picked
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("เมนูนี้เป็นของคนที่สั่งคำสั่ง ลองสั่งเองได้เลย", ephemeral=True)
        return False

    async def _picked(self, interaction: discord.Interaction) -> None:
        chosen = self.tracks[int(self.select.values[0])]
        track = Track.from_dict(chosen.to_dict(), interaction.user)
        await interaction.response.defer()
        player, error = await self.cog.connect(interaction.user, interaction.channel)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        was_idle = player.current is None and not player.queue
        player.add([track])
        self.select.disabled = True
        self.select.placeholder = f"✅ {track.title}"[:150]
        await interaction.edit_original_response(view=self)
        text = f"🎵 กำลังเปิด **{track.link}**" if was_idle else f"➕ เพิ่ม **{track.link}** ลงคิว (ลำดับที่ {len(player.queue)})"
        await interaction.followup.send(text, allowed_mentions=NO_PINGS)
        self.stop()

    async def on_timeout(self) -> None:
        self.select.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


DJ_PROMPT = (
    "คุณคือดีเจที่รู้จักเพลงเยอะมาก ผู้ใช้จะบอกแนว อารมณ์ ศิลปิน หรือโอกาสที่อยากฟัง "
    "ให้เลือกเพลงที่มีอยู่จริงและหาฟังได้บน YouTube {count} เพลง ห้ามแต่งชื่อเพลงขึ้นเอง "
    "เลือกเฉพาะเพลงที่ดังและคุณมั่นใจว่าชื่อเพลงกับศิลปินถูกต้อง ถ้าไม่แน่ใจให้เลือกเพลงที่ดังกว่าแทน "
    "ถ้าผู้ใช้ระบุภาษา/ประเทศ/ยุค ให้เลือกตามนั้น ถ้าไม่ระบุให้เลือกตามภาษาที่ผู้ใช้พิมพ์มา\n"
    "ตอบเป็น JSON object อย่างเดียว ห้ามมีข้อความอื่น รูปแบบ:\n"
    '{{"reply": "ข้อความสั้นๆ 1 ประโยค ภาษาเดียวกับผู้ใช้ แบบดีเจเป็นกันเอง", "songs": ["ศิลปิน - ชื่อเพลง", ...]}}'
)
_FRESH = re.compile(r"ล่าสุด|ใหม่|ตอนนี้|ปีนี้|20\d\d|25\d\d|new|latest|trending", re.I)


class Music(commands.Cog):
    playlist = app_commands.Group(name="playlist", description="เพลย์ลิสต์ส่วนตัวของคุณ", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = bot.storage
        self.players: dict[int, GuildPlayer] = {}
        self._alone_tasks: dict[int, asyncio.Task] = {}
        self._http: aiohttp.ClientSession | None = None
        self._restored = False

    async def cog_load(self) -> None:
        self._save_states.start()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("คำสั่งเพลงใช้ได้ในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
            return False
        return True

    async def cog_unload(self) -> None:
        self._save_states.cancel()
        await self._save_states()  # keep the queues so they come back after a restart
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.destroy(guild, forget=False)
        if self._http and not self._http.closed:
            await self._http.close()

    # ---------- helpers ----------

    async def destroy(self, guild: discord.Guild, forget: bool = True) -> None:
        """Stop and leave. forget=False keeps the saved queue (used on shutdown so a restart resumes it)."""
        player = self.players.pop(guild.id, None)
        if player:
            player.leaving = True
            player.queue.clear()
            if player.task is not asyncio.current_task():
                player.task.cancel()
        if forget:
            await self.storage.set_setting(guild.id, "player_state", None)
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def notify(self, key: str, text: str) -> None:
        notify = getattr(self.bot, "notify_owner", None)
        if notify:
            await notify(key, text)

    def quiz_running(self, guild_id: int) -> bool:
        quiz = self.bot.get_cog("Quiz")
        return bool(quiz and guild_id in quiz.games)

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

    async def _new_player(self, guild: discord.Guild, text_channel: discord.abc.Messageable) -> GuildPlayer:
        player = self.players[guild.id] = GuildPlayer(self, guild, text_channel)
        player.autoplay = (await self.storage.get_setting(guild.id, "autoplay")) == "1"
        volume = await self.storage.get_setting(guild.id, "volume")
        if volume:
            player.volume = int(volume) / 100
        return player

    async def connect(self, member: discord.Member, text_channel: discord.abc.Messageable) -> tuple[GuildPlayer | None, str | None]:
        """Join / move to the member's voice channel. Returns (player, None) or (None, reason it failed)."""
        voice = member.voice
        guild = member.guild
        if not voice or not voice.channel:
            return None, "เข้าห้องเสียงก่อนนะ แล้วค่อยสั่งเปิดเพลง 🎧"
        if self.quiz_running(guild.id):
            return None, "🎮 กำลังเล่นเกมทายเพลงอยู่ รอจบเกมก่อนนะ (หรือ `/quiz stop`)"
        vc = guild.voice_client
        try:
            if vc is None:
                await voice.channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != voice.channel:
                listeners = [m for m in vc.channel.members if not m.bot]
                if vc.is_playing() and listeners:
                    return None, f"บอทกำลังเปิดเพลงอยู่ในห้อง {vc.channel.mention} นะ"
                await vc.move_to(voice.channel)
        except Exception as e:
            log.warning("Voice connect failed: %r", e)
            return None, "❌ เชื่อมต่อห้องเสียงไม่ได้ ลองใหม่อีกครั้ง"
        player = self.players.get(guild.id) or await self._new_player(guild, text_channel)
        player.voice_channel_id = voice.channel.id
        return player, None

    async def _join(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """connect() for slash commands (after defer): reports problems as a followup."""
        player, error = await self.connect(interaction.user, interaction.channel)
        if error:
            await interaction.followup.send(error)
        return player

    @staticmethod
    async def _require_voice(interaction: discord.Interaction) -> bool:
        voice = getattr(interaction.user, "voice", None)
        if voice and voice.channel:
            return True
        await interaction.response.send_message("เข้าห้องเสียงก่อนนะ แล้วค่อยสั่งเปิดเพลง 🎧", ephemeral=True)
        return False

    # ---------- used by other cogs (AI chat) ----------

    async def speak_text(self, member: discord.Member, text_channel: discord.abc.Messageable, text: str,
                         gender: str = "female") -> str | None:
        """Read text aloud in the member's voice channel. Returns an error message, or None on success."""
        try:
            audio = await synthesize(text, gender)
        except TTSError as e:
            return f"❌ {e}"
        player, error = await self.connect(member, text_channel)
        if error:
            return error
        player.speak(audio)
        return None

    async def dj_request(self, member: discord.Member, text_channel: discord.abc.Messageable,
                         prompt: str, count: int = 8) -> str:
        """AI picks songs for a free-text request and queues them. Returns the reply text (raises LLMError)."""
        chat = self.bot.get_cog("Chat")
        if chat is None:
            return "ระบบ AI ยังไม่พร้อม"
        if not member.voice or not member.voice.channel:
            return "เข้าห้องเสียงก่อนนะ แล้วค่อยขอเพลง 🎧"
        reply = await chat.llm.chat(
            [{"role": "system", "content": DJ_PROMPT.format(count=count)}, {"role": "user", "content": prompt}],
            allow_search=bool(_FRESH.search(prompt)),
        )
        try:
            data = json.loads(reply.text[reply.text.index("{"): reply.text.rindex("}") + 1])
        except ValueError:
            log.warning("DJ reply was not JSON: %r", reply.text[:300])
            return "🤔 เลือกเพลงไม่สำเร็จ ลองบอกแนวเพลงใหม่อีกทีนะ"
        songs = [s.strip() for s in data.get("songs", []) if isinstance(s, str) and s.strip()][:count]
        if not songs:
            return "🤔 เลือกเพลงไม่สำเร็จ ลองบอกแนวเพลงใหม่อีกทีนะ"
        player, error = await self.connect(member, text_channel)
        if error:
            return error
        player.add([Track.from_search(s, member) for s in songs])
        lines = "\n".join(f"`{i}.` {s}" for i, s in enumerate(songs, 1))
        return f"🎧 {data.get('reply') or 'จัดให้!'}\n{lines}"

    # ---------- save / restore queues across restarts ----------

    @tasks.loop(seconds=15)
    async def _save_states(self) -> None:
        for guild_id, player in list(self.players.items()):
            try:
                state = player.state()
                await self.storage.set_setting(guild_id, "player_state",
                                               json.dumps(state, ensure_ascii=False) if state else None)
            except Exception:
                log.exception("Could not save player state for %s", guild_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        self._restored = True
        for guild_id, raw in await self.storage.all_settings("player_state"):
            try:
                await self._restore(guild_id, json.loads(raw))
            except Exception:
                log.exception("Could not restore player for guild %s", guild_id)
                await self.storage.set_setting(guild_id, "player_state", None)

    async def _restore(self, guild_id: int, state: dict) -> None:
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(state.get("voice") or 0) if guild else None
        if not channel or not any(not m.bot for m in channel.members) or guild_id in self.players:
            await self.storage.set_setting(guild_id, "player_state", None)  # nobody left to listen
            return
        text = guild.get_channel(state.get("text") or 0) or channel

        def load(d: dict) -> Track:
            return Track.from_dict(d, guild.get_member(d.get("requester_id") or 0) or guild.me)

        tracks = ([load(state["current"])] if state.get("current") else []) + [load(d) for d in state.get("queue", [])]
        if not tracks:
            return
        await channel.connect(self_deaf=True, timeout=20)
        player = await self._new_player(guild, text)
        player.voice_channel_id = channel.id
        player.loop_mode = state.get("loop") if state.get("loop") in LOOP_LABELS else "off"
        player.filter = state.get("filter") if state.get("filter") in FILTERS else "off"
        position = max(0.0, float(state.get("position") or 0) - 2) if state.get("current") else 0.0
        player._resume_offset = position or None
        player.add(tracks)
        log.info("Restored %d tracks in guild %s", len(tracks), guild_id)
        await player._send(f"♻️ บอทเพิ่งรีสตาร์ต — กลับมาเล่นต่อจาก **{tracks[0].title[:80]}**"
                           + (f" ที่ {fmt_duration(position)}" if position else "")
                           + (f" (+ คิวอีก {len(tracks) - 1} เพลง)" if len(tracks) > 1 else ""))

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
        await self.storage.set_setting(interaction.guild_id, "volume", str(level))
        await interaction.response.send_message(f"🔊 ตั้งระดับเสียงเป็น **{level}%** (จำไว้สำหรับเซิร์ฟเวอร์นี้)")

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

    @app_commands.command(name="search", description="ค้นหาเพลงบน YouTube แล้วเลือกเองจาก 5 อันดับแรก")
    @app_commands.describe(query="ชื่อเพลง / ศิลปินที่ต้องการค้นหา")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            entries = await asyncio.get_running_loop().run_in_executor(None, media.search, query, 5)
        except Exception as e:
            log.warning("Search failed for %r: %r", query, e)
            entries = []
        if not entries:
            await interaction.followup.send(f"😢 ไม่พบผลการค้นหา **{query[:100]}**")
            return
        tracks = [Track.from_info(e, interaction.user, resolved=False) for e in entries]
        lines = "\n".join(
            f"`{i}.` [{t.title[:70]}]({t.url}) `{fmt_duration(t.duration)}`" + (f"\n-# {t.artist}" if t.artist else "")
            for i, t in enumerate(tracks, 1)
        )
        embed = discord.Embed(title=f"🔎 ผลการค้นหา: {query[:200]}", description=lines, color=discord.Color.blurple())
        if tracks[0].thumbnail:
            embed.set_thumbnail(url=tracks[0].thumbnail)
        view = TrackSelectView(self, interaction.user.id, tracks, "เลือกเพลงที่จะเปิด…")
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    @app_commands.command(name="skipto", description="ข้ามไปเล่นเพลงลำดับที่ระบุในคิวเลย (DJ)")
    @app_commands.describe(position="ลำดับเพลงในคิว (ดูได้จาก /queue)")
    async def skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
        player = await self.get_player(interaction)
        if not player or not await self.require_dj(interaction):
            return
        if position > len(player.queue):
            await interaction.response.send_message(f"ในคิวมีแค่ {len(player.queue)} เพลง", ephemeral=True)
            return
        del player.queue[:position - 1]
        title = player.queue[0].title
        player.jump()
        await interaction.response.send_message(f"⏩ ข้ามไปเล่น **{title}**" + (f" (ข้ามไป {position - 1} เพลง)" if position > 1 else ""))

    @app_commands.command(name="move", description="ย้ายเพลงในคิวไปลำดับใหม่")
    @app_commands.describe(position="ลำดับเพลงที่จะย้าย", to="ย้ายไปไว้ลำดับที่")
    async def move(self, interaction: discord.Interaction, position: app_commands.Range[int, 1], to: app_commands.Range[int, 1]) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if position > len(player.queue):
            await interaction.response.send_message(f"ในคิวมีแค่ {len(player.queue)} เพลง", ephemeral=True)
            return
        track = player.queue[position - 1]
        if track.requester.id != interaction.user.id and not self.is_dj(interaction.user, interaction.guild.voice_client):
            await interaction.response.send_message("ย้ายได้เฉพาะเพลงที่คุณขอเอง (หรือต้องเป็น DJ)", ephemeral=True)
            return
        player.queue.pop(position - 1)
        to = min(to, len(player.queue) + 1)
        player.queue.insert(to - 1, track)
        await interaction.response.send_message(f"↕️ ย้าย **{track.title}** ไปลำดับที่ {to} แล้ว")

    @app_commands.command(name="previous", description="กลับไปเล่นเพลงก่อนหน้า")
    async def previous(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        current = player.current
        if current and current.requester.id != interaction.user.id and not self.is_dj(interaction.user, interaction.guild.voice_client):
            await interaction.response.send_message("ย้อนเพลงได้เฉพาะคนที่ขอเพลงนี้ หรือ DJ", ephemeral=True)
            return
        prev = player.play_previous()
        if not prev:
            await interaction.response.send_message("ยังไม่มีเพลงก่อนหน้า", ephemeral=True)
            return
        await interaction.response.send_message(f"⏮️ กลับไปเล่น **{prev.title}**")

    @app_commands.command(name="replay", description="เล่นเพลงนี้ใหม่ตั้งแต่ต้น")
    async def replay(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if not player.current:
            await interaction.response.send_message("ไม่มีเพลงที่กำลังเล่นอยู่", ephemeral=True)
            return
        player.restart(0)
        await interaction.response.send_message(f"🔂 เล่น **{player.current.title}** ใหม่ตั้งแต่ต้น")

    @app_commands.command(name="history", description="ดูเพลงที่เล่นไปล่าสุด แล้วเลือกเปิดซ้ำได้")
    async def history(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        played = list(reversed(player.history))[:20] if player else []
        if not played:
            await interaction.response.send_message("ยังไม่มีประวัติเพลงในรอบนี้", ephemeral=True)
            return
        lines = "\n".join(
            f"`{i}.` [{t.title[:70]}]({t.url})" + (" 🎶 *กำลังเล่น*" if t is player.current else "")
            if t.url else f"`{i}.` {t.title[:70]}"
            for i, t in enumerate(played, 1)
        )
        embed = discord.Embed(title="🕘 เพลงที่เล่นไปล่าสุด", description=lines[:4000], color=discord.Color.blurple())
        view = TrackSelectView(self, interaction.user.id, played, "เลือกเพลงที่จะเปิดซ้ำ…")
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="dj", description="ให้ AI เลือกเพลงตามที่ขอแล้วเปิดให้ เช่น 'เพลงเศร้าๆ ยุค 2000'")
    @app_commands.describe(request="อยากฟังเพลงแบบไหน", count="จำนวนเพลง (3-15)")
    async def dj(self, interaction: discord.Interaction, request: str, count: app_commands.Range[int, 3, 15] = 8) -> None:
        if not await self._require_voice(interaction):
            return
        chat = self.bot.get_cog("Chat")
        if chat and not await chat._check_limit(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            text = await self.dj_request(interaction.user, interaction.channel, request, count)
        except LLMError as e:
            log.error("DJ LLM error: %s", e)
            text = "ขอโทษครับ ตอนนี้ AI เลือกเพลงไม่ได้ (อาจโดน rate limit) ลองใหม่อีกครั้งนะ 🙏"
        await interaction.followup.send(f"> **{interaction.user.display_name}:** {request[:200]}\n{text}", allowed_mentions=NO_PINGS)

    @app_commands.command(name="say", description="ให้บอทพูดข้อความในห้องเสียง")
    @app_commands.describe(text="ข้อความที่จะให้พูด", voice="เสียงผู้หญิงหรือผู้ชาย")
    @app_commands.choices(voice=[app_commands.Choice(name="👩 ผู้หญิง", value="female"),
                                 app_commands.Choice(name="👨 ผู้ชาย", value="male")])
    async def say(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 500],
                  voice: app_commands.Choice[str] | None = None) -> None:
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer(thinking=True)
        error = await self.speak_text(interaction.user, interaction.channel, text, voice.value if voice else "female")
        await interaction.followup.send(error or f"🔊 **{interaction.user.display_name}** ให้บอทพูดว่า: {text[:300]}",
                                        allowed_mentions=NO_PINGS)

    @app_commands.command(name="talk", description="เปิด/ปิดโหมดให้บอทอ่านคำตอบ AI ออกเสียงในห้องเสียง")
    @app_commands.describe(enabled="เปิดหรือปิด", voice="เสียงผู้หญิงหรือผู้ชาย")
    @app_commands.choices(voice=[app_commands.Choice(name="👩 ผู้หญิง", value="female"),
                                 app_commands.Choice(name="👨 ผู้ชาย", value="male")])
    async def talk(self, interaction: discord.Interaction, enabled: bool, voice: app_commands.Choice[str] | None = None) -> None:
        if not await self.require_dj(interaction):
            return
        await self.storage.set_setting(interaction.guild_id, "talk", (voice.value if voice else "female") if enabled else None)
        await interaction.response.send_message(
            "🗣️ เปิดโหมดพูดแล้ว — เวลาคนที่อยู่ในห้องเสียง @บอท บอทจะอ่านคำตอบออกเสียงด้วย" if enabled
            else "🤐 ปิดโหมดพูดแล้ว")


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
            player = self.players.get(guild.id)
            if not player or player.leaving:
                return
            if after.channel is not None:
                player.voice_channel_id = after.channel.id  # moved to another channel
            elif not player.current:
                await self.destroy(guild)  # disconnected while idle: just clean up
            # disconnected mid-song: the player loop notices and tries to reconnect (network drops)
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
