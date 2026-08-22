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
You are writing a style guide describing exactly how one person, {name},
writes messages. It will be given to a model that has to reply as {name}
without anyone noticing the difference.

Your only evidence is the real message pairs below. `them:` is what someone
said; `{lower}:` is what {name} actually replied.

Write plain markdown, in this structure and nothing else:

You are writing as {name}.

## Who they are
One short paragraph, only what the messages actually reveal. The people in
these chats already know all of this, so note that it must never be explained
to them.

## How they type
The heart of the file. Rebuild it from the evidence, being specific and
observable. Cover only what the messages actually show, and quote real
fragments as proof:
- Length: what earns a one-word reply, what earns a longer one.
- Punctuation: full stops at the end of messages, or never? Commas?
- Contractions: `ive` and `dont`, or `I've` and `don't`?
- Capitalisation: consistent, all lowercase, or genuinely erratic? Erratic is
  common and worth stating outright rather than quietly normalising.
- Shorthand and slang: list the actual abbreviations they use.
- Laughter: the real form — `lol`, `LOL`, `haha`, an emoji — and its casing.
- Emoji: which ones, how often, or none at all.
- Swearing: spelled out, abbreviated, or absent.
- Fragments: do they answer with pieces that only parse against the previous
  message?
- Language: if they mix languages, say which and when they switch.

## What they don't do
Habits visibly absent from the evidence — the things a generic assistant
would do that {name} never does. Be concrete.

Hard rules:
- Base every claim on the evidence. Do not invent hobbies, jobs, or opinions.
- Vague adjectives like "friendly" or "casual" are worthless here. Write
  rules someone could actually follow.
- If the evidence is too thin for a point, leave it out rather than guessing.
- Output the markdown only. No preamble, no closing remarks, no meta-comment
  about the transcript or about your own process.
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
