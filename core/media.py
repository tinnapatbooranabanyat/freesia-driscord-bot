"""YouTube / YouTube Music / Spotify lookup with yt-dlp, and the Track model the music player queues."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp

from core.spotify import fetch_spotify, is_spotify_url

log = logging.getLogger(__name__)

MAX_PLAYLIST = 200
MAX_MIX = 50  # YouTube Mixes (list=RD...) are endless; take the first songs only
STREAM_URL_TTL = 3 * 60 * 60  # YouTube stream URLs expire after ~6h; re-resolve well before that


def _ytdl_opts(cookies: bool = False, **extra) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "source_address": "0.0.0.0",
    }
    cookie_file = os.getenv("YTDL_COOKIES")
    if cookies and cookie_file and os.path.isfile(cookie_file):
        opts["cookiefile"] = cookie_file
    opts.update(extra)
    return opts


def _extract(query: str, **extra) -> dict:
    """yt-dlp extract_info without cookies first; retry with YTDL_COOKIES only when YouTube asks
    "Sign in to confirm you're not a bot". (Logged-in requests often get stream URLs that need a PO token
    and fail with 403, so cookies are a fallback for blocks rather than the default.)"""
    try:
        with yt_dlp.YoutubeDL(_ytdl_opts(**extra)) as ydl:
            return ydl.extract_info(query, download=False)
    except yt_dlp.utils.DownloadError as e:
        if not any(s in str(e) for s in ("not a bot", "confirm your age")) or "cookiefile" not in _ytdl_opts(cookies=True):
            raise
        log.info("YouTube asked to sign in (bot / age check), retrying with cookies: %s", query)
        with yt_dlp.YoutubeDL(_ytdl_opts(cookies=True, **extra)) as ydl:
            return ydl.extract_info(query, download=False)


def extract_single(query: str) -> dict:
    """Fully resolve one video (URL or 'ytsearch1:...'), including the direct audio stream URL."""
    info = _extract(query)
    if info and "entries" in info:
        info = next((e for e in info["entries"] if e), None)
    if not info or not info.get("url"):
        raise LookupError("ไม่พบเพลง")
    return info


def video_id(url: str | None) -> str | None:
    if not url:
        return None
    u = urlparse(url)
    if u.netloc.endswith("youtu.be"):
        return u.path.strip("/") or None
    return parse_qs(u.query).get("v", [None])[0]


def extract_query(query: str) -> tuple[str | None, list[dict]]:
    """Return (playlist title or None, infos). Playlist entries are extracted 'flat' (fast, no stream URL).

    A watch link that also carries a playlist / Mix ("...watch?v=X&list=PL..." or "&list=RD...") queues the
    whole list, starting from the video in the link.
    """
    if not query.startswith(("http://", "https://")):
        return None, [extract_single(f"ytsearch1:{query}")]
    list_id = parse_qs(urlparse(query).query).get("list", [None])[0]
    vid = video_id(query)

    limit = MAX_MIX if (list_id or "").startswith("RD") else MAX_PLAYLIST
    try:
        info = _extract(query, extract_flat="in_playlist", noplaylist=not list_id, playlistend=limit)
    except yt_dlp.utils.DownloadError:
        # Private / personal lists (Liked videos, Watch later...) can't be read: just play the video itself
        if not (list_id and vid):
            raise
        log.info("Could not load list %s, playing video %s only", list_id, vid)
        return None, [extract_single(f"https://www.youtube.com/watch?v={vid}")]

    if info and "entries" in info:
        entries = [e for e in info["entries"] if e][:limit]
        if vid:
            start = next((i for i, e in enumerate(entries) if e.get("id") == vid), 0)
            entries = entries[start:] + entries[:start]
        if not entries:
            raise LookupError("เพลย์ลิสต์นี้ว่างหรือเปิดไม่ได้")
        return info.get("title") or "Playlist", entries
    if not info or not info.get("url"):
        raise LookupError("ไม่พบเพลง")
    return None, [info]


def search(query: str, count: int = 5) -> list[dict]:
    """Top YouTube results for a text query (flat: title / url / duration / channel, no stream URL)."""
    info = _extract(f"ytsearch{count}:{query}", extract_flat="in_playlist", noplaylist=False)
    return [e for e in (info or {}).get("entries") or [] if e and e.get("id")]


def related(seed_id: str, exclude: set[str], count: int = 8) -> list[dict]:
    """Songs similar to a YouTube video, taken from its YouTube Mix (what autoplay on YouTube uses)."""
    _, entries = extract_query(f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}")
    picks = [e for e in entries if e.get("id") and e["id"] != seed_id and e["id"] not in exclude]
    return picks[:count]


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "LIVE/?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(text: str) -> float | None:
    """'90', '1:30' or '1:02:03' -> seconds."""
    try:
        parts = [float(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


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
    retried: bool = False  # already re-fetched once after the stream failed to start

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

    @classmethod
    def from_search(cls, text: str, requester: discord.abc.User) -> Track:
        """A track known only by "Artist - Title" (Spotify); matched on YouTube when it is about to play."""
        return cls(title=text, search=text, requester=requester, artist=text.split(" - ", 1)[0] if " - " in text else None)

    def to_dict(self) -> dict:
        """Saved-playlist form (no requester / stream URL)."""
        return {k: getattr(self, k) for k in ("title", "url", "search", "duration", "thumbnail", "artist")}

    @classmethod
    def from_dict(cls, data: dict, requester: discord.abc.User) -> Track:
        return cls(requester=requester, **{k: data.get(k) for k in ("title", "url", "search", "duration", "thumbnail", "artist")})

    @property
    def video_id(self) -> str | None:
        return video_id(self.url)

    @property
    def link(self) -> str:
        return f"[{self.title}]({self.url})" if self.url else self.title

    @property
    def card_title(self) -> str:
        """Just the song name ("One More Night"), without "Artist - " or "(Official Music Video)" noise:
        the cards show the artist separately."""
        from core.lyrics import clean_title  # local import: lyrics doesn't need media
        song, _ = clean_title(self.title, self.artist)
        return song or self.title

    @property
    def display_name(self) -> str:
        """Clean "Artist - Song" for lists and "next up" lines."""
        from core.lyrics import clean_title
        song, artist = clean_title(self.title, self.artist)
        return f"{artist} - {song}" if artist and song and artist.lower() not in song.lower() else (song or self.title)

    async def resolve(self) -> None:
        if self.stream_url and time.time() - self.resolved_at < STREAM_URL_TTL:
            return
        query = self.url or f"ytsearch1:{self.search}"
        info = await asyncio.get_running_loop().run_in_executor(None, extract_single, query)
        self.stream_url = info["url"]
        self.resolved_at = time.time()
        self.url = info.get("webpage_url") or self.url
        self.duration = info.get("duration") or self.duration
        self.thumbnail = info.get("thumbnail") or self.thumbnail
        self.artist = self.artist or self._artist(info)
        if not self.search:  # keep the nicer "Artist - Title" name for Spotify tracks
            self.title = info.get("title") or self.title


async def load(query: str, requester: discord.abc.User) -> tuple[str | None, list[Track]]:
    """Turn a /play query (URL or search text) into (playlist name or None, tracks)."""
    if is_spotify_url(query):
        name, songs = await fetch_spotify(query)
        tracks = [Track.from_search(s, requester) for s in songs[:MAX_PLAYLIST]]
        return (name if "/track/" not in query else None), tracks
    playlist, infos = await asyncio.get_running_loop().run_in_executor(None, extract_query, query)
    tracks = [Track.from_info(i, requester, resolved=playlist is None) for i in infos]
    if not tracks:
        raise LookupError("ไม่พบเพลง")
    return playlist, tracks
