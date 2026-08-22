"""Reading your Telegram history and turning it into the bot's inputs.

Everything here is UI-free on purpose: no printing, no input(). The setup
wizard, the scripts in scripts/, and any future frontend all drive the same
functions and present the results however suits them.

Three products come out of your history:

    examples.jsonl   {"them", "me"} pairs — how you actually reply
    memory table     the same pairs, embedded, for retrieval at reply time
    facts/<id>.md    a prose profile of one chat, written by the local model
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from telethon.tl.types import User

URL_RE = re.compile(r"https?://\S+")
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# 1, not 2: in Chinese a single character ("好", "在", "要") is a whole reply.
# A 2-char floor silently discarded most CJK messages.
MIN_CHARS = 1
MAX_CHARS = 220


def usable(text: str) -> bool:
    text = text.strip()
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if text.startswith("/"):
        return False
    if URL_RE.search(text):
        return False
    return True


def matches(dialog, targets: list[str]) -> bool:
    """True if the dialog matches any target: id, @username, or name substring."""
    name = (dialog.name or "").lower()
    username = (getattr(dialog.entity, "username", None) or "").lower()
    ids = {str(dialog.id), str(dialog.id).lstrip("-")}
    # supergroup ids appear as -100XXXXXXXXXX; accept the bare form too
    if str(dialog.id).startswith("-100"):
        ids.add(str(dialog.id)[4:])
    for t in targets:
        if t in ids or (username and t == username) or (t and t in name):
            return True
    return False


def normalise_targets(chats: list[str]) -> list[str]:
    return [t.strip().lower().lstrip("@") for t in chats if t.strip()]


@dataclass
class ChatInfo:
    """A chat the user could pick, with enough detail to choose sensibly."""

    id: int
    name: str
    username: Optional[str] = None
    is_group: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}" + (f"  @{self.username}" if self.username else "")


@dataclass
class Harvest:
    """Pairs from one chat, plus why the count is what it is.

    The counts exist to answer "why did I get nothing?" — the difference
    between 'you never post here' and 'you never use reply' needs different
    fixes, and without these numbers both look identical.
    """

    pairs: list[dict] = field(default_factory=list)
    scanned: int = 0
    mine: int = 0
    replies: int = 0
    orphan: int = 0
    filtered: int = 0

    def explain(self) -> str:
        detail = f"scanned {self.scanned}, {self.mine} from you"
        if self.replies:
            detail += f", {self.replies} were replies"
        if self.orphan:
            detail += f", {self.orphan} pointed past the scan window"
        if self.filtered:
            detail += f", {self.filtered} dropped by the length/url filter"
        return detail


def is_group_dialog(dialog) -> bool:
    return bool(dialog.is_group and not dialog.is_channel)


def wanted(dialog, groups: Optional[bool]) -> bool:
    """Whether a dialog is the kind we are scanning at all.

    groups=True groups only, False private chats only, None both.
    """
    if is_group_dialog(dialog):
        return groups is not False
    if groups is True:
        return False
    return isinstance(dialog.entity, User) and not dialog.entity.bot


async def list_chats(client, limit: int = 40,
                     groups: Optional[bool] = True) -> list[ChatInfo]:
    """Candidate chats to learn from, most recently active first."""
    out: list[ChatInfo] = []
    async for dialog in client.iter_dialogs():
        if len(out) >= limit:
            break
        if not wanted(dialog, groups):
            continue
        out.append(
            ChatInfo(
                id=dialog.id,
                name=dialog.name or str(dialog.id),
                username=getattr(dialog.entity, "username", None),
                is_group=is_group_dialog(dialog),
            )
        )
    return out


def extract_pairs(
    msgs: list,
    is_group: bool,
    loose: bool = False,
    limit: Optional[int] = None,
) -> Harvest:
    """Find places where someone spoke and you answered.

    In a group the message before yours is usually unrelated, so only explicit
    reply markers are trusted — you replied to them, so that IS the pair.
    `loose` falls back to adjacency for people who rarely hit reply, and is
    noisier by construction.
    """
    h = Harvest(scanned=len(msgs), mine=sum(1 for m in msgs if m.out))

    def room() -> bool:
        return limit is None or len(h.pairs) < limit

    def add(prev, cur) -> bool:
        them, me = (prev.message or "").strip(), (cur.message or "").strip()
        if usable(them) and usable(me):
            h.pairs.append({"them": them, "me": me, "ts": str(cur.date)})
            return True
        return False

    if is_group:
        by_id = {m.id: m for m in msgs}
        for cur in msgs:
            if not room():
                break
            if not cur.out or not cur.reply_to_msg_id:
                continue
            h.replies += 1
            prev = by_id.get(cur.reply_to_msg_id)
            if prev is None:
                h.orphan += 1  # the message it answered is older than the scan
                continue
            if prev.out:
                continue
            if not add(prev, cur):
                h.filtered += 1

        if not loose:
            return h

    for prev, cur in zip(msgs, msgs[1:]):
        if not room():
            break
        if prev.out or not cur.out:
            continue
        if is_group and cur.reply_to_msg_id:
            continue  # already counted above
        add(prev, cur)
    return h


async def read_messages(client, dialog, scan: int) -> list:
    """History for one chat, oldest first — the order everything here expects."""
    msgs = [m async for m in client.iter_messages(dialog, limit=scan)]
    msgs.reverse()
    return msgs


# ----------------------------------------------------------------- examples


def write_examples(pairs: list[dict], path: Path, append: bool = False) -> int:
    """examples.jsonl: one {"them", "me"} object per line.

    The timestamp carried by extract_pairs is dropped here — it is only used
    for ordering memory, and examples are quoted as style, not as history.
    """
    lines = "\n".join(
        json.dumps({"them": p["them"], "me": p["me"]}, ensure_ascii=False)
        for p in pairs
    )
    if append and path.exists():
        existing = path.read_text(encoding="utf-8").rstrip("\n")
        body = f"{existing}\n{lines}\n" if lines else f"{existing}\n"
    else:
        body = lines + "\n"
    path.write_text(body, encoding="utf-8")
    return len(pairs)


# ------------------------------------------------------------------- memory


async def store_memory(store, embedder, chat_id: int, chat_name: str,
                       pairs: list[dict]) -> int:
    """Embed pairs and put them in the memory table. Returns how many stuck."""
    from .retrieve import pack

    stored = 0
    for p in pairs:
        vec = await embedder.embed(p["them"])
        if vec is None:
            continue
        await store.db.execute(
            "INSERT INTO memory (chat_id, chat_name, them, me, ts, vec, dim)"
            " VALUES (?,?,?,?,?,?,?)",
            (chat_id, chat_name, p["them"], p["me"], p.get("ts", ""),
             pack(vec), len(vec)),
        )
        stored += 1
    await store.db.commit()
    return stored


# ------------------------------------------------------------------ profile

PROFILE_SYSTEM = """\
You are reading a transcript of a group chat to build a short factual profile
for someone called {name}.

Write plain markdown with these sections, and nothing else:

## Who is in this chat
One line per person who speaks often: their name and anything durable and
useful about them (what they do, their relationship to {name}, what they
usually talk about).

## Recurring topics
The things this group actually discusses, as a short list.

## How {name} talks here
How {name}'s messages in this chat differ from how they might write elsewhere:
language, formality, in-jokes, nicknames.

Rules:
- Only write what the transcript supports. Do not guess or invent.
- Leave a section out entirely if the transcript does not support it.
- No preamble, no closing remarks, no meta-commentary about the transcript.
"""


def transcript(msgs: list, name: str, limit: int = 400) -> str:
    """Flatten history into 'who: what' lines for the profiling model."""
    lines = []
    for m in msgs[-limit:]:
        text = (m.message or "").strip()
        if not text:
            continue
        if m.out:
            who = name
        else:
            s = m.sender
            who = (
                getattr(s, "first_name", None)
                or getattr(s, "username", None)
                or str(m.sender_id)
            )
        lines.append(f"{who}: {' '.join(text.split())[:200]}")
    return "\n".join(lines)


async def write_profile(backend, facts_dir: Path, chat_id: int, chat_name: str,
                        text: str, my_name: str) -> Optional[Path]:
    """Ask the local model to profile one chat and write facts/<chat_id>.md.

    One file per chat. Facts are per-chat by nature — who "Wei" is and what the
    running joke means do not transfer, and a shared file would have the bot
    referencing things nobody in the current conversation has ever said.
    """
    out = await backend.chat(
        PROFILE_SYSTEM.format(name=my_name),
        [{"role": "user", "content": text[:20000]}],
    )
    out = THINK_RE.sub("", out).strip()
    if not out:
        return None

    facts_dir.mkdir(exist_ok=True)
    path = facts_dir / f"{chat_id}.md"
    path.write_text(
        f"<!-- {chat_name} (id {chat_id}) — generated "
        f"{datetime.now():%Y-%m-%d}. Edit freely; the bot re-reads on "
        f"change. -->\n\n{out}\n",
        encoding="utf-8",
    )
    return path


def write_facts_index(facts_dir: Path, written: list[tuple[int, str]]) -> Path:
    index = facts_dir / "README.md"
    lines = [
        "# facts/",
        "",
        "One file per chat, named by chat id. `_global.md` (if you create it) "
        "is prepended to every prompt — put things about yourself there, not "
        "about any one group.",
        "",
        "| chat id | chat |",
        "|---|---|",
    ]
    lines += [f"| `{cid}` | {name} |" for cid, name in written]
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index
