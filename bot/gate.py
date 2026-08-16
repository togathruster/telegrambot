"""The gate, in two halves.

`should_enqueue` asks "is this the kind of message we would ever answer?" —
identity, mode, chat type, content. These facts never change, so a rejection
here is permanent and the message is discarded.

`may_dispatch` asks "may we speak in this chat right now?" — quiet hours,
cooldown, daily caps. These are temporary, so a rejection here means wait,
not discard. That distinction is the whole reason for the queue: the old
single-gate design threw away every message that arrived during a cooldown.

Deliberately free of Telethon types so it can be unit-tested with plain
dataclasses. main.py adapts real events into `Incoming`.

Every rejection carries a reason string, which is written to the audit log.
Reading those reasons is how you tune the config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import ConfigStore, Mode, Resolved
from .store import Store


@dataclass
class Incoming:
    chat_id: int
    chat_name: str
    message_id: int
    text: str
    is_private: bool
    is_group: bool
    sender_is_bot: bool
    sender_is_self: bool
    mentions_me: bool
    replies_to_me: bool
    has_media: bool
    username: Optional[str] = None


@dataclass
class Decision:
    allow: bool
    reason: str
    mode: Mode
    settings: Optional[Resolved] = None
    retry: bool = False  # True when the block is temporary — wait, don't drop

    @property
    def blocked(self) -> bool:
        return not self.allow


def _is_command(text: str) -> bool:
    return text.strip().startswith("/")


async def resolve_settings(msg: Incoming, cfg: ConfigStore, store: Store) -> Resolved:
    override = await store.get_mode_override(msg.chat_id)
    return cfg.resolve(
        str(msg.chat_id),
        f"@{msg.username}" if msg.username else None,
        msg.username,
        mode_override=override,  # type: ignore[arg-type]
    )


async def should_enqueue(msg: Incoming, cfg: ConfigStore, store: Store) -> Decision:
    """Permanent checks. A rejection here means the message is discarded.

    No network calls — this runs on every incoming message in every chat.
    """
    settings = await resolve_settings(msg, cfg, store)
    mode: Mode = settings.mode

    def no(reason: str) -> Decision:
        return Decision(False, reason, mode, settings)

    # 1. Never react to ourselves or to other bots.
    if msg.sender_is_self:
        return no("own message")
    if msg.sender_is_bot:
        return no("sender is a bot")

    # 2. Chat must be switched on.
    if mode == "off":
        return no("chat mode is off")

    # 3. Global kill switch. Temporary, but a queue that fills up while you
    #    are deliberately switched off is not a feature.
    if not await store.is_enabled():
        return no("globally disabled (/on to resume)")

    # 4. Channels and broadcasts are never in scope.
    if not (msg.is_private or msg.is_group):
        return no("not a private chat or group")

    # 5. In groups, require an explicit hook unless configured otherwise.
    if msg.is_group and settings.require_mention_in_groups:
        if not (msg.mentions_me or msg.replies_to_me):
            return no("group message without mention or reply")

    # 6. Content filters.
    if _is_command(msg.text):
        return no("message is a command")
    if not msg.text.strip():
        if not (msg.has_media and cfg.config.reply_to_media_only):
            return no("empty message with no usable text")

    return Decision(True, "queued", mode, settings)


async def may_dispatch(
    chat_id: int,
    settings: Resolved,
    cfg: ConfigStore,
    store: Store,
    *,
    now: Optional[datetime] = None,
    has_prior_outgoing: Optional[bool] = None,
) -> Decision:
    """Temporary checks, evaluated when a queued batch is about to be answered.

    `retry=True` means try again shortly. `retry=False` means this will not
    become true today, so stop holding the message.
    """
    now = now or datetime.now()
    conf = cfg.config
    mode = settings.mode

    def no(reason: str, retry: bool = True) -> Decision:
        return Decision(False, reason, mode, settings, retry=retry)

    if not await store.is_enabled():
        return no("globally disabled (/on to resume)")

    # Stranger filter: never open a conversation we've never taken part in.
    # Permanent in practice, so don't hold the message waiting for it.
    if conf.require_prior_outgoing and has_prior_outgoing is False:
        return no("no prior outgoing message in this chat", retry=False)

    if settings.in_quiet_hours(now):
        return no(f"quiet hours ({settings.quiet_hours})")

    last = await store.last_sent_at(chat_id)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < settings.cooldown_seconds:
            remaining = int(settings.cooldown_seconds - elapsed)
            return no(f"cooldown, {remaining}s remaining")

    # Caps reset at midnight, so waiting is pointless — drop.
    per_chat = await store.count_today(chat_id)
    if per_chat >= settings.max_replies_per_day:
        return no(f"chat daily cap reached ({settings.max_replies_per_day})", False)

    total = await store.count_today()
    if total >= conf.global_max_replies_per_day:
        return no(
            f"global daily cap reached ({conf.global_max_replies_per_day})", False
        )

    return Decision(True, "passed", mode, settings)


async def should_reply(
    msg: Incoming,
    cfg: ConfigStore,
    store: Store,
    *,
    now: Optional[datetime] = None,
    has_prior_outgoing: Optional[bool] = None,
) -> Decision:
    """Both halves at once. Used by tests and by any non-queued path."""
    first = await should_enqueue(msg, cfg, store)
    if first.blocked:
        return first
    assert first.settings is not None
    return await may_dispatch(
        msg.chat_id,
        first.settings,
        cfg,
        store,
        now=now,
        has_prior_outgoing=has_prior_outgoing,
    )
