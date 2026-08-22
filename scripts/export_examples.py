#!/usr/bin/env python3
"""Harvest few-shot examples of how you actually write.

    python -m scripts.export_examples --dialogs 20 --per-dialog 8

Walks your chats, finds places where someone said something and you replied,
and writes {"them": ..., "me": ...} pairs to examples.jsonl.

`python -m bot.main` does this for you on a fresh install. This script is for
re-running it with different settings — a deeper scan, more chats, --loose —
without redoing the rest of setup. The extraction logic lives in bot/harvest.py.

This never leaves your machine. Read the output before using it and delete
anything you would not want a local model conditioning on.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import resolve_session  # noqa: E402
from bot.harvest import (  # noqa: E402
    extract_pairs,
    is_group_dialog,
    matches,
    normalise_targets,
    read_messages,
    wanted,
    write_examples,
)

OUT = ROOT / "examples.jsonl"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialogs", type=int, default=20, help="chats to scan")
    ap.add_argument("--per-dialog", type=int, default=8, help="max pairs per chat")
    ap.add_argument("--scan", type=int, default=400, help="messages to read per chat")
    ap.add_argument(
        "--groups",
        action="store_true",
        help="scan group chats instead of private ones. In groups only explicit "
        "reply-to pairs are used, since the previous message is rarely the one "
        "you were answering.",
    )
    ap.add_argument(
        "--chat",
        action="append",
        default=[],
        metavar="ID|@USER|NAME",
        help="only scan these chats. Repeatable. Matches a numeric id, a "
        "@username, or any part of the chat name (case-insensitive). "
        "Overrides --dialogs.",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="list matching chats with their ids and exit, without exporting",
    )
    ap.add_argument(
        "--append",
        action="store_true",
        help="add to examples.jsonl instead of replacing it",
    )
    ap.add_argument(
        "--loose",
        action="store_true",
        help="in groups, also pair a message of yours with the one directly "
        "before it when explicit reply-to pairs run short. Noisier — read the "
        "output before using it.",
    )
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    for key in ("TG_API_ID", "TG_API_HASH"):
        if not os.getenv(key):
            sys.exit(f"{key} not set. Copy .env.example to .env and fill it in.")

    session = resolve_session(os.getenv("TG_SESSION", ""), ROOT)
    if not session.exists():
        sys.exit(
            f"No session at {session}\n"
            "Run `python -m bot.main` first — this script reads your history "
            "and needs an authenticated session."
        )

    client = TelegramClient(
        str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    )
    await client.start()

    pairs: list[dict] = []
    scanned = 0
    targets = normalise_targets(args.chat)

    async for dialog in client.iter_dialogs():
        if not targets and scanned >= args.dialogs:
            break
        if not wanted(dialog, args.groups):
            continue
        if targets and not matches(dialog, targets):
            continue

        if args.list:
            uname = getattr(dialog.entity, "username", None)
            print(f"{dialog.id:>16}  {dialog.name}" + (f"  @{uname}" if uname else ""))
            continue

        scanned += 1
        msgs = await read_messages(client, dialog, args.scan)
        h = extract_pairs(
            msgs,
            is_group_dialog(dialog),
            loose=args.loose,
            limit=args.per_dialog,
        )
        pairs.extend(h.pairs)
        print(f"{dialog.name}: {len(h.pairs)}  ({h.explain()})")

    if args.list:
        await client.disconnect()
        return

    if targets and not scanned:
        print(f"\nno chats matched {args.chat}. try --list to see what's there")
        await client.disconnect()
        return

    appending = args.append and OUT.exists()
    write_examples(pairs, OUT, append=args.append)
    print(f"\n{'appended' if appending else 'wrote'} {len(pairs)} pairs to {OUT}")

    if not pairs:
        print(
            "\nnothing found. the counts above say why:\n"
            "  '0 from you'        -> you don't post in this group\n"
            "  '0 were replies'    -> you answer without hitting reply. use --loose\n"
            "  'pointed past'      -> raise --scan (try 2000)\n"
            "  'dropped by filter' -> messages were links or over 220 chars"
        )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
