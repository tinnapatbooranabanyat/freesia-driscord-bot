"""Parse reminder times like "30m", "1 ชั่วโมง 30 นาที", "18:30", "พรุ่งนี้ 9:00", "25/12 20:00", "2026-12-31 23:59"."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

THAI_TZ = timezone(timedelta(hours=7))

_UNITS = {
    "s": 1, "sec": 1, "วิ": 1, "วินาที": 1,
    "m": 60, "min": 60, "นาที": 60,
    "h": 3600, "hr": 3600, "ชม": 3600, "ชั่วโมง": 3600,
    "d": 86400, "day": 86400, "วัน": 86400,
    "w": 604800, "สัปดาห์": 604800, "อาทิตย์": 604800,
}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True)) + r")\.?", re.I)
_CLOCK = re.compile(r"(\d{1,2})[:.](\d{2})")
_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Return an aware datetime (UTC+7) in the future, or None if the text isn't understood."""
    now = now or datetime.now(THAI_TZ)
    t = text.strip().lower().replace("อีก", "").replace("ใน", "").strip()

    # Relative: "30m", "1h 30m", "2 ชั่วโมง 15 นาที"
    rest = _DURATION.sub("", t).replace("และ", "").strip(" ,")
    matches = _DURATION.findall(t)
    if matches and not rest:
        seconds = sum(float(n) * _UNITS[u.lower()] for n, u in matches)
        return now + timedelta(seconds=seconds) if seconds > 0 else None

    # Absolute: optional date + clock time
    clock = _CLOCK.search(t)
    date = _DATE.search(_CLOCK.sub("", t)) if t else None
    if not clock and not date:
        return None
    hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (9, 0)
    if hour > 23 or minute > 59:
        return None

    if date:
        if date.group(1):
            year, month, day = int(date.group(1)), int(date.group(2)), int(date.group(3))
        else:
            day, month = int(date.group(4)), int(date.group(5))
            year = int(date.group(6)) if date.group(6) else now.year
            if year < 100:
                year += 2000
        if year > 2400:  # Buddhist era
            year -= 543
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=THAI_TZ)
        except ValueError:
            return None
        if not date.group(6) and not date.group(1) and when <= now:
            when = when.replace(year=when.year + 1)  # "25/12" already passed this year
    else:
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "พรุ่งนี้" in t or "tomorrow" in t:
            when += timedelta(days=1)
        elif when <= now:
            when += timedelta(days=1)
    return when if when > now else None
