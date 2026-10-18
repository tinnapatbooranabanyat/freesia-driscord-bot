"""Flat white player icons, drawn with Pillow and uploaded as the bot's application emojis.

Discord's built-in emoji (⏯️ ⏭️ ...) are boxed blue squares; these plain glyphs look like a real music
player on coloured buttons. Each icon is drawn at 4x and downsampled for smooth edges, with a faint dark
outline so it stays visible on light-theme grey buttons.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFilter

ICON_VERSION = "1"  # bump to re-upload after changing the drawings
SIZE = 128
S = SIZE * 4  # drawing scale
WHITE = (255, 255, 255, 255)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _p(*xy: float) -> list[tuple[float, float]]:
    """Points given on a 0-100 grid."""
    return [(xy[i] * S / 100, xy[i + 1] * S / 100) for i in range(0, len(xy), 2)]


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    return x0 * S / 100, y0 * S / 100, x1 * S / 100, y1 * S / 100


def _arrow_arc(d: ImageDraw.ImageDraw, box, start: float, end: float, width: float, head_at_end: bool = True) -> None:
    """Arc (degrees, clockwise from 3 o'clock) with a triangular arrow head."""
    d.arc(_box(*box), start, end, fill=WHITE, width=int(width * S / 100))
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    r = (box[2] - box[0]) / 2
    ang = math.radians(end if head_at_end else start)
    tip_dir = 1 if head_at_end else -1  # tangent direction of travel
    px, py = cx + r * math.cos(ang), cy + r * math.sin(ang)
    tx, ty = -math.sin(ang) * tip_dir, math.cos(ang) * tip_dir  # tangent
    nx, ny = math.cos(ang), math.sin(ang)  # normal
    size = width * 1.5
    tip = (px + tx * size * 1.2, py + ty * size * 1.2)
    a = (px + nx * size, py + ny * size)
    b = (px - nx * size, py - ny * size)
    d.polygon(_p(*tip, *a, *b), fill=WHITE)


def _draw(name: str) -> Image.Image:
    img, d = _canvas()
    if name == "play":
        d.rounded_rectangle(_box(0, 0, 0, 0), 0)  # no-op keeps the shape list readable
        d.polygon(_p(30, 18, 30, 82, 84, 50), fill=WHITE)
    elif name == "pause":
        d.rounded_rectangle(_box(24, 18, 42, 82), radius=S * 0.04, fill=WHITE)
        d.rounded_rectangle(_box(58, 18, 76, 82), radius=S * 0.04, fill=WHITE)
    elif name == "next":
        d.polygon(_p(18, 20, 18, 80, 64, 50), fill=WHITE)
        d.rounded_rectangle(_box(68, 20, 82, 80), radius=S * 0.03, fill=WHITE)
    elif name == "previous":
        d.polygon(_p(82, 20, 82, 80, 36, 50), fill=WHITE)
        d.rounded_rectangle(_box(18, 20, 32, 80), radius=S * 0.03, fill=WHITE)
    elif name == "stop":
        d.rounded_rectangle(_box(22, 22, 78, 78), radius=S * 0.08, fill=WHITE)
    elif name in ("loop", "loop_one"):
        # two arcs forming a circle with arrow heads: the classic "repeat" glyph
        _arrow_arc(d, (16, 16, 84, 84), 200, 340, 10)
        _arrow_arc(d, (16, 16, 84, 84), 20, 160, 10)
        if name == "loop_one":
            d.rounded_rectangle(_box(46, 34, 56, 66), radius=S * 0.02, fill=WHITE)
            d.polygon(_p(46, 34, 56, 34, 40, 44, 38, 40), fill=WHITE)
    elif name == "shuffle":
        w = int(9 * S / 100)
        # crossing lines with arrow heads on the right
        d.line(_p(14, 30, 34, 30, 62, 70, 76, 70), fill=WHITE, width=w, joint="curve")
        d.line(_p(14, 70, 34, 70, 62, 30, 76, 30), fill=WHITE, width=w, joint="curve")
        d.polygon(_p(74, 18, 92, 30, 74, 42), fill=WHITE)
        d.polygon(_p(74, 58, 92, 70, 74, 82), fill=WHITE)
    elif name == "autoplay":
        # infinity sign: two loops
        w = int(10 * S / 100)
        d.ellipse(_box(10, 30, 52, 70), outline=WHITE, width=w)
        d.ellipse(_box(48, 30, 90, 70), outline=WHITE, width=w)
    elif name == "queue":
        # 2x2 grid of rounded squares (menu / queue)
        for x0, y0 in ((18, 18), (54, 18), (18, 54), (54, 54)):
            d.rounded_rectangle(_box(x0, y0, x0 + 28, y0 + 28), radius=S * 0.06, fill=WHITE)
    elif name == "star":
        pts = []
        for i in range(10):
            r = 40 if i % 2 == 0 else 17
            a = math.radians(-90 + i * 36)
            pts += [50 + r * math.cos(a), 54 + r * math.sin(a)]
        d.polygon(_p(*pts), fill=WHITE)
    elif name == "lyrics":
        # microphone
        d.rounded_rectangle(_box(36, 8, 64, 58), radius=S * 0.14, fill=WHITE)
        d.arc(_box(24, 26, 76, 70), 0, 180, fill=WHITE, width=int(8 * S / 100))
        d.rounded_rectangle(_box(46, 68, 54, 84), radius=S * 0.02, fill=WHITE)
        d.rounded_rectangle(_box(32, 82, 68, 90), radius=S * 0.03, fill=WHITE)
    # ---- glyphs used only on notice banners ----
    elif name == "x":
        w = int(13 * S / 100)
        d.line(_p(24, 24, 76, 76), fill=WHITE, width=w)
        d.line(_p(76, 24, 24, 76), fill=WHITE, width=w)
    elif name == "check":
        d.line(_p(18, 52, 40, 74, 84, 28), fill=WHITE, width=int(13 * S / 100), joint="curve")
    elif name == "alert":
        d.rounded_rectangle(_box(43, 14, 57, 62), radius=S * 0.05, fill=WHITE)
        d.ellipse(_box(42, 70, 58, 86), fill=WHITE)
    elif name == "info":
        d.ellipse(_box(42, 12, 58, 28), fill=WHITE)
        d.rounded_rectangle(_box(43, 38, 57, 86), radius=S * 0.05, fill=WHITE)
    elif name == "moon":
        d.ellipse(_box(14, 14, 82, 82), fill=WHITE)
        d.ellipse(_box(36, 4, 100, 68), fill=(0, 0, 0, 0))  # bite out a crescent
    elif name == "speaker":
        d.polygon(_p(14, 38, 30, 38, 52, 18, 52, 82, 30, 62, 14, 62), fill=WHITE)
        d.arc(_box(42, 32, 70, 68), -50, 50, fill=WHITE, width=int(8 * S / 100))
        d.arc(_box(42, 16, 90, 84), -50, 50, fill=WHITE, width=int(8 * S / 100))
    elif name == "sliders":
        w = int(7 * S / 100)
        for x, knob in ((26, 34), (50, 64), (74, 44)):
            d.line(_p(x, 14, x, 86), fill=WHITE, width=w)
            d.ellipse(_box(x - 11, knob - 11, x + 11, knob + 11), fill=WHITE)
    elif name == "clock":
        d.ellipse(_box(12, 12, 88, 88), outline=WHITE, width=int(9 * S / 100))
        d.line(_p(50, 26, 50, 52, 66, 62), fill=WHITE, width=int(9 * S / 100), joint="curve")
    elif name == "trash":
        d.rounded_rectangle(_box(18, 20, 82, 30), radius=S * 0.03, fill=WHITE)
        d.rounded_rectangle(_box(38, 10, 62, 22), radius=S * 0.03, fill=WHITE)
        d.polygon(_p(24, 34, 76, 34, 70, 88, 30, 88), fill=WHITE)
    elif name == "note":
        d.ellipse(_box(18, 62, 46, 86), fill=WHITE)
        d.rounded_rectangle(_box(38, 12, 46, 76), radius=S * 0.02, fill=WHITE)
        d.polygon(_p(46, 12, 80, 24, 80, 40, 46, 28), fill=WHITE)
    else:
        raise ValueError(name)
    return img


def glyph(name: str, size: int) -> Image.Image:
    """White icon on transparent background at `size` px (for drawing onto banners)."""
    return _draw(name).resize((size, size), Image.LANCZOS)


ICON_NAMES = ("play", "pause", "next", "previous", "stop", "loop", "loop_one", "shuffle", "autoplay",
              "queue", "star", "lyrics")


def render_icon(name: str) -> bytes:
    glyph = _draw(name)
    # faint dark outline: keeps white icons visible on light-theme buttons
    alpha = glyph.getchannel("A")
    outline = alpha.filter(ImageFilter.MaxFilter(21)).filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.8))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.putalpha(outline)
    out.alpha_composite(glyph)
    out = out.resize((SIZE, SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def emoji_name(name: str) -> str:
    return f"fp_{name}_v{ICON_VERSION}"
