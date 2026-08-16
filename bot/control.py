"""Commands you type into your own Saved Messages to steer the bot."""

from __future__ import annotations

import logging
from typing import Optional

from datetime import datetime

from .config import ConfigStore
from .store import Store

log = logging.getLogger(__name__)

HELP = """\
commands (type these in Saved Messages)

/on · /off          resume or halt everything
/status             current modes, today's counts, shadow flag
/mode <chat> <m>    set off | draft | auto for a chat
/mode <chat> clear  drop the override, fall back to config.yaml
/chats              recently seen chats with their ids
/queue              what is waiting to be answered
/last [n]           last n log entries (default 10)
/ok [token]         approve the newest draft, or a specific one
/no [token]         drop the newest draft, or a specific one
/reload             re-read config.yaml now
/help               this

chat can be a numeric id or @username
reply directly to a draft with ok / no / your own text"""

VALID_MODES = {"off", "draft", "auto"}

COMMANDS = {
    "help", "start", "on", "off", "reload", "status",
    "mode", "chats", "last", "ok", "no", "queue",
}


def looks_like_command(text: str) -> bool:
    """Accept a bare `status` as well as `/status`.

    Only for short messages: a real saved note like "status update for the
    Monday call" should stay a note, not become a command.
    """
    parts = text.strip().split()
    if not parts or len(parts) > 4:
        return False
    return parts[0].lower() in COMMANDS


async def _resolve_chat_id(client, token: str) -> Optional[int]:
    token = token.strip()
    if not token:
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        entity = await client.get_entity(token)
        return entity.id
    except Exception:  # noqa: BLE001
        return None


async def handle_command(
    client,
    store: Store,
    cfg: ConfigStore,
    text: str,
    *,
    shadow: bool,
    queue=None,
) -> Optional[str]:
    """Returns a reply string, or None if this wasn't a command."""
    text = text.strip()
    if not text.startswith("/") and not looks_like_command(text):
        return None

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    args = parts[1:]

    if cmd in ("help", "start"):
        return HELP

    if cmd == "on":
        await store.set_enabled(True)
        return "enabled" + (" (SHADOW=1, still nothing will send)" if shadow else "")

    if cmd == "off":
        await store.set_enabled(False)
        return "disabled. nothing will reply until /on"

    if cmd == "reload":
        cfg.reload(force=True)
        if cfg.error:
            return (f"config.yaml is INVALID — still running the last good "
                    f"version:\n\n{cfg.error}")
        return "config.yaml reloaded"

    if cmd == "status":
        return await _status(store, cfg, shadow, queue)

    if cmd == "queue":
        if queue is None:
            return "queue not running"
        batches = await queue.pending()
        if not batches:
            return "queue empty"
        now = datetime.now()
        lines = [f"{await queue.depth()} message(s) waiting", ""]
        for b in sorted(batches, key=lambda x: -x.priority):
            waited = int((now - b.oldest.enqueued_at).total_seconds())
            lines.append(f"{b.chat_name} — {len(b.items)} msg(s), "
                         f"oldest {waited}s ago, prio {b.priority}")
            for i in b.items[-3:]:
                lines.append(f"    {i.sender}: {i.text[:60]}")
        return "\n".join(lines)

    if cmd == "mode":
        if len(args) < 2:
            return "usage: /mode <chat id or @username> <off|draft|auto|clear>"
        chat_id = await _resolve_chat_id(client, args[0])
        if chat_id is None:
            return f"couldn't resolve chat {args[0]!r}"
        target = args[1].lower()
        if target == "clear":
            await store.clear_mode_override(chat_id)
            resolved = cfg.resolve(str(chat_id))
            return f"{chat_id} override cleared, now {resolved.mode} from config.yaml"
        if target not in VALID_MODES:
            return f"mode must be one of: {', '.join(sorted(VALID_MODES))}, or clear"
        await store.set_mode_override(chat_id, target, args[0])
        return f"{chat_id} -> {target}"

    if cmd == "chats":
        return await _chats(store)

    if cmd == "last":
        n = 10
        if args:
            try:
                n = max(1, min(50, int(args[0])))
            except ValueError:
                pass
        return await _last(store, n)

    if cmd in ("ok", "no"):
        from .approval import resolve_draft

        pending = (
            await store.get_pending(args[0])
            if args
            else await store.latest_pending()
        )
        if pending is None:
            return "no pending draft"
        action = "approve" if cmd == "ok" else "reject"
        return await resolve_draft(
            client, store, pending, action, shadow=shadow
        )

    return f"unknown command {cmd!r}. /help for the list"


async def _status(store: Store, cfg: ConfigStore, shadow: bool, queue=None) -> str:
    enabled = await store.is_enabled()
    total = await store.count_today()
    conf = cfg.config
    overrides = await store.all_mode_overrides()

    lines = []
    if cfg.error:
        lines += [
            "⚠️ config.yaml is INVALID — running the last good version",
            f"   {cfg.error}",
            "",
        ]
    lines += [
        f"kill switch: {'on' if enabled else 'OFF'}",
        f"shadow mode: {'ON — nothing sends' if shadow else 'off'}",
        f"sent today: {total} / {conf.global_max_replies_per_day}",
        f"default mode: {conf.defaults.mode}",
    ]
    if queue is not None:
        lines.append(f"queued now: {await queue.depth()}")
    lines.append("")

    if overrides:
        lines.append("runtime overrides:")
        for row in overrides:
            label = row["label"] or ""
            lines.append(f"  {row['chat_id']} {label} -> {row['mode']}")
    else:
        lines.append("runtime overrides: none")

    yaml_chats = [
        f"  {k} -> {v.mode or conf.defaults.mode}" for k, v in conf.chats.items()
    ]
    if yaml_chats:
        lines.append("")
        lines.append("from config.yaml:")
        lines.extend(yaml_chats)

    pending = await store.latest_pending()
    if pending:
        lines.append("")
        lines.append(f"pending draft: #{pending.token} for {pending.chat_name}")

    return "\n".join(lines)


async def _chats(store: Store) -> str:
    rows = await store.recent(limit=200)
    seen: dict[int, str] = {}
    for row in rows:
        if row["chat_id"] not in seen:
            seen[row["chat_id"]] = row["chat_name"] or "?"
        if len(seen) >= 25:
            break
    if not seen:
        return "no chats seen yet"
    return "\n".join(f"{cid}  {name}" for cid, name in seen.items())


async def _last(store: Store, n: int) -> str:
    rows = await store.recent(limit=n)
    if not rows:
        return "log is empty"
    out = []
    for row in rows:
        ts = row["ts"][5:16].replace("T", " ")
        line = f"{ts} [{row['decision']}] {row['chat_name'] or row['chat_id']}"
        if row["reason"] and row["decision"] != "sent":
            line += f" — {row['reason']}"
        if row["reply_text"]:
            snippet = row["reply_text"].replace("\n", " ")[:80]
            line += f"\n    → {snippet}"
        out.append(line)
    return "\n".join(out)
