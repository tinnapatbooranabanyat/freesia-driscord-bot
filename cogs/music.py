"""Music player: /play from YouTube, YouTube Music, Spotify (matched on YouTube) or a plain song name."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from core.card import render_card
from core.spotify import SpotifyError, fetch_spotify, is_spotify_url

log = logging.getLogger(__name__)

FFMPEG_PATH = os.getenv("FFMPEG_PATH") or "ffmpeg"
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT") or 180)
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME") or 50)
MAX_PLAYLIST = 200
MAX_MIX = 50  # YouTube Mixes (list=RD...) are endless; take the first songs only
STREAM_URL_TTL = 3 * 60 * 60  # YouTube stream URLs expire after ~6h; re-resolve well before that
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}
NO_PINGS = discord.AllowedMentions.none()


def _ytdl_opts(**extra) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "source_address": "0.0.0.0",
    }
    cookies = os.getenv("YTDL_COOKIES")
    if cookies and os.path.isfile(cookies):
        opts["cookiefile"] = cookies
    opts.update(extra)
    return opts


def _extract_single(query: str) -> dict:
    """Fully resolve one video (URL or 'ytsearch1:...'), including the direct audio stream URL."""
    with yt_dlp.YoutubeDL(_ytdl_opts()) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        info = next((e for e in info["entries"] if e), None)
    if not info or not info.get("url"):
        raise LookupError("ไม่พบเพลง")
    return info


def _extract_query(query: str) -> tuple[str | None, list[dict]]:
    """Return (playlist title or None, infos). Playlist entries are extracted 'flat' (fast, no stream URL).

    A watch link that also carries a playlist / Mix ("...watch?v=X&list=PL..." or "&list=RD...") queues the
    whole list, starting from the video in the link.
    """
    if not query.startswith(("http://", "https://")):
        return None, [_extract_single(f"ytsearch1:{query}")]
    url = urlparse(query)
    params = parse_qs(url.query)
    list_id = params.get("list", [None])[0]
    video_id = params.get("v", [None])[0]
    if not video_id and url.netloc.endswith("youtu.be"):
        video_id = url.path.strip("/") or None

    limit = MAX_MIX if (list_id or "").startswith("RD") else MAX_PLAYLIST
    opts = _ytdl_opts(extract_flat="in_playlist", noplaylist=not list_id, playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except yt_dlp.utils.DownloadError:
        # Private / personal lists (Liked videos, Watch later...) can't be read: just play the video itself
        if not (list_id and video_id):
            raise
        log.info("Could not load list %s, playing video %s only", list_id, video_id)
        return None, [_extract_single(f"https://www.youtube.com/watch?v={video_id}")]

    if info and "entries" in info:
        entries = [e for e in info["entries"] if e][:limit]
        if video_id:
            start = next((i for i, e in enumerate(entries) if e.get("id") == video_id), 0)
            entries = entries[start:] + entries[:start]
        if not entries:
            raise LookupError("เพลย์ลิสต์นี้ว่างหรือเปิดไม่ได้")
        return info.get("title") or "Playlist", entries
    if not info or not info.get("url"):
        raise LookupError("ไม่พบเพลง")
    return None, [info]


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "LIVE/?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@dataclass
class Track:
    title: str
    requester: discord.abc.User
    url: str | None = None  # YouTube / YT Music page URL
    search: str | None = None  # search text when no URL yet (Spotify)
    duration: float | None = None
    thumbnail: str | None = None
    artist: str | None = None
    stream_url: str | None = None
    resolved_at: float = 0.0

    @staticmethod
    def _artist(info: dict) -> str | None:
        return info.get("artist") or info.get("uploader") or info.get("channel")

    @classmethod
    def from_info(cls, info: dict, requester: discord.abc.User, resolved: bool) -> Track:
        t = cls(
            title=info.get("title") or "Unknown",
            requester=requester,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail") or ((info.get("thumbnails") or [{}])[-1]).get("url"),
            artist=cls._artist(info),
        )
        if resolved:
            t.url = info.get("webpage_url")
            t.stream_url = info.get("url")
            t.resolved_at = time.time()
        else:  # flat playlist entry: "url" is the page URL
            t.url = info.get("url") or info.get("webpage_url")
            if not t.url and info.get("id"):
                t.url = f"https://www.youtube.com/watch?v={info['id']}"
        return t

    @property
    def link(self) -> str:
        return f"[{self.title}]({self.url})" if self.url else self.title

    @property
    def card_title(self) -> str:
        """Title without a leading "Artist - " (Spotify tracks), since the card shows the artist separately."""
        if self.artist and self.title.startswith(f"{self.artist} - "):
            return self.title[len(self.artist) + 3:]
        return self.title

    async def resolve(self) -> None:
        if self.stream_url and time.time() - self.resolved_at < STREAM_URL_TTL:
            return
        query = self.url or f"ytsearch1:{self.search}"
        info = await asyncio.get_running_loop().run_in_executor(None, _extract_single, query)
        self.stream_url = info["url"]
        self.resolved_at = time.time()
        self.url = info.get("webpage_url") or self.url
        self.duration = info.get("duration") or self.duration
        self.thumbnail = info.get("thumbnail") or self.thumbnail
        self.artist = self.artist or self._artist(info)
        if not self.search:  # keep the nicer "Artist - Title" name for Spotify tracks
            self.title = info.get("title") or self.title


class GuildPlayer:
    """Queue + playback loop for one server."""

    def __init__(self, cog: Music, guild: discord.Guild, channel: discord.abc.Messageable):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.channel = channel  # where "now playing" messages go
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.volume = DEFAULT_VOLUME / 100
        self.loop_mode = "off"  # off | track | queue
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._skip = False
        self._started_at = 0.0  # monotonic time the current track started, shifted forward by pauses
        self._paused_at: float | None = None
        self.task = asyncio.create_task(self._run())

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    @property
    def position(self) -> float:
        """Seconds played of the current track."""
        if not self.current:
            return 0.0
        return (self._paused_at or time.monotonic()) - self._started_at

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
                self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None
            return True
        return False

    def skip(self) -> None:
        self._skip = True
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def set_volume(self, volume: float) -> None:
        self.volume = volume
        vc = self.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

    async def _send(self, content: str | None = None, **kwargs) -> None:
        try:
            await self.channel.send(content, allowed_mentions=NO_PINGS, **kwargs)
        except discord.HTTPException:
            pass

    async def _next_track(self) -> Track | None:
        prev, self.current = self.current, None
        skipped, self._skip = self._skip, False
        if prev:
            if self.loop_mode == "track" and not skipped:
                return prev
            if self.loop_mode == "queue":
                self.queue.append(prev)
        while not self.queue:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                return None
        return self.queue.pop(0)

    def _after(self, error: Exception | None) -> None:
        if error:
            log.error("Player error in %s: %r", self.guild.id, error)
        self.bot.loop.call_soon_threadsafe(self._done.set)

    async def _run(self) -> None:
        last_announced: Track | None = None
        failures = 0
        try:
            while True:
                track = await self._next_track()
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

                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track.stream_url, executable=FFMPEG_PATH, **FFMPEG_OPTS),
                    volume=self.volume,
                )
                self.current = track
                self._done.clear()
                vc.play(source, after=self._after)
                self._started_at, self._paused_at = time.monotonic(), None
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
        self.cog.players[interaction.guild_id].skip()
        await interaction.response.send_message(f"⏭️ ข้ามเพลง โดย {interaction.user.display_name}", allowed_mentions=NO_PINGS)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players[interaction.guild_id]
        player.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[player.loop_mode]
        await interaction.response.send_message(f"🔁 โหมดวนซ้ำ: **{LOOP_LABELS[player.loop_mode]}**")

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
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


LOOP_LABELS = {"off": "ปิด", "track": "วนเพลงนี้", "queue": "วนทั้งคิว"}


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
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

    async def _load(self, query: str, requester: discord.abc.User) -> tuple[str | None, list[Track]]:
        if is_spotify_url(query):
            name, songs = await fetch_spotify(query)
            tracks = [
                Track(title=s, search=s, requester=requester, artist=s.split(" - ", 1)[0] if " - " in s else None)
                for s in songs[:MAX_PLAYLIST]
            ]
            return (name if "/track/" not in query else None), tracks
        loop = asyncio.get_running_loop()
        playlist, infos = await loop.run_in_executor(None, _extract_query, query)
        tracks = [Track.from_info(i, requester, resolved=playlist is None) for i in infos]
        if not tracks:
            raise LookupError("ไม่พบเพลง")
        return playlist, tracks

    # ---------- commands ----------

    @app_commands.command(name="play", description="เปิดเพลงจาก YouTube / YouTube Music / Spotify หรือพิมพ์ชื่อเพลง")
    @app_commands.describe(query="ลิงก์เพลง/เพลย์ลิสต์ หรือชื่อเพลงที่ต้องการค้นหา")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.response.send_message("เข้าห้องเสียงก่อนนะ แล้วค่อยสั่ง /play 🎧", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        vc = interaction.guild.voice_client
        try:
            if vc is None:
                await voice.channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != voice.channel:
                listeners = [m for m in vc.channel.members if not m.bot]
                if vc.is_playing() and listeners:
                    await interaction.followup.send(f"บอทกำลังเปิดเพลงอยู่ในห้อง {vc.channel.mention} นะ")
                    return
                await vc.move_to(voice.channel)
        except Exception as e:
            log.warning("Voice connect failed: %r", e)
            await interaction.followup.send("❌ เชื่อมต่อห้องเสียงไม่ได้ ลองใหม่อีกครั้ง")
            return

        player = self.players.get(interaction.guild_id)
        if player is None:
            player = self.players[interaction.guild_id] = GuildPlayer(self, interaction.guild, interaction.channel)

        try:
            playlist, tracks = await self._load(query, interaction.user)
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

    @app_commands.command(name="skip", description="ข้ามเพลงปัจจุบัน")
    async def skip(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        if not player.current:
            await interaction.response.send_message("ไม่มีเพลงให้ข้าม", ephemeral=True)
            return
        title = player.current.title
        player.skip()
        await interaction.response.send_message(f"⏭️ ข้าม **{title}**")

    @app_commands.command(name="stop", description="หยุดเพลง ล้างคิว และออกจากห้องเสียง")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self.get_player(interaction):
            return
        await interaction.response.send_message("⏹️ หยุดเล่นและออกจากห้องแล้ว บ๊ายบาย 👋")
        await self.destroy(interaction.guild)

    @app_commands.command(name="leave", description="ให้บอทออกจากห้องเสียง")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild.voice_client is None:
            await interaction.response.send_message("บอทไม่ได้อยู่ในห้องเสียง", ephemeral=True)
            return
        await interaction.response.send_message("👋 ออกจากห้องเสียงแล้ว")
        await self.destroy(interaction.guild)

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
    @app_commands.choices(mode=[
        app_commands.Choice(name="ปิด", value="off"),
        app_commands.Choice(name="วนเพลงนี้", value="track"),
        app_commands.Choice(name="วนทั้งคิว", value="queue"),
    ])
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
        track = player.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ ลบ **{track.title}** ออกจากคิวแล้ว")

    @app_commands.command(name="clear", description="ล้างคิวเพลงทั้งหมด (เพลงที่เล่นอยู่ยังเล่นต่อ)")
    async def clear(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction)
        if not player:
            return
        count = len(player.queue)
        player.queue.clear()
        await interaction.response.send_message(f"🧹 ล้างคิว {count} เพลงแล้ว")

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
