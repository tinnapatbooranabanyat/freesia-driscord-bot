"""Read Spotify track / album / playlist info from the public embed page (no API key needed).

Spotify does not allow audio streaming, so we only take "title + artist"
and search for the same song on YouTube.
"""
from __future__ import annotations

import json
import re

import aiohttp

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]+)"
)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}


class SpotifyError(Exception):
    pass


def is_spotify_url(text: str) -> bool:
    return SPOTIFY_URL_RE.search(text) is not None


async def fetch_spotify(url: str) -> tuple[str, list[str]]:
    """Return (collection name, ["artist - title", ...]) for a Spotify link."""
    m = SPOTIFY_URL_RE.search(url)
    if not m:
        raise SpotifyError("ลิงก์ Spotify ไม่ถูกต้อง")
    kind, sid = m.groups()

    async with aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(f"https://open.spotify.com/embed/{kind}/{sid}") as r:
            if r.status != 200:
                raise SpotifyError(f"เปิดลิงก์ Spotify ไม่ได้ (HTTP {r.status})")
            html = await r.text()

    nd = _NEXT_DATA_RE.search(html)
    if not nd:
        raise SpotifyError("อ่านข้อมูลจาก Spotify ไม่ได้")
    try:
        entity = json.loads(nd.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise SpotifyError("รูปแบบข้อมูลจาก Spotify เปลี่ยนไป") from e

    name = entity.get("name") or entity.get("title") or "Spotify"
    if kind == "track":
        artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
        return name, [f"{artists} - {name}" if artists else name]

    songs = []
    for t in entity.get("trackList") or []:
        title, artist = t.get("title"), (t.get("subtitle") or "").replace("\xa0", " ")
        if title:
            songs.append(f"{artist} - {title}" if artist else title)
    if not songs:
        raise SpotifyError("ไม่พบเพลงใน playlist/album นี้")
    return name, songs
