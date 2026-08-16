"""A queue of messages awaiting a reply, and the policy for draining it.

Replacing immediate per-message handling with a queue fixes several things at
once:

**Cooldown stops destroying messages.** Previously a message arriving inside
the cooldown window was dropped and never answered. Now it waits.

**Bursts get one answer, not four.** Someone firing off three messages in ten
seconds is having one thought. A human reads all three and replies once. The
queue coalesces everything pending for a chat and answers the newest.

**Generation latency stops mattering.** Slow replies pile up in a table
instead of in per-chat locks, and the worker drains at a human pace.

**Restarts don't lose anything.** The queue is a SQLite table, so pending
work survives a crash — bounded by expiry, since answering a 90-minute-old
"lol" is worse than staying quiet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    enqueued_at  TEXT    NOT NULL,
    chat_id      INTEGER NOT NULL,
    chat_name    TEXT,
    message_id   INTEGER NOT NULL,
    sender       TEXT,
    text         TEXT    NOT NULL,
    is_group     INTEGER NOT NULL DEFAULT 0,
    priority     INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, chat_id);
"""

# higher wins when choosing which chat to serve next
PRIORITY_DM = 30
PRIORITY_REPLY = 20
PRIORITY_MENTION = 10


@dataclass
class Item:
    id: int
    enqueued_at: datetime
    chat_id: int
    chat_name: str
    message_id: int
    sender: str
    text: str
    is_group: bool
    priority: int


@dataclass
class Batch:
    """Everything pending for one chat, oldest first."""

    chat_id: int
    chat_name: str
    items: list[Item]

    @property
    def newest(self) -> Item:
        return self.items[-1]

    @property
    def oldest(self) -> Item:
        return self.items[0]

    @property
    def priority(self) -> int:
        return max(i.priority for i in self.items)

    @property
    def ids(self) -> list[int]:
        return [i.id for i in self.items]

    def combined_text(self) -> str:
        """All queued messages as one block, for classification.

        A burst is one thought; classifying only the last line misses the
        message before it that mentioned money.
        """
        return "\n".join(i.text for i in self.items)


def _row_to_item(row) -> Item:
    return Item(
        id=row["id"],
        enqueued_at=datetime.fromisoformat(row["enqueued_at"]),
        chat_id=row["chat_id"],
        chat_name=row["chat_name"] or "",
        message_id=row["message_id"],
        sender=row["sender"] or "",
        text=row["text"],
        is_group=bool(row["is_group"]),
        priority=row["priority"],
    )


class Queue:
    def __init__(self, db) -> None:
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def push(
        self,
        *,
        chat_id: int,
        chat_name: str,
        message_id: int,
        sender: str,
        text: str,
        is_group: bool,
        priority: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """Enqueue a message. False if it was already queued."""
        now = now or datetime.now()
        try:
            await self.db.execute(
                "INSERT INTO queue (enqueued_at, chat_id, chat_name, message_id,"
                " sender, text, is_group, priority) VALUES (?,?,?,?,?,?,?,?)",
                (
                    now.isoformat(timespec="seconds"),
                    chat_id,
                    chat_name,
                    message_id,
                    sender,
                    text,
                    int(is_group),
                    priority,
                ),
            )
            await self.db.commit()
            return True
        except Exception:  # noqa: BLE001  (UNIQUE violation)
            return False

    async def pending(self) -> list[Batch]:
        """Everything waiting, grouped by chat."""
        async with self.db.execute(
            "SELECT * FROM queue WHERE status = 'pending' ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()

        grouped: dict[int, list[Item]] = {}
        for row in rows:
            grouped.setdefault(row["chat_id"], []).append(_row_to_item(row))

        return [
            Batch(chat_id=cid, chat_name=items[-1].chat_name, items=items)
            for cid, items in grouped.items()
        ]

    async def resolve(self, ids: list[int], status: str = "done") -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        await self.db.execute(
            f"UPDATE queue SET status = ? WHERE id IN ({marks})", (status, *ids)
        )
        await self.db.commit()

    async def expire(self, max_age_minutes: int, now: Optional[datetime] = None) -> int:
        now = now or datetime.now()
        cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat(
            timespec="seconds"
        )
        cur = await self.db.execute(
            "UPDATE queue SET status = 'expired'"
            " WHERE status = 'pending' AND enqueued_at < ?",
            (cutoff,),
        )
        await self.db.commit()
        return cur.rowcount or 0

    async def prune(self, keep_hours: int = 48) -> None:
        cutoff = (datetime.now() - timedelta(hours=keep_hours)).isoformat(
            timespec="seconds"
        )
        await self.db.execute(
            "DELETE FROM queue WHERE status != 'pending' AND enqueued_at < ?",
            (cutoff,),
        )
        await self.db.commit()

    async def depth(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) n FROM queue WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"])


def is_ready(
    batch: Batch,
    *,
    debounce_seconds: int,
    max_wait_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """Has this chat settled enough to answer?

    Two clocks. The debounce waits for a quiet gap after the *newest* message,
    so a burst still being typed isn't answered halfway through. The max wait
    measures from the *oldest*, so a chat where someone keeps typing forever
    still gets answered eventually.
    """
    now = now or datetime.now()
    since_newest = (now - batch.newest.enqueued_at).total_seconds()
    since_oldest = (now - batch.oldest.enqueued_at).total_seconds()
    return since_newest >= debounce_seconds or since_oldest >= max_wait_seconds


def choose(batches: list[Batch], now: Optional[datetime] = None) -> Optional[Batch]:
    """Which chat to serve next: highest priority, then longest waiting."""
    if not batches:
        return None
    return sorted(
        batches,
        key=lambda b: (-b.priority, b.oldest.enqueued_at),
    )[0]


def should_use_reply_to(messages_since: int, threshold: int) -> bool:
    """Whether to attach a Telegram reply, or just send a plain message.

    Replying inline to something said five seconds ago reads as robotic — in a
    live exchange people simply talk. Reply-to is for when the conversation has
    moved on and it would otherwise be unclear what you're answering.
    """
    return messages_since > threshold
