#!/usr/bin/env python3
"""Scan your history, embed it, and build a profile the bot can use.

    # everything: 20 chats, 3000 messages each
    python -m scripts.build_context --dialogs 20 --scan 3000

    # one group, deeper
    python -m scripts.build_context --chat "uni bois" --scan 8000

    # also write facts/ using the local model
    python -m scripts.build_context --chat "uni bois" --scan 5000 --profile

`python -m bot.main` does both of these on a fresh install. This script is for
re-running them later — a deeper scan, a different chat, --reset — without
redoing the rest of setup. The logic lives in bot/harvest.py.

Two outputs:

  memory table   every exchange where you replied to someone, with an
                 embedding, so the bot can retrieve how you answered similar
                 messages before.

  facts/<id>.md  an optional prose profile per chat: who is in the group,
                 recurring topics, how you talk to them. Injected into every
                 prompt for that chat.

Nothing leaves your machine. Read facts/ before using it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import Env, resolve_session  # noqa: E402
from bot.harvest import (  # noqa: E402
    extract_pairs,
    is_group_dialog,
    matches,
    normalise_targets,
    read_messages,
    store_memory,
    transcript,
    wanted,
    write_facts_index,
    write_profile,
)
from bot.retrieve import SCHEMA, Embedder  # noqa: E402
from bot.store import Store  # noqa: E402

FACTS_DIR = ROOT / "facts"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="append", default=[], metavar="ID|@USER|NAME")
    ap.add_argument("--dialogs", type=int, default=15)
    ap.add_argument("--scan", type=int, default=3000, help="messages per chat")
    ap.add_argument("--groups-only", action="store_true")
    ap.add_argument("--profile", action="store_true", help="also write facts/")
    ap.add_argument("--reset", action="store_true", help="wipe memory before building")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    env = Env.load()
    session = resolve_session(env.session, ROOT)
    if not session.exists():
        sys.exit(f"No session at {session}\nRun `python -m bot.main` first.")

    client = TelegramClient(str(session), env.api_id, env.api_hash)
    await client.start()
    me = await client.get_me()
    my_name = me.first_name or me.username or "the user"

    store = await Store(env.db_path).open()
    await store.db.executescript(SCHEMA)
    if args.reset:
        await store.db.execute("DELETE FROM memory")
    await store.db.commit()

    embedder = Embedder(env.ollama_host, env.embed_model)
    probe = await embedder.embed("test")
    if probe is None:
        await client.disconnect()
        await store.close()
        sys.exit(f"embedding model unavailable. run:\n  ollama pull {env.embed_model}")
    print(f"embedding model ok ({len(probe)} dims)\n")

    targets = normalise_targets(args.chat)
    scanned = 0
    total_pairs = 0
    profiles: list[tuple[int, str, str]] = []

    async for dialog in client.iter_dialogs():
        if not targets and scanned >= args.dialogs:
            break
        is_group = is_group_dialog(dialog)
        if args.groups_only and not is_group:
            continue
        if not wanted(dialog, True if args.groups_only else None):
            continue
        if targets and not matches(dialog, targets):
            continue
        scanned += 1

        msgs = await read_messages(client, dialog, args.scan)
        h = extract_pairs(msgs, is_group)
        stored = await store_memory(store, embedder, dialog.id, dialog.name, h.pairs)
        total_pairs += stored
        print(f"{dialog.name}: {len(msgs)} messages, {stored} exchanges stored")

        if args.profile:
            text = transcript(msgs, my_name)
            if len(text) > 200:
                profiles.append((dialog.id, dialog.name, text))

    if args.profile and profiles:
        from bot.generate import make_longform_backend

        backend = make_longform_backend(
            env.backend, env.ollama_host, env.ollama_model
        )
        written: list[tuple[int, str]] = []

        for chat_id, chat_name, text in profiles:
            print(f"\nprofiling {chat_name} — this takes a minute...")
            try:
                path = await write_profile(
                    backend, FACTS_DIR, chat_id, chat_name, text, my_name
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {exc}")
                continue
            if path is None:
                print("  model returned nothing")
                continue
            written.append((chat_id, chat_name))
            print(f"  -> {path.relative_to(ROOT)}")

        if written:
            index = write_facts_index(FACTS_DIR, written)
            print(f"\nwrote {len(written)} profiles + {index.relative_to(ROOT)}")
            print("READ THEM before running the bot — the model may have "
                  "invented things, and they go into every prompt for that chat")

    print(f"\n{total_pairs} exchanges in memory across {scanned} chats")
    await client.disconnect()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
