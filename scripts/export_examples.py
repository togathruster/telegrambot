#!/usr/bin/env python3
"""Harvest few-shot examples of how you actually write.

    python -m scripts.export_examples --dialogs 20 --per-dialog 8

Walks your private chats, finds places where someone said something and you
replied, and writes {"them": ..., "me": ...} pairs to examples.jsonl.

This never leaves your machine. Read the output before using it and delete
anything you would not want a local model conditioning on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import resolve_session  # noqa: E402

OUT = ROOT / "examples.jsonl"

URL_RE = re.compile(r"https?://\S+")
# 1, not 2: in Chinese a single character ("好", "在", "要") is a whole reply.
# A 2-char floor silently discarded most CJK messages.
MIN_CHARS = 1
MAX_CHARS = 220


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


def usable(text: str) -> bool:
    text = text.strip()
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if text.startswith("/"):
        return False
    if URL_RE.search(text):
        return False
    return True


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
            "Run `python -m scripts.login` first — this script reads your history "
            "and needs an authenticated session."
        )

    client = TelegramClient(
        str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    )
    await client.start()

    pairs: list[dict] = []
    scanned = 0
    targets = [t.strip().lower().lstrip("@") for t in args.chat if t.strip()]

    async for dialog in client.iter_dialogs():
        if not targets and scanned >= args.dialogs:
            break

        if args.groups:
            if not (dialog.is_group and not dialog.is_channel):
                continue
        else:
            if not isinstance(dialog.entity, User) or dialog.entity.bot:
                continue

        if targets and not matches(dialog, targets):
            continue

        if args.list:
            uname = getattr(dialog.entity, "username", None)
            print(f"{dialog.id:>16}  {dialog.name}" + (f"  @{uname}" if uname else ""))
            continue

        scanned += 1

        msgs = [m async for m in client.iter_messages(dialog, limit=args.scan)]
        msgs.reverse()

        found = 0
        mine = sum(1 for m in msgs if m.out)

        if args.groups:
            # In a group the message before yours is usually unrelated. Only
            # trust explicit replies: you replied to them, so that IS the pair.
            # --loose falls back to adjacency when you rarely use reply.
            by_id = {m.id: m for m in msgs}
            replies = orphan = filtered = 0

            for cur in msgs:
                if found >= args.per_dialog:
                    break
                if not cur.out or not cur.reply_to_msg_id:
                    continue
                replies += 1
                prev = by_id.get(cur.reply_to_msg_id)
                if prev is None:
                    orphan += 1  # target older than --scan
                    continue
                if prev.out:
                    continue
                them, me = (prev.message or "").strip(), (cur.message or "").strip()
                if usable(them) and usable(me):
                    pairs.append({"them": them, "me": me})
                    found += 1
                else:
                    filtered += 1

            if args.loose and found < args.per_dialog:
                for prev, cur in zip(msgs, msgs[1:]):
                    if found >= args.per_dialog:
                        break
                    if prev.out or not cur.out or cur.reply_to_msg_id:
                        continue
                    them = (prev.message or "").strip()
                    me = (cur.message or "").strip()
                    if usable(them) and usable(me):
                        pairs.append({"them": them, "me": me})
                        found += 1

            detail = (
                f"scanned {len(msgs)}, {mine} from you, {replies} were replies"
            )
            if orphan:
                detail += f", {orphan} pointed past --scan"
            if filtered:
                detail += f", {filtered} dropped by the length/url filter"
            print(f"{dialog.name}: {found}  ({detail})")
            continue
        else:
            for prev, cur in zip(msgs, msgs[1:]):
                if found >= args.per_dialog:
                    break
                if prev.out or not cur.out:
                    continue
                them, me = (prev.message or "").strip(), (cur.message or "").strip()
                if usable(them) and usable(me):
                    pairs.append({"them": them, "me": me})
                    found += 1

        print(f"{dialog.name}: {found}  (scanned {len(msgs)}, {mine} from you)")

    if args.list:
        await client.disconnect()
        return

    if targets and not scanned:
        print(f"\nno chats matched {args.chat}. try --list to see what's there")
        await client.disconnect()
        return

    lines = "\n".join(json.dumps(p, ensure_ascii=False) for p in pairs)
    if args.append and OUT.exists():
        existing = OUT.read_text(encoding="utf-8").rstrip("\n")
        OUT.write_text(
            f"{existing}\n{lines}\n" if lines else f"{existing}\n",
            encoding="utf-8",
        )
        print(f"\nappended {len(pairs)} pairs to {OUT}")
    else:
        OUT.write_text(lines + "\n", encoding="utf-8")
        print(f"\nwrote {len(pairs)} pairs to {OUT}")
    if not pairs:
        print(
            "\nnothing found. the counts above say why:\n"
            "  '0 from you'        -> you don't post in this group\n"
            "  '0 were replies'    -> you answer without hitting reply. use --loose\n"
            "  'pointed past'      -> raise --scan (try 2000)\n"
            "  'dropped by filter' -> messages were links or over 220 chars"
        )
    print("read it over and delete anything private before running the bot")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
