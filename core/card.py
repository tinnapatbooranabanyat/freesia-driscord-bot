"""Render a "now playing" banner image (1000x300 PNG) with Pillow.

Text is shaped with HarfBuzz and rasterised with FreeType instead of Pillow's text API: Pillow's basic
layout can't stack Thai vowels and tone marks (ที่ comes out as ที), and its HarfBuzz mode needs an extra
fribidi DLL on Windows. Song titles also mix scripts (Thai, Korean, Japanese, symbols...), so text is
split into runs and each run is drawn with the first font in the chain that has those characters.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import freetype
import uharfbuzz as hb
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

log = logging.getLogger(__name__)

W, H = 1000, 380
THUMB_W, THUMB_H = 448, 252  # 16:9 like YouTube thumbnails
PAD = 40

_WIN = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_NOTO = "/usr/share/fonts/truetype/noto"
_CJK = "/usr/share/fonts/opentype/noto"
_HERE = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")

# Fallback chains, first match wins per character. Windows system fonts, then Debian/Ubuntu Noto
# packages (fonts-noto-core, fonts-noto-cjk), then anything dropped into assets/fonts.
FONT_CHAINS = {
    "regular": [
        f"{_WIN}/segoeui.ttf", f"{_WIN}/LeelawUI.ttf", f"{_WIN}/malgun.ttf", f"{_WIN}/YuGothM.ttc",
        f"{_WIN}/msyh.ttc", f"{_WIN}/seguisym.ttf",
        f"{_NOTO}/NotoSans-Regular.ttf", f"{_NOTO}/NotoSansThai-Regular.ttf", f"{_CJK}/NotoSansCJK-Regular.ttc",
        f"{_NOTO}/NotoSansSymbols2-Regular.ttf", f"{_HERE}/regular.ttf",
    ],
    "bold": [
        f"{_WIN}/segoeuib.ttf", f"{_WIN}/LeelaUIb.ttf", f"{_WIN}/malgunbd.ttf", f"{_WIN}/YuGothB.ttc",
        f"{_WIN}/msyhbd.ttc", f"{_WIN}/seguisym.ttf",
        f"{_NOTO}/NotoSans-Bold.ttf", f"{_NOTO}/NotoSansThai-Bold.ttf", f"{_CJK}/NotoSansCJK-Bold.ttc",
        f"{_NOTO}/NotoSansSymbols2-Regular.ttf", f"{_HERE}/bold.ttf",
    ],
}

# FreeType faces are stateful; cards are rendered from worker threads, so render one at a time
_LOCK = threading.Lock()


@lru_cache(maxsize=None)
def _hb_face(path: str) -> hb.Face:
    return hb.Face(hb.Blob.from_file_path(path), 0)


@lru_cache(maxsize=None)
def _hb_font(path: str, size: int) -> hb.Font:
    font = hb.Font(_hb_face(path))
    font.scale = (size * 64, size * 64)  # positions come back in 26.6 fixed point, like FreeType
    return font


@lru_cache(maxsize=None)
def _ft_face(path: str, size: int) -> freetype.Face:
    face = freetype.Face(path, 0)
    face.set_char_size(size * 64)
    return face


@lru_cache(maxsize=None)
def _available(weight: str) -> tuple[str, ...]:
    paths = []
    for p in FONT_CHAINS[weight]:
        if os.path.isfile(p):
            try:
                _hb_face(p)
                paths.append(p)
            except Exception as e:  # broken / unsupported font file
                log.warning("Skipping font %s: %r", p, e)
    if not paths:
        raise RuntimeError("No fonts found for the now-playing card")
    return tuple(paths)


@lru_cache(maxsize=4096)
def _has_char(path: str, cp: int) -> bool:
    return _hb_font(path, 16).get_nominal_glyph(cp) is not None


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Mc", "Me") or ch in "\u200d\ufe0f"


@dataclass
class _Run:
    text: str
    path: str
    size: int

    def shape(self) -> tuple[list, list]:
        buf = hb.Buffer()
        buf.add_str(self.text)
        buf.guess_segment_properties()
        hb.shape(_hb_font(self.path, self.size), buf, {"kern": True, "liga": True})
        return buf.glyph_infos, buf.glyph_positions

    @property
    def width(self) -> float:
        return sum(p.x_advance for p in self.shape()[1]) / 64


def _runs(text: str, weight: str, size: int) -> list[_Run]:
    """Split text into runs that each render with one font. Combining marks stay with their base char."""
    paths = _available(weight)
    runs: list[_Run] = []
    for ch in text:
        if runs and _is_mark(ch):
            runs[-1].text += ch
            continue
        path = next((p for p in paths if _has_char(p, ord(ch))), None)
        if path is None:
            if not ch.isspace():
                continue  # no font has this glyph (e.g. colour emoji): drop it rather than draw a box
            path = runs[-1].path if runs else paths[0]
        if runs and runs[-1].path == path:
            runs[-1].text += ch
        else:
            runs.append(_Run(ch, path, size))
    return runs


def _width(text: str, weight: str, size: int) -> float:
    return sum(r.width for r in _runs(text, weight, size))


def _draw_text(img: Image.Image, xy: tuple[float, float], text: str, weight: str, size: int,
               fill: tuple[int, int, int], anchor: str = "ls") -> None:
    """Draw text with its baseline at y. anchor 'ls' = left, 'rs' = right, 'ms' = centred."""
    runs = _runs(text, weight, size)
    x, baseline = xy
    if anchor == "rs":
        x -= sum(r.width for r in runs)
    elif anchor == "ms":
        x -= sum(r.width for r in runs) / 2
    colour = Image.new("RGBA", (1, 1), fill + (255,))
    for run in runs:
        face = _ft_face(run.path, size)
        infos, positions = run.shape()
        for info, pos in zip(infos, positions):
            face.load_glyph(info.codepoint, freetype.FT_LOAD_DEFAULT)
            face.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
            bmp = face.glyph.bitmap
            if bmp.width and bmp.rows:
                data = bytes(bmp.buffer)
                if bmp.pitch != bmp.width:  # rows can be padded
                    data = b"".join(data[r * bmp.pitch: r * bmp.pitch + bmp.width] for r in range(bmp.rows))
                mask = Image.frombytes("L", (bmp.width, bmp.rows), data)
                gx = round(x + pos.x_offset / 64 + face.glyph.bitmap_left)
                gy = round(baseline - pos.y_offset / 64 - face.glyph.bitmap_top)
                img.paste(colour.resize(mask.size), (gx, gy), mask)
            x += pos.x_advance / 64


def _clusters(text: str) -> list[str]:
    """Split into user-visible characters (base char + its combining marks) so we never cut through a Thai syllable."""
    out: list[str] = []
    for ch in text:
        if out and _is_mark(ch):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _fit(text: str, weight: str, size: int, max_w: float) -> str:
    """Trim text with an ellipsis to fit max_w pixels."""
    if _width(text, weight, size) <= max_w:
        return text
    parts = _clusters(text)
    while parts and _width("".join(parts) + "…", weight, size) > max_w:
        parts.pop()
    return "".join(parts).rstrip() + "…"


def _wrap(text: str, weight: str, size: int, max_w: float, max_lines: int) -> list[str]:
    """Wrap into at most max_lines lines (break at spaces when possible), ellipsizing the last line."""
    lines: list[str] = []
    rest = _clusters(text.strip())
    while rest and len(lines) < max_lines - 1:
        if _width("".join(rest), weight, size) <= max_w:
            break
        n = len(rest)
        while n > 1 and _width("".join(rest[:n]), weight, size) > max_w:
            n -= 1
        space = max((i for i in range(n) if rest[i] == " "), default=-1)
        if space > n // 2:
            n = space + 1
        lines.append("".join(rest[:n]).rstrip())
        rest = rest[n:]
        while rest and rest[0] == " ":
            rest.pop(0)
    if rest:
        lines.append(_fit("".join(rest), weight, size, max_w))
    return lines


def _accent(img: Image.Image) -> tuple[int, int, int]:
    """A vivid version of the thumbnail's average colour, used for the progress bar and embed colour."""
    small = ImageEnhance.Color(img.convert("RGB").resize((32, 32))).enhance(2.2)
    r, g, b = (int(c) for c in ImageStat.Stat(small).mean[:3])
    boost = max(0, 150 - max(r, g, b))  # keep it bright enough to read on the dark background
    return min(255, r + boost), min(255, g + boost), min(255, b + boost)


def _fmt(seconds: float | None) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def render_card(
    thumbnail: bytes | None,
    title: str,
    artist: str | None,
    duration: float | None,
    position: float = 0.0,
    footer: str | None = None,
    label: str = "NOW PLAYING",
) -> tuple[bytes, tuple[int, int, int]]:
    """Return (JPEG bytes, accent RGB colour)."""
    with _LOCK:
        return _render(thumbnail, title, artist, duration, position, footer, label)


def _render(thumbnail, title, artist, duration, position, footer, label):
    thumb = None
    if thumbnail:
        try:
            thumb = Image.open(io.BytesIO(thumbnail)).convert("RGB")
        except Exception as e:
            log.warning("Bad thumbnail: %r", e)
    if thumb is None:
        thumb = Image.new("RGB", (480, 270), (40, 44, 52))
    accent = _accent(thumb)

    # Background: blurred, darkened thumbnail + left-to-right shade
    bg = ImageOps.fit(thumb, (W, H)).filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    shade = Image.linear_gradient("L").rotate(90, expand=True).resize((W, H))  # 255 on the left -> 0 on the right
    bg = Image.composite(Image.new("RGB", (W, H), (12, 12, 16)), bg, shade.point(lambda v: int(v * 0.55)))
    card = bg.convert("RGBA")

    # Thumbnail with rounded corners and a soft shadow
    tx, ty = PAD, (H - THUMB_H) // 2
    art = ImageOps.fit(thumb, (THUMB_W, THUMB_H)).convert("RGBA")
    mask = Image.new("L", (THUMB_W, THUMB_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, THUMB_W, THUMB_H), radius=22, fill=255)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((tx + 4, ty + 10, tx + THUMB_W + 4, ty + THUMB_H + 10), radius=22, fill=(0, 0, 0, 160))
    card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    card.paste(art, (tx, ty), mask)

    draw = ImageDraw.Draw(card)
    x = tx + THUMB_W + 36
    max_w = W - x - PAD

    # Label pill
    lw = _width(label, "bold", 19)
    draw.rounded_rectangle((x, 56, x + lw + 30, 92), radius=18, fill=accent + (255,))
    text_col = (20, 20, 24) if sum(accent) > 450 else (255, 255, 255)
    _draw_text(card, (x + 15, 81), label, "bold", 19, text_col)

    # Title (up to 2 lines) and artist
    y = 160
    for line in _wrap(title, "bold", 48, max_w, 2):
        _draw_text(card, (x, y), line, "bold", 48, (255, 255, 255))
        y += 60
    if artist:
        _draw_text(card, (x, y + 2), _fit(artist, "regular", 28, max_w), "regular", 28, (205, 205, 215))

    # Progress bar + times + footer
    bar_y = 308
    draw.rounded_rectangle((x, bar_y, x + max_w, bar_y + 8), radius=4, fill=(255, 255, 255, 70))
    if duration:
        filled = int(max_w * max(0.0, min(1.0, position / duration)))
        if filled > 0:
            draw.rounded_rectangle((x, bar_y, x + max(filled, 8), bar_y + 8), radius=4, fill=accent + (255,))
        draw.ellipse((x + filled - 11, bar_y - 7, x + filled + 11, bar_y + 15), fill=(255, 255, 255))
    grey = (215, 215, 225)
    _draw_text(card, (x, bar_y + 44), _fmt(position) if duration else "🔴 LIVE", "regular", 22, grey)
    if duration:
        _draw_text(card, (x + max_w, bar_y + 44), _fmt(duration), "regular", 22, grey, anchor="rs")
    if footer:
        footer = _fit(footer, "regular", 20, max_w * 0.55)
        _draw_text(card, (x + max_w / 2, bar_y + 44), footer, "regular", 20, (170, 170, 180), anchor="ms")

    out = io.BytesIO()
    # JPEG: ~4x smaller than PNG, which matters because the card is re-uploaded every few seconds
    card.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue(), accent


# ---------------------------------------------------------------------------
# Welcome card (new member joins)
# ---------------------------------------------------------------------------

WW, WH = 1000, 460


def _circle(img: Image.Image, size: int) -> Image.Image:
    """Anti-aliased circular crop (mask drawn at 4x then downsampled)."""
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    out = ImageOps.fit(img.convert("RGBA"), (size, size))
    out.putalpha(big.resize((size, size), Image.LANCZOS))
    return out


def _ring(size: int, width: int, colour: tuple[int, int, int], alpha: int = 255) -> Image.Image:
    big = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), outline=colour + (alpha,), width=width * 4)
    return big.resize((size, size), Image.LANCZOS)


def render_welcome(avatar: bytes | None, background: bytes | None, name: str, username: str,
                   server: str, member_number: int) -> tuple[bytes, tuple[int, int, int]]:
    """Return (PNG bytes, accent RGB colour)."""
    with _LOCK:
        return _render_welcome(avatar, background, name, username, server, member_number)


def _open(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        log.warning("Bad image: %r", e)
        return None


def _render_welcome(avatar, background, name, username, server, member_number):
    av = _open(avatar) or Image.new("RGB", (256, 256), (88, 101, 242))
    accent = _accent(av)

    # Background: server banner/icon (or the avatar) blurred and darkened, with the accent colour glowing
    # behind the avatar
    bg = ImageOps.fit(_open(background) or av, (WW, WH)).filter(ImageFilter.GaussianBlur(24))
    card = ImageEnhance.Brightness(bg).enhance(0.38).convert("RGBA")
    shade = Image.linear_gradient("L").resize((WW, WH)).point(lambda v: int(v * 0.6))  # darker towards the bottom
    card.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (WW, WH), (8, 8, 12)).split(), shade)))

    cx, cy, size = WW // 2, 140, 184
    glow = Image.new("RGBA", (WW, WH), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - 170, cy - 150, cx + 170, cy + 150), fill=accent + (150,))
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(55)))

    # Thin rounded frame
    frame = Image.new("RGBA", (WW, WH), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle((14, 14, WW - 15, WH - 15), radius=26, outline=accent + (110,), width=3)
    card.alpha_composite(frame)

    # Avatar with accent ring + soft white inner ring
    ring = size + 22
    card.alpha_composite(_ring(ring, 6, accent), (cx - ring // 2, cy - ring // 2))
    inner = size + 8
    card.alpha_composite(_ring(inner, 2, (255, 255, 255), 170), (cx - inner // 2, cy - inner // 2))
    card.alpha_composite(_circle(av, size), (cx - size // 2, cy - size // 2))

    draw = ImageDraw.Draw(card)
    # "WELCOME" pill
    label = "WELCOME"
    lw = _width(label, "bold", 16)
    top = cy + ring // 2 + 16
    draw.rounded_rectangle((cx - lw / 2 - 16, top, cx + lw / 2 + 16, top + 30), radius=15, fill=accent + (255,))
    text_col = (20, 20, 24) if sum(accent) > 450 else (255, 255, 255)
    _draw_text(card, (cx, top + 21), label, "bold", 16, text_col, anchor="ms")

    _draw_text(card, (cx, top + 86), _fit(name, "bold", 46, WW - 160), "bold", 46, (255, 255, 255), anchor="ms")
    _draw_text(card, (cx, top + 124), _fit(f"ยินดีต้อนรับสู่ {server}", "regular", 24, WW - 200), "regular", 24,
               (225, 225, 235), anchor="ms")
    _draw_text(card, (cx, top + 156), _fit(f"@{username}  •  สมาชิกคนที่ #{member_number:,}", "regular", 18, WW - 240),
               "regular", 18, (170, 170, 185), anchor="ms")

    out = io.BytesIO()
    card.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue(), accent


def circle_png(data: bytes, size: int = 128) -> bytes:
    """Round avatar PNG (used for the bot-avatar emoji in the card header)."""
    img = _open(data) or Image.new("RGB", (size, size), (88, 101, 242))
    out = io.BytesIO()
    _circle(img, size).save(out, "PNG")
    return out.getvalue()
