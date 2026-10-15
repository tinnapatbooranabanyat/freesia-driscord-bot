"""Text-to-speech with Microsoft Edge's free online voices (edge-tts). Returns MP3 bytes."""
from __future__ import annotations

import logging
import re

import edge_tts

log = logging.getLogger(__name__)

VOICES = {
    "female": ("th-TH-PremwadeeNeural", "en-US-AvaNeural"),
    "male": ("th-TH-NiwatNeural", "en-US-AndrewNeural"),
}
MAX_CHARS = 1200
_THAI = re.compile(r"[฀-๿]")


class TTSError(Exception):
    pass


def clean_for_speech(text: str) -> str:
    """Markdown/links/code/emoji are noise when read aloud."""
    text = re.sub(r"```.*?```", " (โค้ด) ", text, flags=re.S)
    text = re.sub(r"^-# .*$", "", text, flags=re.M)  # small footer lines (sources...)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"<?https?://\S+>?", "", text)
    text = re.sub(r"<a?:\w+:\d+>|<[@#&!]+\d+>", "", text)  # custom emoji, mentions
    text = re.sub(r"[*_`~>|#]", "", text)
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿]", "", text)  # emoji
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


def _chunks(text: str, size: int = 280) -> list[str]:
    """Split at sentence ends / spaces so each request stays small (long requests fail more often)."""
    out: list[str] = []
    while len(text) > size:
        cut = max(text.rfind(p, 0, size) for p in (". ", "? ", "! ", " "))
        cut = cut + 1 if cut > size // 3 else size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


async def _synth(text: str, voices: list[str]) -> bytes:
    # The Thai female voice randomly answers "no audio" for some sentences; retry, then use the other voice
    for voice in voices:
        for _ in range(2):
            try:
                audio = b""
                async for chunk in edge_tts.Communicate(text, voice).stream():
                    if chunk["type"] == "audio":
                        audio += chunk["data"]
                if audio:
                    return audio
            except Exception as e:
                log.info("edge-tts %s failed: %r", voice, e)
    raise TTSError("สร้างเสียงไม่ได้ ลองใหม่อีกครั้ง")


async def synthesize(text: str, gender: str = "female") -> bytes:
    text = clean_for_speech(text)
    if not text:
        raise TTSError("ไม่มีข้อความให้อ่าน")
    thai, english = VOICES.get(gender, VOICES["female"])
    other_thai = VOICES["male" if gender == "female" else "female"][0]
    voices = [thai, other_thai] if _THAI.search(text) else [english, thai]
    # MP3 frames can simply be concatenated; ffmpeg plays the result as one stream
    return b"".join([await _synth(part, voices) for part in _chunks(text)])
