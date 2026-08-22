"""Assembles the prompt: persona + few-shot examples + recent chat history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

MAX_MESSAGE_CHARS = 600

SYSTEM_TEMPLATE = """\
{persona}

## How this works

You are drafting a reply that will be sent from {name}'s own Telegram account,
as if {name} typed it. You are not an assistant and you are not talking to
{name} — you are talking to their contact, as them.

## Hard rules

- Never agree to a meeting, a call, a date, or a time. Never confirm plans.
- Never discuss money, payments, invoices, salaries, or anything financial.
- Never make a promise or a commitment on {name}'s behalf.
- Never share addresses, passwords, codes, or any personal detail not already
  visible in this conversation.
- Never claim to have done something you cannot know {name} has done.
- If the message needs a decision, a fact you do not have, or carries any
  emotional weight, output exactly `<skip>` and nothing else.
- If you are less than confident the reply is right, output `<skip>`.

`<skip>` is always a safe answer. A missed reply costs nothing. A wrong reply
is sent from {name}'s real account and cannot be taken back.

## Style

- Write the way the section above says {name} writes. It was built from their
  real messages, so follow it over any instinct about what reads well.
- Do not tidy anything. Keep their spelling, their shorthand, their
  capitalisation, and their missing punctuation. If they write `u` and never
  end a message with a full stop, neither do you. Cleaning up how someone
  writes is the clearest sign a model wrote the message.
- Fragments quoted above are proof of a habit, not lines to reuse.
- One message, as long as {name} would actually make it. Most replies are a
  few words.
- No greeting unless the conversation just started, and no sign-off ever.
- Start with the reply. Do not open by acknowledging the message: no `sure`,
  `got it`, `of course`, or `haha yeah` bolted on the front.
- Do not repeat their question back, do not explain your answer, and never
  offer to help further.
- Never use em-dashes. Never sound like a chatbot or customer service.
- No emoji unless the other person is using them.
- Do not explain yourself, do not add meta-commentary, do not use quotes.

Output the reply text only, or `<skip>`.
{group_rules}{chat_notes}"""

GROUP_RULES = """
## This is a group chat

Incoming lines are prefixed with who said them. You are only here because
someone mentioned {name} or replied to them directly.

- Answer only the person who addressed {name}. Ignore everything else.
- If the question was aimed at someone else in the group, output `<skip>`.
- If other people have already answered it, output `<skip>`.
- Never address the group as a whole. Never summarise the conversation.
- Do not use anyone's name unless it's needed to be clear.
- A group notices a long reply more than a DM does, so lean short — but not
  at the cost of being unclear. Say what the answer needs and stop.
"""


@dataclass
class Msg:
    """One line of conversation. `mine` means it was sent by the account owner."""

    mine: bool
    text: str
    sender: str = ""


def load_persona(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return "You are replying on behalf of a Telegram user."
    return p.read_text(encoding="utf-8").strip()


def load_examples(path: str | Path, limit: int = 10) -> list[dict]:
    """examples.jsonl: one {"them": "...", "me": "..."} object per line."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("them") and obj.get("me"):
            out.append(obj)
        if len(out) >= limit:
            break
    return out


def format_examples(examples: list[dict], name: str) -> str:
    """Render examples as a style reference inside the system prompt.

    They deliberately do NOT go into the message array as conversation turns.
    Sitting there, the model cannot tell them apart from things it said a
    moment ago, and answers by retrieving the nearest one verbatim instead of
    writing anything. Quoted here, they show register without being copyable
    as a previous turn.
    """
    if not examples:
        return ""
    lines = [
        f"\n## How {name} has written in the past\n",
        "Old exchanges, quoted only to show register — rhythm, length, "
        "spelling, capitalisation. They are NOT part of the conversation you "
        "are in and you must never reuse their wording. If your reply matches "
        "one of these, you are copying rather than answering: write something "
        "else or output `<skip>`.\n",
    ]
    for ex in examples:
        lines.append(f"- them: {_truncate(ex['them'])}\n  {name.lower()}: {_truncate(ex['me'])}")
    return "\n".join(lines) + "\n"


class FactsStore:
    """Per-chat background, loaded from a facts/ directory.

        facts/_global.md        true everywhere — about the account owner
        facts/<chat_id>.md      that chat only — who's in it, running jokes
        facts.md                legacy single file, treated as global

    Facts are per-chat by nature: who "Wei" is, what the running joke means,
    what this group is for. A single shared file would leak one group's
    context into another and invite the model to reference things nobody in
    the current chat has ever said.

    Files are re-read when their mtime changes, so you can edit them while
    the bot runs.
    """

    def __init__(self, root: str | Path) -> None:
        self.dir = Path(root) / "facts"
        self.legacy = Path(root) / "facts.md"
        self._cache: dict[str, tuple[float, str]] = {}

    def _read(self, path: Path) -> str:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._cache.pop(str(path), None)
            return ""
        cached = self._cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        self._cache[str(path)] = (mtime, text)
        return text

    def global_facts(self) -> str:
        return self._read(self.dir / "_global.md") or self._read(self.legacy)

    def chat_facts(self, chat_id: int) -> str:
        return self._read(self.dir / f"{chat_id}.md")

    def get(self, chat_id: Optional[int] = None) -> str:
        """Global facts plus this chat's, in that order."""
        parts = [self.global_facts()]
        if chat_id is not None:
            parts.append(self.chat_facts(chat_id))
        return "\n\n".join(p for p in parts if p).strip()

    def available(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name for p in self.dir.glob("*.md"))


def build_system_prompt(
    persona: str,
    name: str,
    chat_notes: str = "",
    is_group: bool = False,
    examples: Optional[list[dict]] = None,
    facts: str = "",
    retrieved: str = "",
) -> str:
    notes = ""
    if chat_notes.strip():
        notes = f"\n## About this specific chat\n\n{chat_notes.strip()}\n"
    group_rules = GROUP_RULES.format(name=name) if is_group else ""
    base = SYSTEM_TEMPLATE.format(
        persona=persona, name=name, group_rules=group_rules, chat_notes=notes
    )

    parts = [base]
    if facts.strip():
        parts.append(
            f"\n## Background on these people\n\n"
            f"Assembled from past conversations. Treat as probably-true "
            f"context, not gospel.\n\n{facts.strip()}\n"
        )
    parts.append(format_examples(examples or [], name))
    if retrieved.strip():
        parts.append(retrieved)
    return "".join(p for p in parts if p)


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1] + "…"


def build_messages(
    history: Iterable[Msg],
    include_names: bool = False,
) -> list[dict]:
    """The live conversation only — nothing synthetic.

    Style examples live in the system prompt (see `format_examples`). Putting
    them here as fake turns made the model treat them as its own recent
    messages and repeat them verbatim.

    Consecutive messages from the same side are merged, because models handle
    strictly alternating turns far better than ragged ones.

    `include_names` prefixes each incoming line with its sender. Essential in
    group chats: without it every participant collapses into one "user" voice
    and the model cannot tell who it is answering.
    """
    merged: list[dict] = []
    for m in history:
        text = _truncate(m.text)
        if not text:
            continue
        if include_names and not m.mine and m.sender:
            text = f"{m.sender}: {text}"
        role = "assistant" if m.mine else "user"
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + text
        else:
            merged.append({"role": role, "content": text})

    # The model must be answering something, so the last turn has to be theirs.
    while merged and merged[-1]["role"] == "assistant":
        merged.pop()

    return merged
