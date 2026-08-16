"""Retrieval over your own chat history.

The writer's core problem is missing facts. Static examples show it *how* you
write; retrieval shows it how you answered *this kind of message before* —
which is the closest thing to knowing your position without asking you.

Vectors live in the same SQLite file as everything else. Brute-force cosine
over a few thousand rows takes single-digit milliseconds, so there is no
index, no vector database, and nothing else to install or keep running.

Retrieved exchanges go into the system prompt as labelled reference material,
never into the message array. Turns get copied verbatim; that lesson cost a
day of debugging already.
"""

from __future__ import annotations

import array
import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    chat_name TEXT,
    them      TEXT NOT NULL,
    me        TEXT NOT NULL,
    ts        TEXT,
    vec       BLOB NOT NULL,
    dim       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_chat ON memory(chat_id);
"""


@dataclass
class Exchange:
    them: str
    me: str
    score: float
    chat_name: str = ""


def pack(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return list(vec)
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both sides are stored normalised, so this is just a dot product."""
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """Wraps an Ollama embedding model. Vectors are normalised on the way out."""

    def __init__(self, host: str, model: str = "nomic-embed-text") -> None:
        from ollama import AsyncClient

        self.client = AsyncClient(host=host)
        self.model = model

    async def embed(self, text: str) -> Optional[list[float]]:
        text = " ".join(text.split())
        if not text:
            return None
        try:
            resp = await self.client.embeddings(model=self.model, prompt=text)
        except Exception:  # noqa: BLE001
            log.exception("embedding failed")
            return None
        vec = resp.get("embedding") or []
        return normalise(vec) if vec else None


async def search(
    db,
    embedder: Embedder,
    query: str,
    *,
    k: int = 4,
    chat_id: Optional[int] = None,
    min_score: float = 0.45,
) -> list[Exchange]:
    """Top-k past exchanges most similar to `query`.

    `chat_id` restricts to one chat. Leaving it None searches everything, which
    is usually what you want — how you answered a question in one group is
    still evidence for how you'd answer it in another.
    """
    qvec = await embedder.embed(query)
    if qvec is None:
        return []

    sql = "SELECT chat_name, them, me, vec FROM memory"
    args: tuple = ()
    if chat_id is not None:
        sql += " WHERE chat_id = ?"
        args = (chat_id,)

    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()

    scored: list[Exchange] = []
    for row in rows:
        vec = unpack(row["vec"])
        if len(vec) != len(qvec):
            continue  # embedding model changed since this row was written
        score = cosine(qvec, vec)
        if score >= min_score:
            scored.append(
                Exchange(
                    them=row["them"],
                    me=row["me"],
                    score=score,
                    chat_name=row["chat_name"] or "",
                )
            )

    scored.sort(key=lambda e: e.score, reverse=True)
    return scored[:k]


def format_retrieved(exchanges: list[Exchange], name: str) -> str:
    """Render retrieved exchanges for the system prompt."""
    if not exchanges:
        return ""
    lines = [
        f"\n## Similar things {name} has been asked before\n",
        "Pulled from his history because they resemble the message you are "
        "answering now. Use them to work out his actual position — what he "
        "tends to say, what he avoids, how much he gives away. Do not copy "
        "the wording, and do not assume a past answer is still true.\n",
    ]
    for e in exchanges:
        them = " ".join(e.them.split())[:200]
        me = " ".join(e.me.split())[:200]
        lines.append(f"- them: {them}\n  {name.lower()}: {me}")
    return "\n".join(lines) + "\n"
