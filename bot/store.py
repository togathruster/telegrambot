"""SQLite persistence: audit log, cooldowns, daily caps, kill switch,
runtime mode overrides, and pending drafts awaiting approval."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    chat_name     TEXT,
    message_id    INTEGER,
    mode          TEXT,
    decision      TEXT    NOT NULL,   -- sent | drafted | skipped | shadow | error
    reason        TEXT,
    incoming_text TEXT,
    reply_text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_chat_ts ON events(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_decision_ts ON events(decision, ts);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_mode (
    chat_id INTEGER PRIMARY KEY,
    mode    TEXT NOT NULL,
    label   TEXT
);

CREATE TABLE IF NOT EXISTS pending (
    token         TEXT PRIMARY KEY,
    ts            TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    chat_name     TEXT,
    reply_to_id   INTEGER,
    draft_msg_id  INTEGER,
    incoming_text TEXT,
    reply_text    TEXT    NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""

# decisions that represent an actual outbound message
SENT_DECISIONS = ("sent",)


@dataclass
class Pending:
    token: str
    chat_id: int
    chat_name: Optional[str]
    reply_to_id: Optional[int]
    draft_msg_id: Optional[int]
    incoming_text: Optional[str]
    reply_text: str


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> "Store":
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.open() was never awaited")
        return self._db

    # ---------------------------------------------------------------- events

    async def log(
        self,
        *,
        chat_id: int,
        chat_name: Optional[str],
        message_id: Optional[int],
        mode: Optional[str],
        decision: str,
        reason: Optional[str] = None,
        incoming_text: Optional[str] = None,
        reply_text: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO events (ts, chat_id, chat_name, message_id, mode,"
            " decision, reason, incoming_text, reply_text)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                chat_id,
                chat_name,
                message_id,
                mode,
                decision,
                reason,
                incoming_text,
                reply_text,
            ),
        )
        await self.db.commit()

    async def recent(self, limit: int = 10, decision: Optional[str] = None):
        sql = "SELECT * FROM events"
        args: tuple = ()
        if decision:
            sql += " WHERE decision = ?"
            args = (decision,)
        sql += " ORDER BY id DESC LIMIT ?"
        async with self.db.execute(sql, (*args, limit)) as cur:
            return await cur.fetchall()

    # ------------------------------------------------------ rate limiting

    async def last_sent_at(self, chat_id: int) -> Optional[datetime]:
        async with self.db.execute(
            "SELECT ts FROM events WHERE chat_id = ? AND decision IN ('sent','drafted')"
            " ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None

    async def recent_replies(self, chat_id: int, limit: int = 6) -> list[str]:
        """What we last said in this chat, newest first.

        Used to stop the model copying its own previous reply — once a short
        filler lands in the history it becomes the most probable next token
        sequence, and it repeats forever.
        """
        async with self.db.execute(
            "SELECT reply_text FROM events WHERE chat_id = ?"
            " AND decision IN ('sent','shadow','drafted')"
            " AND reply_text IS NOT NULL AND reply_text != ''"
            " ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [r["reply_text"] for r in rows]

    async def count_today(self, chat_id: Optional[int] = None) -> int:
        midnight = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        sql = "SELECT COUNT(*) AS n FROM events WHERE decision = 'sent' AND ts >= ?"
        args: tuple = (midnight,)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            args = (midnight, chat_id)
        async with self.db.execute(sql, args) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    # ---------------------------------------------------------- dedupe

    async def mark_seen(self, chat_id: int, message_id: int) -> bool:
        """Returns True if this is the first time we've seen the message."""
        try:
            await self.db.execute(
                "INSERT INTO seen (chat_id, message_id) VALUES (?,?)",
                (chat_id, message_id),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def prune_seen(self, keep: int = 5000) -> None:
        await self.db.execute(
            "DELETE FROM seen WHERE rowid NOT IN"
            " (SELECT rowid FROM seen ORDER BY rowid DESC LIMIT ?)",
            (keep,),
        )
        await self.db.commit()

    # ------------------------------------------------------------- state

    async def get_state(self, key: str, default: Optional[str] = None):
        async with self.db.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO state (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def is_enabled(self) -> bool:
        return await self.get_state("enabled", "1") == "1"

    async def set_enabled(self, on: bool) -> None:
        await self.set_state("enabled", "1" if on else "0")

    # -------------------------------------------------- mode overrides

    async def get_mode_override(self, chat_id: int) -> Optional[str]:
        async with self.db.execute(
            "SELECT mode FROM chat_mode WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["mode"] if row else None

    async def set_mode_override(
        self, chat_id: int, mode: str, label: Optional[str] = None
    ) -> None:
        await self.db.execute(
            "INSERT INTO chat_mode (chat_id, mode, label) VALUES (?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET mode = excluded.mode,"
            " label = COALESCE(excluded.label, chat_mode.label)",
            (chat_id, mode, label),
        )
        await self.db.commit()

    async def clear_mode_override(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chat_mode WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def all_mode_overrides(self):
        async with self.db.execute(
            "SELECT chat_id, mode, label FROM chat_mode ORDER BY mode, chat_id"
        ) as cur:
            return await cur.fetchall()

    # ----------------------------------------------------------- drafts

    async def add_pending(
        self,
        *,
        chat_id: int,
        chat_name: Optional[str],
        reply_to_id: Optional[int],
        incoming_text: Optional[str],
        reply_text: str,
    ) -> str:
        token = secrets.token_hex(2)
        await self.db.execute(
            "INSERT INTO pending (token, ts, chat_id, chat_name, reply_to_id,"
            " incoming_text, reply_text) VALUES (?,?,?,?,?,?,?)",
            (
                token,
                _now(),
                chat_id,
                chat_name,
                reply_to_id,
                incoming_text,
                reply_text,
            ),
        )
        await self.db.commit()
        return token

    async def attach_draft_message(self, token: str, draft_msg_id: int) -> None:
        await self.db.execute(
            "UPDATE pending SET draft_msg_id = ? WHERE token = ?",
            (draft_msg_id, token),
        )
        await self.db.commit()

    async def get_pending(self, token: str) -> Optional[Pending]:
        async with self.db.execute(
            "SELECT * FROM pending WHERE token = ? AND resolved = 0", (token,)
        ) as cur:
            row = await cur.fetchone()
        return self._to_pending(row) if row else None

    async def get_pending_by_message(self, draft_msg_id: int) -> Optional[Pending]:
        async with self.db.execute(
            "SELECT * FROM pending WHERE draft_msg_id = ? AND resolved = 0",
            (draft_msg_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._to_pending(row) if row else None

    async def latest_pending(self) -> Optional[Pending]:
        async with self.db.execute(
            "SELECT * FROM pending WHERE resolved = 0 ORDER BY rowid DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return self._to_pending(row) if row else None

    async def resolve_pending(self, token: str) -> None:
        await self.db.execute(
            "UPDATE pending SET resolved = 1 WHERE token = ?", (token,)
        )
        await self.db.commit()

    async def expire_pending(self, older_than_hours: int = 12) -> int:
        cutoff = (
            datetime.now() - timedelta(hours=older_than_hours)
        ).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "UPDATE pending SET resolved = 1 WHERE resolved = 0 AND ts < ?",
            (cutoff,),
        )
        await self.db.commit()
        return cur.rowcount or 0

    @staticmethod
    def _to_pending(row) -> Pending:
        return Pending(
            token=row["token"],
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            reply_to_id=row["reply_to_id"],
            draft_msg_id=row["draft_msg_id"],
            incoming_text=row["incoming_text"],
            reply_text=row["reply_text"],
        )
