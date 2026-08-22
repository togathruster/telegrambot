"""First-run setup: everything the bot needs, without a list of commands.

`python -m bot.main` calls into here when required files are missing, so a new
joiner runs one command and answers questions instead of memorising a six-step
pipeline.

The design constraint is that none of this is tied to a terminal. All logic
lives in run_setup(), which asks its questions through a Prompter. The console
implementation below is one such Prompter; a web frontend can supply another
without any of the steps changing. Nothing in the core prints or calls input().
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence

import yaml
from telethon import TelegramClient

from .config import (
    Config,
    Env,
    account_session_name,
    find_session,
    rename_session,
    resolve_session,
    temp_session,
)
from .harvest import (
    ChatInfo,
    extract_pairs,
    list_chats,
    read_messages,
    store_memory,
    transcript,
    write_examples,
    write_facts_index,
    write_profile,
)
from .persona import draft_persona, save_persona, suspect_lines

# How much history each step reads. Examples only need enough to show voice;
# memory and profiles want everything they can get.
EXAMPLE_SCAN = 2000
EXAMPLE_PER_CHAT = 25
CONTEXT_SCAN = 3000

# Below this, a group has not given the persona model enough to work from and
# it starts inventing habits. Strict reply-pairs only count the messages you
# answered with Telegram's reply marker, which in a busy group is a small
# fraction of what you actually wrote.
MIN_PAIRS_PER_CHAT = 12


# --------------------------------------------------------------------- state


@dataclass
class SetupState:
    """What already exists. Every field is a step that can be skipped."""

    session: Optional[Path]
    examples: bool
    persona: bool
    memory: bool
    facts: bool
    active_chat: bool

    @property
    def has_session(self) -> bool:
        return self.session is not None and self.session.exists()

    @property
    def complete(self) -> bool:
        return all(
            (self.has_session, self.examples, self.persona,
             self.memory, self.facts, self.active_chat)
        )

    def missing(self) -> list[str]:
        out = []
        if not self.has_session:
            out.append("a logged-in Telegram session")
        if not self.examples:
            out.append("examples.jsonl (how you write)")
        if not self.persona:
            out.append("persona.md (your voice)")
        if not self.memory:
            out.append("embedded history (for retrieval)")
        if not self.facts:
            out.append("facts/ (chat profiles)")
        if not self.active_chat:
            out.append("a chat set to draft or auto in config.yaml")
        return out


def _has_rows(db_path: Path, table: str) -> bool:
    """Cheap synchronous peek. The bot's async Store isn't open yet."""
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        cur = con.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False  # table not created yet
    finally:
        con.close()


def _any_chat_enabled(config_path: Path) -> bool:
    """True if anything is set to reply. Otherwise the bot runs and does nothing."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        cfg = Config.model_validate(raw)
    except Exception:  # noqa: BLE001
        return False
    if cfg.defaults.mode != "off":
        return True
    return any(c.mode and c.mode != "off" for c in cfg.chats.values())


def detect(env: Env, root: Path) -> SetupState:
    # Deliberately not session_path(): detection must not create directories.
    db = Path(env.db_path).expanduser()
    if not db.is_absolute():
        db = root / db
    facts_dir = root / "facts"
    examples = root / "examples.jsonl"
    return SetupState(
        session=find_session(root) if not env.session else resolve_session(env.session, root),
        examples=examples.exists() and examples.stat().st_size > 0,
        persona=(root / "persona.md").exists(),
        memory=_has_rows(db, "memory"),
        facts=facts_dir.exists() and any(facts_dir.glob("*.md")),
        active_chat=_any_chat_enabled(root / "config.yaml")
        or _has_rows(db, "chat_mode"),
    )


# ------------------------------------------------------------------ prompter


class Prompter(Protocol):
    """How setup talks to a human. Implement this to put setup behind any UI."""

    def notice(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def step(self, title: str) -> None: ...

    # Telethon calls these directly during login; they may be awaited.
    async def ask_phone(self) -> str: ...
    async def ask_code(self) -> str: ...
    async def ask_password(self) -> str: ...

    async def choose_chats(self, chats: Sequence[ChatInfo]) -> list[ChatInfo]: ...
    async def review_persona(self, text: str) -> str: ...
    async def edit_text(self, text: str) -> str: ...
    async def confirm(self, question: str, default: bool = True) -> bool: ...


async def _ainput(prompt: str) -> str:
    """input() without stalling the event loop Telethon is running on."""
    return (await asyncio.to_thread(input, prompt)).strip()


class ConsolePrompter:
    """Terminal implementation. The only place in setup that does I/O."""

    def notice(self, text: str) -> None:
        print(text)

    def warn(self, text: str) -> None:
        print(f"  ! {text}", file=sys.stderr)

    def step(self, title: str) -> None:
        print(f"\n\033[1m{title}\033[0m")

    async def ask_phone(self) -> str:
        return await _ainput("  phone number (with country code, e.g. +65...): ")

    async def ask_code(self) -> str:
        return await _ainput("  the code Telegram just sent you: ")

    async def ask_password(self) -> str:
        import getpass

        return (await asyncio.to_thread(getpass.getpass, "  2FA password: ")).strip()

    async def choose_chats(self, chats: Sequence[ChatInfo]) -> list[ChatInfo]:
        for i, c in enumerate(chats, 1):
            kind = "group" if c.is_group else "dm"
            print(f"  {i:>3}. [{kind:>5}] {c.label}")
        print(
            "\n  Pick the chats to learn from — numbers separated by commas "
            "(e.g. 1,3,7),\n  a range (1-5), or 'all'. Groups you talk in a "
            "lot give the best result."
        )
        while True:
            raw = await _ainput("  > ")
            picked = _parse_selection(raw, len(chats))
            if picked:
                return [chats[i] for i in picked]
            print("  nothing selected — type at least one number, or 'all'")

    async def review_persona(self, text: str) -> str:
        print("\n" + "─" * 68)
        print(text)
        print("─" * 68)
        print(
            "\n  This goes into every prompt. Read it: the model may have "
            "invented\n  habits you do not have."
        )
        while True:
            raw = (await _ainput("  [a]ccept  [r]egenerate  [e]dit: ")).lower()
            if raw.startswith("a"):
                return "accept"
            if raw.startswith("r"):
                return "regenerate"
            if raw.startswith("e"):
                return "edit"

    async def edit_text(self, text: str) -> str:
        editor = os.getenv("EDITOR") or ("notepad" if os.name == "nt" else "nano")
        fd, tmp = tempfile.mkstemp(suffix=".md", text=True)
        os.close(fd)
        path = Path(tmp)
        path.write_text(text, encoding="utf-8")
        try:
            await asyncio.to_thread(subprocess.call, [editor, str(path)])
            return path.read_text(encoding="utf-8")
        except OSError:
            self.warn(f"could not launch {editor} — keeping the draft as it was")
            return text
        finally:
            path.unlink(missing_ok=True)

    async def confirm(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        raw = (await _ainput(f"  {question} {suffix} ")).lower()
        if not raw:
            return default
        return raw.startswith("y")


def _parse_selection(raw: str, count: int) -> list[int]:
    """'1,3,5-7' or 'all' -> zero-based indices, deduped and in order."""
    raw = raw.strip().lower()
    if raw in ("all", "*"):
        return list(range(count))
    picked: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            lo, _, hi = part.partition("-")
            try:
                for n in range(int(lo), int(hi) + 1):
                    if 1 <= n <= count and n - 1 not in picked:
                        picked.append(n - 1)
            except ValueError:
                continue
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= count and n - 1 not in picked:
                picked.append(n - 1)
    return picked


# -------------------------------------------------------------- config.yaml

CHATS_KEY = "\nchats:\n"


def enable_chats(config_path: Path, chats: Sequence[ChatInfo],
                 mode: str = "auto") -> list[ChatInfo]:
    """Add picked chats to config.yaml under `chats:`, set to `mode`.

    Edited as text rather than re-serialised: config.yaml is mostly comments
    explaining every knob, and PyYAML would throw all of them away on a
    round-trip. Returns the chats actually added.
    """
    original = config_path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(original) or {}
    except Exception:  # noqa: BLE001
        return []
    already = set((raw.get("chats") or {}).keys())

    new = [c for c in chats if str(c.id) not in already]
    if not new:
        return []

    lines = []
    for c in new:
        label = " ".join(str(c.name).split())[:60]
        lines.append(f'  "{c.id}":   # {label}')
        lines.append(f'    mode: "{mode}"')
    block = "\n".join(lines) + "\n"

    marker = "\nchats:\n"
    if marker in original:
        head, _, tail = original.partition(marker)
        updated = head + marker + block + tail
    else:
        updated = original.rstrip("\n") + "\n" + marker + block

    # Never leave the user with a config the bot refuses to start on.
    try:
        Config.model_validate(yaml.safe_load(updated) or {})
    except Exception:  # noqa: BLE001
        return []
    config_path.write_text(updated, encoding="utf-8")
    return new


# ----------------------------------------------------------------- the steps


@dataclass
class SetupResult:
    session: Optional[Path] = None
    examples: int = 0
    persona: bool = False
    memory: int = 0
    facts: list[Path] = field(default_factory=list)
    enabled: list[ChatInfo] = field(default_factory=list)
    ran: bool = False


async def _login(env: Env, root: Path, p: Prompter) -> tuple[TelegramClient, object, Path]:
    """Log in, then name the session after whoever authenticated."""
    existing = find_session(root)
    path = existing or temp_session(root)
    client = TelegramClient(str(path), env.api_id, env.api_hash)

    if existing:
        p.notice(f"  already logged in ({existing.name})")
    else:
        p.notice(
            "  Telegram will text you a code. Nothing is sent anywhere else —\n"
            "  the session file stays on this machine."
        )

    # An authorised session signs straight back in without asking anything.
    await client.start(phone=p.ask_phone, code_callback=p.ask_code,
                       password=p.ask_password)
    me = await client.get_me()
    if existing:
        return client, me, path

    p.notice(f"  logged in as {me.first_name} (id {me.id})")
    # The real name is only knowable now, and the file must be closed to move.
    await client.disconnect()
    path = rename_session(path, account_session_name(me))
    for f in path.parent.glob(f"{path.name}*"):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    client = TelegramClient(str(path), env.api_id, env.api_hash)
    await client.connect()
    return client, me, path


async def run_setup(env: Env, root: Path, p: Prompter,
                    force: bool = False) -> SetupResult:
    """Fill in whatever is missing. Safe to re-run: finished steps are skipped."""
    from .generate import check_models, make_longform_backend
    from .retrieve import SCHEMA, Embedder
    from .store import Store

    state = detect(env, root)
    result = SetupResult(session=state.session)
    if state.complete and not force:
        return result
    result.ran = True

    p.notice("\n\033[1mSetup\033[0m — this runs once. Everything stays on this machine.")
    for item in state.missing():
        p.notice(f"  missing: {item}")

    # Fail before the interview rather than after it, so nobody answers ten
    # questions and then discovers the model was never pulled.
    needed = [env.ollama_model, env.embed_model]
    missing_models = await check_models(env.ollama_host, needed)
    if missing_models:
        pulls = "\n".join(f"  ollama pull {m}" for m in missing_models)
        raise SystemExit(
            f"\nSetup needs {len(missing_models)} Ollama model(s) first:\n\n"
            f"{pulls}\n\nIs Ollama running at {env.ollama_host}?\n"
        )

    p.step("1/5  Telegram login")
    client, me, result.session = await _login(env, root, p)

    my_name = me.first_name or me.username or "the user"

    try:
        p.step("2/5  Which chats should the bot learn from?")
        chats = await list_chats(client, limit=40, groups=None)
        if not chats:
            raise SystemExit("No usable chats found on this account.")
        picked = await p.choose_chats(chats)
        p.notice(f"  picked {len(picked)}: " + ", ".join(c.name for c in picked))

        # -------------------------------------------------- examples.jsonl
        p.step("3/5  Learning how you write")
        examples: list[dict] = []
        if state.examples and not force:
            from .context import load_examples

            examples = load_examples(root / "examples.jsonl", limit=500)
            p.notice(f"  examples.jsonl already has {len(examples)} pairs")
        else:
            for c in picked:
                msgs = await read_messages(client, c.id, EXAMPLE_SCAN)
                h = extract_pairs(msgs, c.is_group, loose=not c.is_group,
                                  limit=EXAMPLE_PER_CHAT)
                # Strict pairs came up short: fall back to adjacency so the
                # persona has something to read. Noisier, but a persona built
                # on a handful of lines is guesswork.
                if c.is_group and len(h.pairs) < MIN_PAIRS_PER_CHAT:
                    loose = extract_pairs(msgs, True, loose=True,
                                          limit=EXAMPLE_PER_CHAT)
                    if len(loose.pairs) > len(h.pairs):
                        p.notice(
                            f"  {c.name}: only {len(h.pairs)} reply-marked "
                            f"pairs — also using adjacent messages"
                        )
                        h = loose
                examples.extend(h.pairs)
                p.notice(f"  {c.name}: {len(h.pairs)} pairs  ({h.explain()})")
            if not examples:
                raise SystemExit(
                    "\nFound no usable examples in those chats. Pick chats you "
                    "actually reply in, or run:\n"
                    "  python -m scripts.export_examples --groups --loose\n"
                )
            result.examples = write_examples(examples, root / "examples.jsonl")
            p.notice(f"  wrote {result.examples} pairs to examples.jsonl")

        # ------------------------------------------------------- persona.md
        p.step("4/5  Writing persona.md")
        persona_file = root / "persona.md"
        if state.persona and not force:
            p.notice("  persona.md already exists — keeping it")
            result.persona = True
        else:
            backend = make_longform_backend(
                env.backend, env.ollama_host, env.ollama_model
            )
            text = ""
            while True:
                if not text:
                    p.notice(f"  asking {env.ollama_model} to read your messages...")
                    text = await draft_persona(backend, examples, my_name)
                    if not text:
                        p.warn("the model returned nothing")
                        if not await p.confirm("try again?", True):
                            break
                        continue
                for line, why in suspect_lines(text, examples):
                    p.warn(f"{why}\n      {line}")
                action = await p.review_persona(text)
                if action == "accept":
                    save_persona(text, persona_file)
                    result.persona = True
                    p.notice("  wrote persona.md")
                    break
                if action == "edit":
                    text = await p.edit_text(text)
                    continue
                text = ""  # regenerate

        # ------------------------------------------- memory + chat profiles
        p.step("5/5  Reading history for context")
        store = await Store(env.db_path).open()
        try:
            await store.db.executescript(SCHEMA)
            await store.db.commit()
            embedder = Embedder(env.ollama_host, env.embed_model)
            backend = make_longform_backend(
                env.backend, env.ollama_host, env.ollama_model
            )
            facts_dir = root / "facts"
            written: list[tuple[int, str]] = []

            for c in picked:
                msgs = await read_messages(client, c.id, CONTEXT_SCAN)
                if not (state.memory and not force):
                    h = extract_pairs(msgs, c.is_group)
                    n = await store_memory(store, embedder, c.id, c.name, h.pairs)
                    result.memory += n
                    p.notice(f"  {c.name}: {len(msgs)} messages, {n} exchanges stored")

                if state.facts and not force:
                    continue
                text = transcript(msgs, my_name)
                if len(text) < 200:
                    continue
                p.notice(f"  profiling {c.name} — this takes a minute...")
                try:
                    path = await write_profile(backend, facts_dir, c.id, c.name,
                                               text, my_name)
                except Exception as exc:  # noqa: BLE001
                    p.warn(f"profiling {c.name} failed: {exc}")
                    continue
                if path:
                    result.facts.append(path)
                    written.append((c.id, c.name))
            if written:
                write_facts_index(facts_dir, written)
        finally:
            await store.close()

        # ------------------------------------------------------ config.yaml
        if not state.active_chat or force:
            result.enabled = enable_chats(root / "config.yaml", picked, "auto")
    finally:
        await client.disconnect()

    return result


def summarise(result: SetupResult, p: Prompter) -> None:
    """Say what was built and what to check before it starts replying as you."""
    if not result.ran:
        return
    p.notice("\n\033[1mSetup complete.\033[0m")
    if result.examples:
        p.notice(f"  examples.jsonl   {result.examples} pairs")
    if result.persona:
        p.notice("  persona.md       written")
    if result.memory:
        p.notice(f"  memory           {result.memory} exchanges embedded")
    if result.facts:
        p.notice(f"  facts/           {len(result.facts)} chat profile(s)")
    if result.enabled:
        names = ", ".join(c.name for c in result.enabled)
        p.notice(f"  config.yaml      auto mode for {names}")
        p.warn(
            "auto mode sends replies as you, without asking. Change any chat "
            "to \"draft\" in config.yaml to approve them first."
        )
    if result.facts:
        p.notice(
            "\n  Read facts/ before trusting it — the model may have invented "
            "things,\n  and they go into every prompt for that chat."
        )
