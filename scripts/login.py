#!/usr/bin/env python3
"""One-time interactive login. Creates the .session file.

    python -m scripts.login

Telegram will send you a code. If you have 2FA on, it asks for that too.
The resulting .session file grants full access to your account: it is
chmod 600 here, and it must never be committed or synced to iCloud.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import SECRETS_DIR, find_session, session_path  # noqa: E402

TEMP_PREFIX = ".login-"


def _account_name(me) -> str:
    """File-safe name for the account that just logged in.

    Username where there is one, numeric id otherwise — a phone-only account
    has no username, and bots always do.
    """
    raw = (me.username or "").strip() or str(me.id)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    return safe or str(me.id)


def _rename_session(path: Path, name: str) -> Path:
    """Move the freshly written session onto its real name.

    Telethon leaves sidecar files (-journal, -wal) next to the database; they
    have to travel with it. The client must already be disconnected — Windows
    refuses to move a file SQLite still holds open.
    """
    target = path.with_name(f"{name}.session")
    for f in sorted(path.parent.glob(f"{path.name}*")):
        os.replace(f, target.with_name(target.name + f.name[len(path.name):]))
    return target


async def main() -> None:
    load_dotenv(ROOT / ".env")

    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit(
            "TG_API_ID / TG_API_HASH missing.\n"
            "Get them from https://my.telegram.org -> API development tools, "
            "then put them in .env"
        )

    # An explicit TG_SESSION wins, then any session already logged in (so
    # re-running this re-authenticates in place). Failing both, write to a
    # temporary name — the account is only known once Telegram answers.
    explicit = os.getenv("TG_SESSION")
    if explicit:
        path = session_path(explicit, ROOT)
    else:
        path = find_session(ROOT) or Path(f"./{SECRETS_DIR}/{TEMP_PREFIX}{os.getpid()}.session")
        path = session_path(str(path), ROOT)

    client = TelegramClient(str(path), int(api_id), api_hash)
    await client.start()

    me = await client.get_me()
    print(f"\nlogged in as {me.first_name} (@{me.username}) id={me.id}")

    await client.disconnect()

    if path.name.startswith(TEMP_PREFIX):
        path = _rename_session(path, _account_name(me))

    for f in path.parent.glob(f"{path.name}*"):
        os.chmod(f, 0o600)
    print(f"session written to {path} (chmod 600)")


if __name__ == "__main__":
    asyncio.run(main())
