"""Draft approval through Saved Messages.

Telegram only lets *bot* accounts attach inline keyboards, so a userbot cannot
give you Send/Ignore buttons. Instead a draft is posted to Saved Messages and
you reply to it:

    ok / y / send          -> send the draft as written
    no / n / skip / x      -> discard it
    anything else          -> send what you typed instead

You can also act on the most recent draft without replying, using
`/ok`, `/no`, or `/ok <token>` for a specific one.
"""

from __future__ import annotations

import logging
from typing import Optional

from .store import Pending, Store

log = logging.getLogger(__name__)

APPROVE_WORDS = {"ok", "okay", "k", "y", "yes", "send", "go", "yep", "sure"}
REJECT_WORDS = {"no", "n", "nope", "skip", "x", "ignore", "drop", "nah"}

DRAFT_TEMPLATE = """\
📝 draft · #{token} · {chat}

they said:
{incoming}

reply:
{reply}

↩️ reply `ok` to send · `no` to drop · or type your own version"""


def format_draft(token: str, chat: str, incoming: str, reply: str) -> str:
    incoming = (incoming or "(no text)").strip()
    if len(incoming) > 400:
        incoming = incoming[:399] + "…"
    return DRAFT_TEMPLATE.format(
        token=token,
        chat=chat or str("unknown"),
        incoming=incoming,
        reply=reply.strip(),
    )


async def post_draft(
    client,
    store: Store,
    *,
    chat_id: int,
    chat_name: str,
    reply_to_id: Optional[int],
    incoming_text: str,
    reply_text: str,
) -> str:
    """Store the draft and post it to Saved Messages. Returns the token."""
    token = await store.add_pending(
        chat_id=chat_id,
        chat_name=chat_name,
        reply_to_id=reply_to_id,
        incoming_text=incoming_text,
        reply_text=reply_text,
    )
    sent = await client.send_message(
        "me", format_draft(token, chat_name, incoming_text, reply_text)
    )
    await store.attach_draft_message(token, sent.id)
    return token


def classify(text: str) -> str:
    """-> 'approve' | 'reject' | 'replace'"""
    word = text.strip().lower().strip(".!")
    if word in APPROVE_WORDS:
        return "approve"
    if word in REJECT_WORDS:
        return "reject"
    return "replace"


async def resolve_draft(
    client,
    store: Store,
    pending: Pending,
    action: str,
    replacement: Optional[str] = None,
    *,
    shadow: bool = False,
) -> str:
    """Carry out an approval decision. Returns a short status line."""
    await store.resolve_pending(pending.token)

    if action == "reject":
        await store.log(
            chat_id=pending.chat_id,
            chat_name=pending.chat_name,
            message_id=pending.reply_to_id,
            mode="draft",
            decision="skipped",
            reason="rejected at approval",
            incoming_text=pending.incoming_text,
            reply_text=pending.reply_text,
        )
        return f"dropped #{pending.token}"

    text = replacement if action == "replace" and replacement else pending.reply_text

    if shadow:
        await store.log(
            chat_id=pending.chat_id,
            chat_name=pending.chat_name,
            message_id=pending.reply_to_id,
            mode="draft",
            decision="shadow",
            reason="approved but SHADOW=1",
            incoming_text=pending.incoming_text,
            reply_text=text,
        )
        return f"#{pending.token} approved but SHADOW=1, nothing sent"

    try:
        await client.send_message(
            pending.chat_id, text, reply_to=pending.reply_to_id
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to send approved draft")
        await store.log(
            chat_id=pending.chat_id,
            chat_name=pending.chat_name,
            message_id=pending.reply_to_id,
            mode="draft",
            decision="error",
            reason=str(exc),
            incoming_text=pending.incoming_text,
            reply_text=text,
        )
        return f"send failed: {exc}"

    await store.log(
        chat_id=pending.chat_id,
        chat_name=pending.chat_name,
        message_id=pending.reply_to_id,
        mode="draft",
        decision="sent",
        reason="approved" if action == "approve" else "edited then approved",
        incoming_text=pending.incoming_text,
        reply_text=text,
    )
    return f"sent to {pending.chat_name}"
