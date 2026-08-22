"""Entry point.

Incoming messages are not answered on arrival. They are queued, and a single
worker drains the queue: waiting for bursts to settle, coalescing everything
pending for a chat into one reply, and holding rather than discarding when a
cooldown is in force. See bot/queue.py for why.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import User

from .approval import classify as classify_approval
from .approval import post_draft, resolve_draft
from .classify import classify
from .config import ConfigStore, Env, resolve_session
from .context import (
    FactsStore,
    Msg,
    build_messages,
    build_system_prompt,
    load_examples,
    load_persona,
)
from .control import handle_command
from .gate import Incoming, may_dispatch, should_enqueue
from .generate import check_models, generate_reply, is_repetitive, make_backend
from .queue import (
    PRIORITY_DM,
    PRIORITY_MENTION,
    PRIORITY_REPLY,
    Batch,
    Queue,
    choose,
    is_ready,
    should_use_reply_to,
)
from .retrieve import SCHEMA as MEMORY_SCHEMA
from .retrieve import Embedder, format_retrieved, search
from .setup import ConsolePrompter, detect, run_setup, summarise
from .store import Store
from .verify import feedback_for, verify

ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("bot")


class Bot:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.cfg = ConfigStore(ROOT / "config.yaml")
        self.store = Store(env.db_path)
        self.queue: Queue | None = None

        self.session = resolve_session(env.session, ROOT)
        if not self.session.exists():
            raise SystemExit(
                f"No session at {self.session}\n"
                "Run `python -m bot.main` — it will log you in."
            )
        self.client = TelegramClient(str(self.session), env.api_id, env.api_hash)

        conf = self.cfg.config
        self.backend = make_backend(env.backend, env.ollama_host, env.ollama_model)
        self.classifier = (
            make_backend(env.backend, env.ollama_host, env.classifier_model)
            if conf.classifier_enabled
            else None
        )
        self.embedder = (
            Embedder(env.ollama_host, env.embed_model)
            if conf.retrieval_enabled
            else None
        )
        # The critic defaults to the writer model — judging a draft is closer
        # to writing than to classifying, and a 4B is a weak reviewer.
        self.critic = (
            make_backend(
                env.backend, env.ollama_host, env.verify_model or env.ollama_model
            )
            if conf.verify_enabled
            else None
        )

        self.persona = load_persona(ROOT / "persona.md")
        self.examples = load_examples(ROOT / "examples.jsonl")
        self.facts = FactsStore(ROOT)
        self.me: User | None = None
        self.my_name = "the user"

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def chat_label(chat) -> str:
        for attr in ("title", "first_name", "username"):
            value = getattr(chat, attr, None)
            if value:
                return str(value)
        return str(getattr(chat, "id", "unknown"))

    async def has_prior_outgoing(self, chat) -> bool:
        async for _ in self.client.iter_messages(chat, limit=1, from_user="me"):
            return True
        return False

    async def messages_since(self, chat, message_id: int, cap: int = 30) -> int:
        """How many messages have landed since `message_id`.

        Decides whether a reply needs Telegram's reply-to marker. Capped
        because past the threshold the exact number stops mattering.
        """
        count = 0
        async for _ in self.client.iter_messages(chat, min_id=message_id, limit=cap):
            count += 1
        return count

    async def history(self, chat, limit: int, with_names: bool = False) -> list[Msg]:
        rows: list[Msg] = []
        async for m in self.client.iter_messages(chat, limit=limit):
            text = (m.message or "").strip()
            if not text:
                continue
            sender = ""
            if with_names and not m.out:
                # Telethon usually has the entity cached from iter_messages;
                # fall back to the raw id rather than paying for a fetch.
                s = m.sender
                sender = (
                    getattr(s, "first_name", None)
                    or getattr(s, "title", None)
                    or getattr(s, "username", None)
                    or str(m.sender_id)
                )
            rows.append(Msg(mine=bool(m.out), text=text, sender=sender))
        rows.reverse()
        return rows

    def dump_prompt(self, label: str, system: str, messages: list, outcome) -> None:
        """Write the exact prompt and raw output to logs/prompts/ when
        LOG_PROMPTS=1. This is the only way to see what the model actually
        received — everything else shows you what you think it received."""
        if os.getenv("LOG_PROMPTS", "0") not in ("1", "true", "True"):
            return
        try:
            d = ROOT / "logs" / "prompts"
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in label)[:40]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            body = [
                "=" * 70, "SYSTEM PROMPT", "=" * 70, system, "",
                "=" * 70, f"MESSAGES ({len(messages)} turns)", "=" * 70,
            ]
            for m in messages:
                body.append(f"--- {m['role']} ---")
                body.append(m["content"])
            body += [
                "", "=" * 70, "RAW MODEL OUTPUT", "=" * 70,
                outcome.raw or "(empty)", "",
                f"cleaned : {outcome.text!r}",
                f"reason  : {outcome.reason}",
            ]
            (d / f"{stamp}-{safe}.txt").write_text("\n".join(body), encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("failed to dump prompt")

    async def type_and_send(self, chat_id: int, text: str, reply_to: int | None):
        conf = self.cfg.config
        delay = min(
            conf.max_typing_seconds,
            max(conf.min_typing_seconds, len(text) / 18.0),
        )
        delay *= random.uniform(0.85, 1.25)
        async with self.client.action(chat_id, "typing"):
            await asyncio.sleep(delay)
        await self.client.send_message(chat_id, text, reply_to=reply_to)

    # --------------------------------------------------------------- intake

    async def on_incoming(self, event) -> None:
        """Decide whether this could ever be answered, then queue it.

        Deliberately cheap: no model calls, no extra API round-trips. All the
        expensive work happens in the worker, once per batch instead of once
        per message.
        """
        chat = await event.get_chat()
        chat_id = event.chat_id
        label = self.chat_label(chat)
        text = (event.message.message or "").strip()

        if not await self.store.mark_seen(chat_id, event.message.id):
            return

        sender = await event.get_sender()
        username = getattr(sender, "username", None) or getattr(chat, "username", None)

        replies_to_me = False
        if event.message.reply_to_msg_id:
            replied = await event.message.get_reply_message()
            replies_to_me = bool(replied and replied.out)

        msg = Incoming(
            chat_id=chat_id,
            chat_name=label,
            message_id=event.message.id,
            text=text,
            is_private=bool(event.is_private),
            is_group=bool(event.is_group),
            sender_is_bot=bool(getattr(sender, "bot", False)),
            sender_is_self=bool(event.message.out),
            mentions_me=bool(event.message.mentioned),
            replies_to_me=replies_to_me,
            has_media=bool(event.message.media),
            username=username,
        )

        decision = await should_enqueue(msg, self.cfg, self.store)
        if decision.blocked:
            log.debug("drop %s: %s", label, decision.reason)
            if decision.reason not in (
                "chat mode is off", "own message", "sender is a bot",
            ):
                await self.store.log(
                    chat_id=chat_id, chat_name=label, message_id=msg.message_id,
                    mode=decision.mode, decision="skipped",
                    reason=decision.reason, incoming_text=text,
                )
            return

        if msg.is_private:
            priority = PRIORITY_DM
        elif msg.replies_to_me:
            priority = PRIORITY_REPLY
        else:
            priority = PRIORITY_MENTION

        assert self.queue is not None
        await self.queue.push(
            chat_id=chat_id,
            chat_name=label,
            message_id=msg.message_id,
            sender=getattr(sender, "first_name", "") or "them",
            text=text,
            is_group=msg.is_group,
            priority=priority,
        )
        log.info("[queued] %s: %s", label, text[:60])

    # --------------------------------------------------------------- worker

    async def worker(self) -> None:
        assert self.queue is not None
        while True:
            await asyncio.sleep(self.cfg.config.queue_tick_seconds)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")

    async def tick(self) -> None:
        assert self.queue is not None
        conf = self.cfg.config

        expired = await self.queue.expire(conf.queue_max_age_minutes)
        if expired:
            log.info("%d queued message(s) expired unanswered", expired)

        batches = await self.queue.pending()
        ready = [
            b for b in batches
            if is_ready(
                b,
                debounce_seconds=conf.queue_debounce_seconds,
                max_wait_seconds=conf.queue_max_wait_seconds,
            )
        ]
        batch = choose(ready)
        if batch is not None:
            await self.process(batch)

    async def process(self, batch: Batch) -> None:
        assert self.queue is not None
        conf = self.cfg.config
        label = batch.chat_name
        target = batch.newest

        try:
            chat = await self.client.get_entity(batch.chat_id)
        except Exception:  # noqa: BLE001
            log.exception("cannot resolve chat %s, dropping batch", batch.chat_id)
            await self.queue.resolve(batch.ids, "dropped")
            return

        override = await self.store.get_mode_override(batch.chat_id)
        settings = self.cfg.resolve(str(batch.chat_id), mode_override=override)

        prior = None
        if conf.require_prior_outgoing:
            prior = await self.has_prior_outgoing(chat)

        gate = await may_dispatch(
            batch.chat_id, settings, self.cfg, self.store, has_prior_outgoing=prior
        )
        if gate.blocked:
            if gate.retry:
                log.debug("[%s] holding: %s", label, gate.reason)
                return  # stays queued, tried again next tick
            await self.queue.resolve(batch.ids, "dropped")
            await self.store.log(
                chat_id=batch.chat_id, chat_name=label,
                message_id=target.message_id, mode=settings.mode,
                decision="skipped", reason=gate.reason,
                incoming_text=batch.combined_text(),
            )
            log.info("[%s] dropped: %s", label, gate.reason)
            return

        # from here the batch is consumed whatever happens
        await self.queue.resolve(batch.ids)
        in_group = target.is_group
        text = batch.combined_text()

        # ---- stage 1: is this safe to answer at all? ----
        if self.classifier is not None:
            pre = build_messages(
                await self.history(chat, 6, with_names=in_group),
                include_names=in_group,
            )
            verdict = await classify(
                self.classifier, self.my_name, text, target.sender,
                pre, set(conf.classifier_allow),
                timeout=conf.classifier_timeout_seconds,
            )
            if not verdict.allowed:
                await self.store.log(
                    chat_id=batch.chat_id, chat_name=label,
                    message_id=target.message_id, mode=settings.mode,
                    decision="skipped", reason=verdict.reason, incoming_text=text,
                )
                log.info("[%s] %s", label, verdict.reason)
                return

        history = await self.history(chat, settings.context_messages, with_names=in_group)

        # ---- retrieval: how has he answered things like this before? ----
        retrieved = ""
        if self.embedder is not None:
            try:
                hits = await search(
                    self.store.db, self.embedder, text,
                    k=conf.retrieval_k,
                    chat_id=batch.chat_id if conf.retrieval_same_chat_only else None,
                    min_score=conf.retrieval_min_score,
                )
                retrieved = format_retrieved(hits, self.my_name)
            except Exception:  # noqa: BLE001
                log.exception("retrieval failed, continuing without it")

        system = build_system_prompt(
            self.persona, self.my_name, settings.persona_notes,
            is_group=in_group, examples=self.examples,
            facts=self.facts.get(batch.chat_id), retrieved=retrieved,
        )
        messages = build_messages(history, include_names=in_group)

        recent = await self.store.recent_replies(batch.chat_id)
        reply: str | None = None
        feedback: str | None = None
        fail_reason = ""

        # write -> criticise -> rewrite. Bounded; silence is the fallback.
        for attempt in range(conf.verify_max_retries + 1):
            outcome = await generate_reply(
                self.backend, system, messages,
                timeout=conf.generation_timeout_seconds,
                feedback=feedback,
            )
            self.dump_prompt(label, system, messages, outcome)

            if outcome.text is None:
                fail_reason = outcome.reason
                break

            if is_repetitive(outcome.text, recent):
                fail_reason = "repeat of a recent reply"
                log.info("[%s] suppressed repeat: %s", label, outcome.text)
                break

            if self.critic is None:
                reply = outcome.text
                break

            critique = await verify(
                self.critic, self.my_name, messages, outcome.text,
                timeout=conf.verify_timeout_seconds,
            )
            if critique.passed:
                reply = outcome.text
                if attempt:
                    log.info("[%s] passed on retry %d", label, attempt)
                break

            fail_reason = critique.log_reason
            log.info("[%s] %s — draft was: %s",
                     label, critique.log_reason, outcome.text)
            if critique.error:
                break  # a broken critic won't get better on retry
            feedback = feedback_for(critique.reason)

        if reply is None:
            await self.store.log(
                chat_id=batch.chat_id, chat_name=label,
                message_id=target.message_id, mode=settings.mode,
                decision="skipped", reason=fail_reason or "no usable reply",
                incoming_text=text,
            )
            log.info("[%s] no reply: %s", label, fail_reason)
            return

        # ---- reply-to only once the thread has moved on ----
        since = await self.messages_since(chat, target.message_id)
        use_reply = should_use_reply_to(since, conf.reply_to_threshold)
        reply_to = target.message_id if use_reply else None

        if self.env.shadow:
            await self.store.log(
                chat_id=batch.chat_id, chat_name=label,
                message_id=target.message_id, mode=settings.mode,
                decision="shadow", reason=f"SHADOW=1 (+{since} since)",
                incoming_text=text, reply_text=reply,
            )
            log.info("[shadow][%s] would reply%s: %s",
                     label, " (as reply-to)" if use_reply else "", reply)
            return

        if settings.mode == "draft":
            token = await post_draft(
                self.client, self.store,
                chat_id=batch.chat_id, chat_name=label,
                reply_to_id=target.message_id,  # by approval time it has moved on
                incoming_text=text, reply_text=reply,
            )
            await self.store.log(
                chat_id=batch.chat_id, chat_name=label,
                message_id=target.message_id, mode="draft",
                decision="drafted", reason=f"awaiting approval #{token}",
                incoming_text=text, reply_text=reply,
            )
            log.info("[draft][%s] #%s %s", label, token, reply)
            return

        try:
            await self.type_and_send(batch.chat_id, reply, reply_to)
        except Exception as exc:  # noqa: BLE001
            log.exception("send failed")
            await self.store.log(
                chat_id=batch.chat_id, chat_name=label,
                message_id=target.message_id, mode=settings.mode,
                decision="error", reason=str(exc),
                incoming_text=text, reply_text=reply,
            )
            return

        await self.store.log(
            chat_id=batch.chat_id, chat_name=label,
            message_id=target.message_id, mode="auto",
            decision="sent",
            reason=f"auto, {len(batch.items)} msg(s)"
                   + (", reply-to" if use_reply else ""),
            incoming_text=text, reply_text=reply,
        )
        log.info("[sent][%s] %s", label, reply)

    # ---------------------------------------------------------- saved messages

    async def on_saved_message(self, event) -> None:
        """Outgoing messages in Saved Messages: commands and draft approvals."""
        text = (event.message.message or "").strip()
        if not text:
            return

        reply = await handle_command(
            self.client, self.store, self.cfg, text,
            shadow=self.env.shadow, queue=self.queue,
        )
        if reply is not None:
            await self.client.send_message("me", reply)
            return

        if not event.message.reply_to_msg_id:
            return

        pending = await self.store.get_pending_by_message(event.message.reply_to_msg_id)
        if pending is None:
            return

        action = classify_approval(text)
        status = await resolve_draft(
            self.client, self.store, pending, action,
            replacement=text, shadow=self.env.shadow,
        )
        await self.client.send_message("me", status)

    # ------------------------------------------------------------------ run

    async def housekeeping(self) -> None:
        assert self.queue is not None
        while True:
            await asyncio.sleep(3600)
            try:
                await self.store.prune_seen()
                await self.queue.prune()
                expired = await self.store.expire_pending()
                if expired:
                    log.info("expired %d stale drafts", expired)
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")

    async def preflight(self) -> None:
        """Fail at startup on a missing model, not silently on every message."""
        if self.env.backend != "ollama":
            return
        conf = self.cfg.config
        required = [self.env.ollama_model]
        if conf.classifier_enabled:
            required.append(self.env.classifier_model)
        if conf.retrieval_enabled:
            required.append(self.env.embed_model)
        if conf.verify_enabled and self.env.verify_model:
            required.append(self.env.verify_model)

        missing = await check_models(self.env.ollama_host, required)
        if missing:
            pulls = "\n".join(f"  ollama pull {m}" for m in missing)
            raise SystemExit(
                f"\nOllama is missing {len(missing)} model(s):\n\n{pulls}\n\n"
                "Pull them, or disable the stage that needs them in "
                "config.yaml (classifier_enabled / retrieval_enabled).\n"
            )

    async def run(self) -> None:
        await self.preflight()
        await self.store.open()
        await self.store.db.executescript(MEMORY_SCHEMA)
        await self.store.db.commit()
        self.queue = Queue(self.store.db)
        await self.queue.setup()

        await self.client.start()
        self.me = await self.client.get_me()
        self.my_name = self.me.first_name or self.me.username or "the user"

        self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(
            self.on_saved_message, events.NewMessage(chats="me", outgoing=True)
        )

        asyncio.create_task(self.worker())
        asyncio.create_task(self.housekeeping())

        conf = self.cfg.config
        log.info("logged in as %s (id %s)", self.my_name, self.me.id)
        log.info("writer=%s", self.env.ollama_model)
        if self.classifier is not None:
            log.info("classifier=%s allow=%s",
                     self.env.classifier_model, ",".join(conf.classifier_allow))
        else:
            log.warning("classifier disabled — the writer polices itself")
        if self.critic is not None:
            log.info("critic=%s retries=%d",
                     self.env.verify_model or self.env.ollama_model,
                     conf.verify_max_retries)
        else:
            log.warning("critic disabled — drafts go out unreviewed")
        if self.embedder is not None:
            async with self.store.db.execute("SELECT COUNT(*) n FROM memory") as cur:
                row = await cur.fetchone()
            if row["n"]:
                log.info("retrieval=%s over %d exchanges",
                         self.env.embed_model, row["n"])
            else:
                log.warning("retrieval on but memory is empty — "
                            "run `python -m bot.main --setup`")
        available = self.facts.available()
        if available:
            log.info("facts: %s", ", ".join(available))
        elif self.facts.global_facts():
            log.info("facts: legacy facts.md only")
        else:
            log.warning("no facts files — run `python -m bot.main --setup`")
        log.info(
            "queue: debounce %ss, max wait %ss, expire %smin, reply-to past %d msgs",
            conf.queue_debounce_seconds, conf.queue_max_wait_seconds,
            conf.queue_max_age_minutes, conf.reply_to_threshold,
        )
        depth = await self.queue.depth()
        if depth:
            log.info("%d message(s) still queued from last run", depth)
        if self.env.shadow:
            log.warning("SHADOW MODE — replies are logged, never sent")
        if not self.examples:
            log.warning("no examples.jsonl — run `python -m bot.main --setup`")

        await self.client.run_until_disconnected()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m bot.main",
        description="Run the bot. On a fresh install it sets itself up first.",
    )
    ap.add_argument(
        "--setup",
        action="store_true",
        help="re-run setup even if everything is already in place",
    )
    ap.add_argument(
        "--no-setup",
        action="store_true",
        help="never run setup; fail instead if something is missing",
    )
    ap.add_argument(
        "--setup-only",
        action="store_true",
        help="run setup and exit without starting the bot",
    )
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    env = Env.load()

    try:
        asyncio.run(_start(env, args))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


async def _start(env: Env, args) -> None:
    """Set up if needed, then run. Setup owns the session until it hands over."""
    state = detect(env, ROOT)

    if args.no_setup:
        if not state.complete:
            raise SystemExit(
                "\nNot ready to run, and --no-setup was given. Missing:\n"
                + "\n".join(f"  - {m}" for m in state.missing())
                + "\n\nDrop --no-setup to fix this interactively.\n"
            )
    elif args.setup or not state.complete:
        prompter = ConsolePrompter()
        result = await run_setup(env, ROOT, prompter, force=args.setup)
        summarise(result, prompter)
        if args.setup_only:
            return
        print()

    if args.setup_only:
        print("nothing to set up — everything is already in place")
        return

    await Bot(env).run()


if __name__ == "__main__":
    main()
