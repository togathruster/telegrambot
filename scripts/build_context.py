#!/usr/bin/env python3
"""Scan your history, embed it, and build a profile the bot can use.

    # everything: 20 chats, 3000 messages each
    python -m scripts.build_context --dialogs 20 --scan 3000

    # one group, deeper
    python -m scripts.build_context --chat "uni bois" --scan 8000

    # also write facts.md using the local model
    python -m scripts.build_context --chat "uni bois" --scan 5000 --profile

Two outputs:

  memory table   every exchange where you replied to someone, with an
                 embedding, so the bot can retrieve how you answered similar
                 messages before.

  facts.md       an optional prose profile: who is in the group, recurring
                 topics, how you talk to them. Injected into every prompt.

Nothing leaves your machine. Read facts.md before using it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import Env, resolve_session  # noqa: E402
from bot.retrieve import SCHEMA, Embedder, pack  # noqa: E402
from bot.store import Store  # noqa: E402

URL_RE = re.compile(r"https?://\S+")
FACTS_DIR = ROOT / "facts"

PROFILE_SYSTEM = """\
You are reading a transcript of a group chat to build a short factual profile
for someone called {name}.

Write plain markdown with these sections, and nothing else:

## Who is in this chat
One line per person who speaks often: their name and anything durable and
useful about them (what they do, their relationship to {name}, what they
usually talk about).

## Recurring topics
What this group actually talks about, in five bullets or fewer.

## Running jokes and shared references
Things that would confuse an outsider. Keep it short.

## How {name} talks to these people
Two or three lines. Register, how much he gives away, what he deflects.

Rules:
- Only write things clearly supported by the transcript.
- Do not invent. If a section has nothing, write "nothing clear".
- Do not include anyone's financial details, health, or relationship problems.
- No preamble, no conclusion, just the sections."""


def usable(text: str) -> bool:
    text = text.strip()
    if not (1 <= len(text) <= 300):
        return False
    if text.startswith("/") or URL_RE.search(text):
        return False
    return True


def matches(dialog, targets: list[str]) -> bool:
    name = (dialog.name or "").lower()
    username = (getattr(dialog.entity, "username", None) or "").lower()
    ids = {str(dialog.id), str(dialog.id).lstrip("-")}
    if str(dialog.id).startswith("-100"):
        ids.add(str(dialog.id)[4:])
    return any(
        t in ids or (username and t == username) or (t and t in name) for t in targets
    )


def extract_pairs(msgs: list, is_group: bool) -> list[dict]:
    """Exchanges where you answered someone.

    Groups only trust explicit replies; in a DM the previous message is
    reliably the one you were answering.
    """
    pairs: list[dict] = []
    if is_group:
        by_id = {m.id: m for m in msgs}
        for cur in msgs:
            if not cur.out or not cur.reply_to_msg_id:
                continue
            prev = by_id.get(cur.reply_to_msg_id)
            if prev is None or prev.out:
                continue
            them, me = (prev.message or "").strip(), (cur.message or "").strip()
            if usable(them) and usable(me):
                pairs.append({"them": them, "me": me, "ts": str(cur.date)})
    else:
        for prev, cur in zip(msgs, msgs[1:]):
            if prev.out or not cur.out:
                continue
            them, me = (prev.message or "").strip(), (cur.message or "").strip()
            if usable(them) and usable(me):
                pairs.append({"them": them, "me": me, "ts": str(cur.date)})
    return pairs


def transcript(msgs: list, my_id: int, name: str, limit: int = 400) -> str:
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="append", default=[], metavar="ID|@USER|NAME")
    ap.add_argument("--dialogs", type=int, default=15)
    ap.add_argument("--scan", type=int, default=3000, help="messages per chat")
    ap.add_argument("--groups-only", action="store_true")
    ap.add_argument("--profile", action="store_true", help="also write facts.md")
    ap.add_argument("--reset", action="store_true", help="wipe memory before building")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    env = Env.load()
    session = resolve_session(env.session, ROOT)
    if not session.exists():
        sys.exit(f"No session at {session}\nRun `python -m scripts.login` first.")

    client = TelegramClient(str(session), env.api_id, env.api_hash)
    await client.start()
    me = await client.get_me()
    my_name = me.first_name or me.username or "the user"

    store = await Store(env.db_path).open()
    await store.db.executescript(SCHEMA)
    if args.reset:
        await store.db.execute("DELETE FROM memory")
    await store.db.commit()

    embedder = Embedder(env.ollama_host, os.getenv("EMBED_MODEL", "nomic-embed-text"))

    probe = await embedder.embed("test")
    if probe is None:
        await client.disconnect()
        await store.close()
        sys.exit(
            "embedding model unavailable. run:\n"
            f"  ollama pull {os.getenv('EMBED_MODEL', 'nomic-embed-text')}"
        )
    print(f"embedding model ok ({len(probe)} dims)\n")

    targets = [t.strip().lower().lstrip("@") for t in args.chat if t.strip()]
    scanned = 0
    total_pairs = 0
    profiles: list[str] = []

    async for dialog in client.iter_dialogs():
        if not targets and scanned >= args.dialogs:
            break
        is_group = bool(dialog.is_group and not dialog.is_channel)
        if args.groups_only and not is_group:
            continue
        if not is_group and not isinstance(dialog.entity, User):
            continue
        if not is_group and getattr(dialog.entity, "bot", False):
            continue
        if targets and not matches(dialog, targets):
            continue
        scanned += 1

        msgs = [m async for m in client.iter_messages(dialog, limit=args.scan)]
        msgs.reverse()
        pairs = extract_pairs(msgs, is_group)

        stored = 0
        for p in pairs:
            vec = await embedder.embed(p["them"])
            if vec is None:
                continue
            await store.db.execute(
                "INSERT INTO memory (chat_id, chat_name, them, me, ts, vec, dim)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    dialog.id,
                    dialog.name,
                    p["them"],
                    p["me"],
                    p["ts"],
                    pack(vec),
                    len(vec),
                ),
            )
            stored += 1
        await store.db.commit()
        total_pairs += stored
        print(f"{dialog.name}: {len(msgs)} messages, {stored} exchanges stored")

        if args.profile:
            text = transcript(msgs, me.id, my_name)
            if len(text) > 200:
                profiles.append((dialog.id, dialog.name, text))

    if args.profile and profiles:
        from bot.generate import make_backend

        backend = make_backend("ollama", env.ollama_host, env.ollama_model)
        FACTS_DIR.mkdir(exist_ok=True)
        written = []

        for chat_id, chat_name, text in profiles:
            print(f"\nprofiling {chat_name} — this takes a minute...")
            try:
                out = await backend.chat(
                    PROFILE_SYSTEM.format(name=my_name),
                    [{"role": "user", "content": text[:20000]}],
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {exc}")
                continue
            out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()
            if not out:
                print("  model returned nothing")
                continue

            # One file per chat, named by id. Facts are per-chat by nature —
            # who "Wei" is and what the running joke means do not transfer,
            # and a shared file would have the bot referencing things nobody
            # in the current conversation has ever said.
            path = FACTS_DIR / f"{chat_id}.md"
            path.write_text(
                f"<!-- {chat_name} (id {chat_id}) — generated "
                f"{datetime.now():%Y-%m-%d}. Edit freely; the bot re-reads on "
                f"change. -->\n\n{out}\n",
                encoding="utf-8",
            )
            written.append((chat_id, chat_name, path))
            print(f"  -> {path.relative_to(ROOT)}")

        if written:
            index = FACTS_DIR / "README.md"
            lines = [
                "# facts/",
                "",
                "One file per chat, named by chat id. `_global.md` (if you "
                "create it) is prepended to every prompt — put things about "
                "yourself there, not about any one group.",
                "",
                "| chat id | chat |",
                "|---|---|",
            ]
            lines += [f"| `{cid}` | {name} |" for cid, name, _ in written]
            index.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"\nwrote {len(written)} profiles + {index.relative_to(ROOT)}")
            print("READ THEM before running the bot — the model may have "
                  "invented things, and they go into every prompt for that chat")

    async with store.db.execute("SELECT COUNT(*) n FROM memory") as cur:
        row = await cur.fetchone()
    print(f"\nmemory now holds {row['n']} exchanges ({total_pairs} added this run)")

    await store.close()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
