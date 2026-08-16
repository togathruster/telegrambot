"""Stage 1: decide whether a message is safe to answer at all.

Separating this from writing is the whole point. Asking one model to be
careful *and* casual in a single prompt makes it bad at both — the caution
rules bleed into the voice, and the casual instructions undermine the caution.

Classification is also a much easier task than generation, which is why a 3-4B
model does it reliably while a 14B struggles to police itself mid-sentence.

Only labels in the allow-list reach the writer. Everything else is silence,
recorded with the label that stopped it, so the log tells you what your chats
are actually made of.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

LABELS = ("SAFE", "FACT", "MONEY", "PLAN", "SENSITIVE", "OTHER")

SYSTEM = """\
You are a filter. You do not reply to messages. You label them.

Someone has just addressed {name} in a group chat. Decide what kind of message
it is, so a separate system can decide whether to auto-reply as {name}.

Answer with exactly ONE of these words and nothing else:

SAFE       - a joke, banter, a reaction, small talk, an acknowledgement, or
             something already settled by others. Answering costs nothing even
             if the answer is slightly wrong.
FACT       - needs information only {name} has. What he did, where he is, what
             he decided, whether something happened, what he thinks about a
             specific thing he hasn't already said here.
MONEY      - money, prices, debts, payments, splitting, owing, salaries, what
             something cost, what someone can afford.
PLAN       - a time, a date, availability, meeting up, attending, confirming
             or declining anything, anything that commits {name} to something.
SENSITIVE  - health, mood, mental state, relationships, dating, family, an
             argument, or anyone being upset. Including as a joke.
OTHER      - anything that fits none of the above, or you are unsure.

When a message could be two labels, choose the more cautious one. SAFE is the
least cautious label — only use it when you are confident.

Reply with one word."""

USER = """\
Recent conversation:
{history}

The message to label:
{sender}: {text}

One word:"""


@dataclass
class Verdict:
    label: str
    allowed: bool
    raw: str = ""

    @property
    def reason(self) -> str:
        return f"classifier: {self.label}"


def parse_label(raw: str) -> str:
    """Pull a label out of whatever the small model actually said.

    They ignore "one word only" surprisingly often, answering "SAFE - this is
    just banter" or "The label is PLAN". Take the first label that appears.
    """
    if not raw:
        return "OTHER"
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    upper = text.upper()

    best: Optional[tuple[int, str]] = None
    for label in LABELS:
        pos = upper.find(label)
        if pos != -1 and (best is None or pos < best[0]):
            best = (pos, label)
    return best[1] if best else "OTHER"


def format_history(messages: list[dict], limit: int = 6) -> str:
    if not messages:
        return "(nothing yet)"
    lines = []
    for m in messages[-limit:]:
        who = "them" if m["role"] == "user" else "him"
        content = " ".join(m["content"].split())
        lines.append(f"{who}: {content[:200]}")
    return "\n".join(lines)


async def classify(
    backend,
    name: str,
    text: str,
    sender: str,
    history: list[dict],
    allow: set[str],
    *,
    timeout: int = 30,
) -> Verdict:
    """Label the message. On any failure, fail closed — silence, not a guess."""
    system = SYSTEM.format(name=name)
    user = USER.format(
        history=format_history(history),
        sender=sender or "them",
        text=" ".join(text.split())[:500],
    )

    try:
        raw = await asyncio.wait_for(
            backend.chat(system, [{"role": "user", "content": user}]),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("classifier timed out after %ss", timeout)
        return Verdict("OTHER", False, "timeout")
    except Exception as exc:  # noqa: BLE001
        log.exception("classifier failed")
        return Verdict("OTHER", False, f"error: {exc}")

    label = parse_label(raw)
    return Verdict(label, label in allow, raw)
