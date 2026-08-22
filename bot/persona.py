"""Writing persona.md from your own messages, using the local model.

This replaces the "paste examples.jsonl into an LLM by hand" step. The model
never sees anything that did not come off your machine.

persona.md matters more than any setting in the project, so nothing here
writes it without the caller confirming: draft_persona() returns text and the
caller decides. See bot/setup.py for the review gate.
"""

from __future__ import annotations

import re
from pathlib import Path

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

PERSONA_SYSTEM = """\
You are studying how one person, {name}, writes messages, and producing the
instructions another model will follow to write as them.

The evidence below is real. `them:` is what someone said to {name}; `{lower}:`
is what {name} actually wrote back. It is everything you know about them.

Write markdown, addressed to the model that will do the writing. The first
line of your output must be exactly:

You are writing as {name}.

Then these sections:

## Who they are
Only what the messages prove: who they talk to and what about. Three sentences
at most. Name the subjects they actually discuss. Do not characterise their
tone or personality here; that belongs below, with proof.

## How they write
The part that matters. Every line is a rule the writer can follow, and every
rule carries a real fragment from the evidence, in backticks, as proof. Work
out from the evidence:

- how long a reply runs, and what makes one longer than a word
- what they put at the end of a message, if anything
- how they spell the words they use most
- their capitalisation, including where it contradicts itself
- the shorthand and slang they use
- how they laugh, and whether it is capitalised
- whether a single message in the evidence contains an emoji at all
- swearing, if any
- whether replies stand alone or only parse against the previous message
- which languages they use, and what makes them switch

## Never
What the evidence shows they never do. Write each one as a command to the
writer, starting with `Never` or `Do not` — not as a description of {name},
and never as a double negative.

Rules:
- Quote the evidence. A rule you cannot prove with a fragment does not go in.
- Write only what is in the evidence above. Never add a habit because it is
  common in messaging, and never carry over an example from these
  instructions.
- The words "casual", "informal", "conversational" and "friendly" must not
  appear anywhere in your output. They are true of everyone and describe
  nobody. The same goes for any other line that would fit most people.
- Where they are inconsistent, say so. Do not pick the tidier answer.
- If a fragment is garbled, cut off, or you cannot tell what it meant, leave
  it out rather than inventing a reading for it.
- Output the markdown only. No preamble, no notes about the evidence.
"""


def format_evidence(examples: list[dict], name: str, limit: int = 120) -> str:
    """Render the harvested pairs as evidence for the drafting model."""
    lower = name.lower()
    lines = []
    for ex in examples[:limit]:
        them = " ".join(str(ex.get("them", "")).split())
        me = " ".join(str(ex.get("me", "")).split())
        if not them or not me:
            continue
        lines.append(f"them: {them}\n{lower}: {me}")
    return "\n\n".join(lines)


async def draft_persona(backend, examples: list[dict], name: str,
                        limit: int = 120) -> str:
    """Draft persona.md text. Returns "" if the model gave nothing usable."""
    evidence = format_evidence(examples, name, limit)
    if not evidence:
        return ""
    out = await backend.chat(
        PERSONA_SYSTEM.format(name=name, lower=name.lower()),
        [{"role": "user", "content": evidence[:20000]}],
    )
    return THINK_RE.sub("", out).strip()


def save_persona(text: str, path: Path) -> Path:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


# The prompt forbids all of this, but a 14B follows negative constraints
# unevenly — three runs on the same evidence produced "informal" twice and an
# invented emoji habit twice. Checking the output is cheaper than trusting the
# instruction, so the review gate gets told what to look at.
GENERIC_WORDS = ("casual", "informal", "conversational", "friendly")


def _has_emoji(text: str) -> bool:
    return any(
        0x1F300 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF
        for c in text
    )


def suspect_lines(text: str, examples: list[dict]) -> list[tuple[str, str]]:
    """Lines in a drafted persona that the evidence does not back up."""
    corpus = " ".join(str(e.get("me", "")) for e in examples)
    emoji_used = _has_emoji(corpus)
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        hit = next((w for w in GENERIC_WORDS if w in low), None)
        if hit:
            out.append((line, f'says "{hit}", which is true of everyone'))
        elif "emoji" in low and not emoji_used and not any(
            n in low for n in ("never", "no emoji", "not use emoji", "without emoji")
        ):
            out.append((line, "claims emoji; none appear in your messages"))
    return out
