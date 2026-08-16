"""Stage 3: read the draft back and decide whether it actually makes sense.

The writer is optimising for voice — short, casual, in character. That
optimisation is blind to whether the reply is *responsive*. A model that has
just produced three convincing words has no mechanism for noticing they answer
a question nobody asked.

A critic reading the conversation and the draft together is a different task
with a different failure mode, so it catches things the writer cannot see in
itself. Rejections feed back into one retry: the writer is told what was wrong
and tries again. If the second attempt also fails, the reply is dropped —
silence costs nothing, a confusing message costs a real conversation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

SYSTEM = """\
You are checking a draft message before it is sent from {name}'s real Telegram
account. You are not writing anything. You are deciding whether to send.

Reply with PASS or FAIL on the first line, then one short line saying why.

FAIL if any of these are true:

- It does not respond to what was actually said. A non-sequitur.
- It answers a question that was aimed at someone else.
- It repeats something already said in the conversation.
- It invents a fact, an opinion, a plan, or a commitment that {name} has not
  expressed anywhere in this conversation.
- It agrees to or confirms anything — a time, a place, an amount, an action.
- It reads like an assistant: helpful, explanatory, formal, or eager.
- It would make the other person confused, or ask "what do you mean?"
- The tone is wrong for how these people talk to each other.
- It is so vague it carries no meaning — filler like "ok" where the message
  clearly needed an actual answer.

PASS only if a real person reading it in this conversation would notice
nothing odd at all.

If you are unsure, FAIL. A missing reply is invisible. A wrong one is not."""

USER = """\
The conversation so far:
{conversation}

The draft reply, to be sent as {name}:
{reply}

PASS or FAIL, then why:"""


@dataclass
class Critique:
    passed: bool
    reason: str
    raw: str = ""
    error: str = ""

    @property
    def log_reason(self) -> str:
        if self.error:
            return f"critic unavailable: {self.error}"
        return f"rejected by critic: {self.reason}"


def parse(raw: str) -> tuple[bool, str]:
    """Read a PASS/FAIL verdict and its justification out of the response."""
    if not raw:
        return False, "critic returned nothing"

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    if not text:
        return False, "critic returned only reasoning"

    upper = text.upper()
    fail_at = upper.find("FAIL")
    pass_at = upper.find("PASS")

    if fail_at == -1 and pass_at == -1:
        return False, "critic gave no verdict"
    if pass_at == -1:
        passed = False
    elif fail_at == -1:
        passed = True
    else:
        passed = pass_at < fail_at  # whichever it said first

    # everything after the verdict word is the justification
    start = (pass_at if passed else fail_at)
    tail = text[start:].split("\n", 1)
    reason = (tail[1] if len(tail) > 1 else "").strip()
    if not reason:
        reason = re.sub(r"^(PASS|FAIL)\b[\s:.\-–—]*", "", text[start:],
                        flags=re.IGNORECASE).strip()
    reason = " ".join(reason.split())[:200] or ("looks fine" if passed else "no reason given")
    return passed, reason


def format_conversation(messages: list[dict], name: str, limit: int = 10) -> str:
    if not messages:
        return "(nothing)"
    lines = []
    for m in messages[-limit:]:
        who = "them" if m["role"] == "user" else name
        lines.append(f"{who}: {' '.join(m['content'].split())[:300]}")
    return "\n".join(lines)


async def verify(
    backend,
    name: str,
    messages: list[dict],
    reply: str,
    *,
    timeout: int = 45,
) -> Critique:
    """Fails closed: if the critic can't run, the reply doesn't go out."""
    system = SYSTEM.format(name=name)
    user = USER.format(
        conversation=format_conversation(messages, name),
        name=name,
        reply=reply,
    )

    try:
        raw = await asyncio.wait_for(
            backend.chat(system, [{"role": "user", "content": user}]),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("critic timed out after %ss", timeout)
        return Critique(False, "", error=f"timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip() or type(exc).__name__
        log.error("critic call failed: %s", detail)
        return Critique(False, "", error=detail)

    passed, reason = parse(raw)
    return Critique(passed, reason, raw)


def feedback_for(reason: str) -> str:
    """Turn a rejection into an instruction for the retry."""
    return (
        "\n## Your previous attempt was rejected\n\n"
        f"A reviewer read your last draft and rejected it: \"{reason}\"\n\n"
        "Write a different reply that does not have this problem. If you "
        "cannot, output `<skip>`. Do not apologise or explain — just write "
        "the reply, or skip."
    )
