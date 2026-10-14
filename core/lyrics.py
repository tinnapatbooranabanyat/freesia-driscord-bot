"""Song lyrics from lrclib.net (free, no API key)."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

API = "https://lrclib.net/api/search"
HEADERS = {"User-Agent": "discord-llm-bot/1.0 (self-hosted Discord bot)"}

# "(Official MV)", "[Lyric Video]", "| 4K", "ft. X" ... are noise for a lyrics search
_NOISE = re.compile(
    r"[\(\[【][^\)\]】]*(official|m/?v|music video|video|audio|lyric|visuali[sz]er|4k|hd|hq|remaster|"
    r"live|performance|teaser|color coded|eng|sub|ver\.?)[^\)\]】]*[\)\]】]",
    re.I,
)
_FEAT = re.compile(r"\s+(ft\.?|feat\.?|featuring)\s+.*$", re.I)
_TAIL = re.compile(r"\s*[|｜].*$")
_SUFFIX = re.compile(
    r"\s*(official\s*)?((music|lyric)\s*)?(video|audio|mv|m/v|visuali[sz]er)\s*$|\s*(lyrics?|4k|hd|hq)\s*$", re.I
)
_JP_TITLE = re.compile(r"[「『](.+?)[」』]")  # YOASOBI「夜に駆ける」 -> song inside the brackets
_QUOTES = " '\"‘’“”-–—"


@dataclass
class Lyrics:
    title: str
    artist: str
    text: str
    synced: bool


def clean_title(title: str, artist: str | None) -> tuple[str, str | None]:
    """Strip video noise and split "Artist - Song" titles. Returns (song, artist)."""
    if artist:
        artist = re.sub(r"\s*-\s*Topic$", "", artist).strip()  # YouTube auto-generated "Artist - Topic"
    t = _TAIL.sub("", _NOISE.sub("", title))
    jp = _JP_TITLE.search(t)
    if jp:
        artist = t[:jp.start()].strip(_QUOTES) or artist
        t = jp.group(1)
    t = _FEAT.sub("", t).strip(_QUOTES)
    if " - " in t:
        left, right = t.split(" - ", 1)
        if not artist or artist.lower() in left.lower() or left.lower() in (artist or "").lower():
            artist, t = left, right
    previous = None
    while previous != t:  # peel off trailing "M/V", "Official Video", "Lyrics", quotes...
        previous = t
        t = _SUFFIX.sub("", t).strip(_QUOTES)
    t = re.sub(r"\s+", " ", t).strip()
    return t or title, artist


def _variants(song: str) -> list[str]:
    """'춤 (CHOOM)' -> ['춤 (CHOOM)', 'CHOOM', '춤']: databases often store only one of the names."""
    out = [song]
    m = re.search(r"\(([^)]+)\)", song)
    if m:
        out += [m.group(1).strip(), (song[:m.start()] + song[m.end():]).strip()]
    return [v for v in dict.fromkeys(out) if v]


async def _search(session: aiohttp.ClientSession, params: dict) -> list[dict]:
    for attempt in range(2):
        try:
            async with session.get(API, params=params) as r:
                if r.status == 503 and attempt == 0:  # lrclib answers "server busy" now and then
                    await asyncio.sleep(1.5)
                    continue
                if r.status != 200:
                    return []
                data = await r.json()
                return data if isinstance(data, list) else []
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.info("lrclib request failed: %r", e)
            return []
    return []


async def find_lyrics(title: str, artist: str | None = None, duration: float | None = None) -> Lyrics | None:
    song, artist = clean_title(title, artist)
    queries = []
    for name in _variants(song):
        if artist:
            queries.append({"track_name": name, "artist_name": artist})
            queries.append({"q": f"{artist} {name}"})
    queries.append({"q": song})

    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as session:
        for params in queries:
            results = [r for r in await _search(session, params) if r.get("plainLyrics") or r.get("syncedLyrics")]
            if not results:
                continue
            if duration:  # prefer the version whose length matches the playing track
                results.sort(key=lambda r: abs((r.get("duration") or 0) - duration))
            best = results[0]
            text = best.get("plainLyrics") or re.sub(r"^\[[\d:.]+\]\s?", "", best["syncedLyrics"], flags=re.M)
            return Lyrics(best.get("trackName") or song, best.get("artistName") or artist or "", text.strip(),
                          bool(best.get("syncedLyrics")))
    return None
