"""Small SQLite store (data/bot.db) for things that should survive a restart:
chat memory, per-server personas, per-server settings and users' saved playlists.

sqlite3 is blocking, so every call runs in a worker thread; one connection guarded by a lock is plenty
for a bot this size.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_history_channel ON chat_history(channel_id, id);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS playlists (
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    tracks TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    due REAL NOT NULL,
    text TEXT NOT NULL,
    dm INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(due);
"""


class Storage:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def _run(self, fn):
        def locked():
            with self._lock, self._conn:  # "with conn" commits on success, rolls back on error
                return fn(self._conn)
        return asyncio.to_thread(locked)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- chat memory ----------

    async def get_history(self, channel_id: int, limit: int) -> list[dict]:
        rows = await self._run(lambda c: c.execute(
            "SELECT role, content FROM chat_history WHERE channel_id=? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall())
        return [{"role": r, "content": t} for r, t in reversed(rows)]

    async def add_history(self, channel_id: int, messages: list[dict], keep: int) -> None:
        def write(c: sqlite3.Connection):
            now = time.time()
            c.executemany(
                "INSERT INTO chat_history(channel_id, role, content, ts) VALUES (?,?,?,?)",
                [(channel_id, m["role"], m["content"], now) for m in messages],
            )
            c.execute(
                "DELETE FROM chat_history WHERE channel_id=? AND id NOT IN "
                "(SELECT id FROM chat_history WHERE channel_id=? ORDER BY id DESC LIMIT ?)",
                (channel_id, channel_id, keep),
            )
        await self._run(write)

    async def clear_history(self, channel_id: int) -> None:
        await self._run(lambda c: c.execute("DELETE FROM chat_history WHERE channel_id=?", (channel_id,)))

    # ---------- per-server settings (persona, autoplay, ...) ----------

    async def get_setting(self, guild_id: int, key: str, default: str | None = None) -> str | None:
        row = await self._run(lambda c: c.execute(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key)
        ).fetchone())
        return row[0] if row else default

    async def set_setting(self, guild_id: int, key: str, value: str | None) -> None:
        if value is None:
            await self._run(lambda c: c.execute(
                "DELETE FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key)))
        else:
            await self._run(lambda c: c.execute(
                "INSERT INTO guild_settings(guild_id, key, value) VALUES (?,?,?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            ))

    async def all_settings(self, key: str) -> list[tuple[int, str]]:
        """(guild_id, value) for every server that has this setting."""
        return await self._run(lambda c: c.execute(
            "SELECT guild_id, value FROM guild_settings WHERE key=?", (key,)).fetchall())

    # ---------- reminders ----------

    async def add_reminder(self, user_id: int, guild_id: int | None, channel_id: int | None,
                           due: float, text: str, dm: bool) -> int:
        cur = await self._run(lambda c: c.execute(
            "INSERT INTO reminders(user_id, guild_id, channel_id, due, text, dm, created) VALUES (?,?,?,?,?,?,?)",
            (user_id, guild_id, channel_id, due, text, int(dm), time.time()),
        ))
        return cur.lastrowid

    async def list_reminders(self, user_id: int) -> list[tuple[int, float, str]]:
        return await self._run(lambda c: c.execute(
            "SELECT id, due, text FROM reminders WHERE user_id=? ORDER BY due", (user_id,)).fetchall())

    async def delete_reminder(self, user_id: int, reminder_id: int) -> bool:
        cur = await self._run(lambda c: c.execute(
            "DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id)))
        return cur.rowcount > 0

    async def pop_due_reminders(self, now: float) -> list[dict]:
        def take(c: sqlite3.Connection):
            rows = c.execute(
                "SELECT id, user_id, guild_id, channel_id, due, text, dm FROM reminders WHERE due<=?", (now,)
            ).fetchall()
            c.executemany("DELETE FROM reminders WHERE id=?", [(r[0],) for r in rows])
            return rows
        keys = ("id", "user_id", "guild_id", "channel_id", "due", "text", "dm")
        return [dict(zip(keys, r)) for r in await self._run(take)]

    # ---------- playlists ----------

    async def save_playlist(self, user_id: int, name: str, tracks: list[dict]) -> None:
        data = json.dumps(tracks, ensure_ascii=False)
        await self._run(lambda c: c.execute(
            "INSERT INTO playlists(user_id, name, tracks, created) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET tracks=excluded.tracks, created=excluded.created",
            (user_id, name, data, time.time()),
        ))

    async def get_playlist(self, user_id: int, name: str) -> list[dict] | None:
        row = await self._run(lambda c: c.execute(
            "SELECT tracks FROM playlists WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone())
        return json.loads(row[0]) if row else None

    async def list_playlists(self, user_id: int) -> list[tuple[str, int]]:
        rows = await self._run(lambda c: c.execute(
            "SELECT name, tracks FROM playlists WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall())
        return [(name, len(json.loads(tracks))) for name, tracks in rows]

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        cur = await self._run(lambda c: c.execute(
            "DELETE FROM playlists WHERE user_id=? AND name=?", (user_id, name)))
        return cur.rowcount > 0
