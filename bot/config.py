"""Configuration loading, validation, and per-chat resolution.

Resolution order for any setting, highest priority first:
    1. mode overrides stored in the database (set at runtime via /mode)
    2. the chat's entry in config.yaml
    3. config.yaml `defaults`
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

Mode = Literal["off", "draft", "auto"]

_BOOLISH = {"true", "false", "yes", "no", "on", "off"}


def _describe(exc: Exception) -> str:
    """Turn a pydantic/yaml explosion into one line you can act on."""
    try:
        from pydantic import ValidationError
    except ImportError:  # pragma: no cover
        return str(exc)

    if not isinstance(exc, ValidationError):
        return " ".join(str(exc).split())[:300]

    parts = []
    for err in exc.errors():
        where = ".".join(str(x) for x in err["loc"])
        got = err.get("input")
        line = f"{where}: {err['msg'].lower()} (got {got!r})"
        # the overwhelmingly common case is a typo'd boolean
        if isinstance(got, str) and err["type"] == "bool_parsing":
            near = [b for b in _BOOLISH if b.startswith(got.lower()[:3])]
            if near:
                line += f" — did you mean {near[0]}?"
        parts.append(line)
    return "; ".join(parts)[:400]


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


class ChatConfig(BaseModel):
    """Per-chat overrides. Every field is optional; None means 'inherit'."""

    mode: Optional[Mode] = None
    cooldown_seconds: Optional[int] = None
    context_messages: Optional[int] = None
    quiet_hours: Optional[str] = None
    require_mention_in_groups: Optional[bool] = None
    max_replies_per_day: Optional[int] = None
    persona_notes: Optional[str] = None


class Defaults(BaseModel):
    mode: Mode = "off"
    cooldown_seconds: int = 300
    context_messages: int = 15
    quiet_hours: Optional[str] = "23:00-08:00"
    require_mention_in_groups: bool = True
    max_replies_per_day: int = 20
    persona_notes: str = ""

    @field_validator("quiet_hours")
    @classmethod
    def _check_quiet_hours(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, "", "null"):
            return None
        start, _, end = v.partition("-")
        _parse_hhmm(start)
        _parse_hhmm(end)
        return v


class Config(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    chats: dict[str, ChatConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data):
        """A YAML key with only comments under it parses as None, not {}."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    global_max_replies_per_day: int = 60
    require_prior_outgoing: bool = True
    reply_to_media_only: bool = False
    min_typing_seconds: float = 1.5
    max_typing_seconds: float = 7.0
    generation_timeout_seconds: int = 90

    # Stage 1. Only these labels reach the writer; everything else is silence.
    classifier_enabled: bool = True
    classifier_allow: list[str] = Field(default_factory=lambda: ["SAFE"])
    classifier_timeout_seconds: int = 30

    # Stage 3. Reads the draft back before sending; rejections get one retry.
    verify_enabled: bool = True
    verify_max_retries: int = 1
    verify_timeout_seconds: int = 45

    # Queue and pacing
    queue_tick_seconds: int = 2
    queue_debounce_seconds: int = 10
    queue_max_wait_seconds: int = 90
    queue_max_age_minutes: int = 45
    reply_to_threshold: int = 5

    # Retrieval over your own history
    retrieval_enabled: bool = True
    retrieval_k: int = 4
    retrieval_min_score: float = 0.45
    retrieval_same_chat_only: bool = False

    @field_validator("classifier_allow")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v]


@dataclass(frozen=True)
class Resolved:
    """Fully resolved settings for one chat."""

    mode: Mode
    cooldown_seconds: int
    context_messages: int
    quiet_hours: Optional[str]
    require_mention_in_groups: bool
    max_replies_per_day: int
    persona_notes: str

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        if not self.quiet_hours:
            return False
        now = now or datetime.now()
        start_s, _, end_s = self.quiet_hours.partition("-")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        current = now.time()
        if start <= end:
            return start <= current < end
        # window spans midnight, e.g. 23:00-08:00
        return current >= start or current < end


class ConfigStore:
    """Loads config.yaml and reloads it when the file changes on disk.

    A live-reloading config must never take the process down. `config` is read
    on every incoming message and every worker tick, so a typo in the file
    would otherwise raise from deep inside unrelated code, forever, while the
    process stays alive looking healthy.

    So: a bad edit is logged loudly and the last known-good config stays in
    force. The only fatal case is the very first load, where there is nothing
    good to fall back to.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mtime: float = -1.0
        self._config = Config()
        self._loaded = False
        self.error: Optional[str] = None
        self.reload(force=True)

    @property
    def config(self) -> Config:
        self.reload()
        return self._config

    @property
    def healthy(self) -> bool:
        return self.error is None

    def reload(self, force: bool = False) -> bool:
        """Re-read the file if its mtime changed. Returns True if reloaded."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if not self._loaded:
                raise
            if self.error is None:
                self.error = f"{self.path} has disappeared"
                log.error("%s — keeping the config already in memory", self.error)
            return False

        if not force and mtime == self._mtime:
            return False

        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            parsed = Config.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            # Record the mtime anyway: without this, every single access
            # re-parses the broken file and re-logs the same error.
            self._mtime = mtime
            self.error = _describe(exc)
            if not self._loaded:
                raise SystemExit(
                    f"\n{self.path} is invalid:\n\n  {self.error}\n\n"
                    "Fix it and start again.\n"
                )
            log.error(
                "config.yaml is invalid, still using the last good version: %s",
                self.error,
            )
            return False

        self._config = parsed
        self._mtime = mtime
        self._loaded = True
        if self.error:
            log.info("config.yaml is valid again")
        self.error = None
        return True

    def chat_entry(self, *keys: Optional[str]) -> ChatConfig:
        """First matching entry for any of the given keys (id, @username)."""
        chats = self.config.chats
        for key in keys:
            if key and key in chats:
                return chats[key]
        return ChatConfig()

    def resolve(
        self,
        *keys: Optional[str],
        mode_override: Optional[Mode] = None,
    ) -> Resolved:
        cfg = self.config
        d = cfg.defaults
        c = self.chat_entry(*keys)

        def pick(name: str):
            v = getattr(c, name)
            return getattr(d, name) if v is None else v

        return Resolved(
            mode=mode_override or pick("mode"),
            cooldown_seconds=pick("cooldown_seconds"),
            context_messages=pick("context_messages"),
            quiet_hours=pick("quiet_hours"),
            require_mention_in_groups=pick("require_mention_in_groups"),
            max_replies_per_day=pick("max_replies_per_day"),
            persona_notes=pick("persona_notes") or "",
        )


SECRETS_DIR = "secrets"


def session_path(session: str, root: Path) -> Path:
    """Absolute session path, with its parent directory guaranteed to exist.

    SQLite raises a bare 'unable to open database file' when the parent
    directory is missing, which points nowhere useful. Create it up front.
    """
    p = Path(session).expanduser()
    if not p.is_absolute():
        p = Path(root) / p
    p = p.resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except OSError:
        pass
    return p


def find_session(root: Path) -> Path | None:
    """The one session in secrets/, or None if login hasn't run yet.

    The file is named after whoever logged in, so its name is not knowable up
    front — look for it rather than assuming a name.
    """
    found = sorted((Path(root) / SECRETS_DIR).glob("*.session"))
    if len(found) > 1:
        raise SystemExit(
            f"Several sessions in {SECRETS_DIR}/: "
            f"{', '.join(p.name for p in found)}\n"
            "Set TG_SESSION in .env to the one you want to run as."
        )
    return found[0] if found else None


def resolve_session(session: str, root: Path) -> Path:
    """Session to use: TG_SESSION if set, otherwise whatever login created."""
    if session:
        return session_path(session, root)
    found = find_session(root)
    # Nothing yet: name a path anyway so the caller can report a missing
    # session against something concrete instead of an empty string.
    return session_path(str(found) if found else f"./{SECRETS_DIR}/telegram.session", root)


@dataclass(frozen=True)
class Env:
    api_id: int
    api_hash: str
    session: str
    ollama_host: str
    ollama_model: str
    backend: str
    shadow: bool
    db_path: str
    classifier_model: str
    embed_model: str
    verify_model: str

    @staticmethod
    def load() -> "Env":
        missing = [k for k in ("TG_API_ID", "TG_API_HASH") if not os.getenv(k)]
        if missing:
            raise SystemExit(
                f"Missing required env vars: {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )
        return Env(
            api_id=int(os.environ["TG_API_ID"]),
            api_hash=os.environ["TG_API_HASH"],
            # Blank means "use whatever session login created" — see
            # resolve_session(). The name depends on who logged in.
            session=os.getenv("TG_SESSION", ""),
            ollama_host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:14b"),
            backend=os.getenv("BACKEND", "ollama").lower(),
            shadow=os.getenv("SHADOW", "1") not in ("0", "false", "False", ""),
            db_path=os.getenv("DB_PATH", "./secrets/state.db"),
            classifier_model=os.getenv("CLASSIFIER_MODEL", "qwen3:4b"),
            embed_model=os.getenv("EMBED_MODEL", "nomic-embed-text"),
            # blank means "use the writer model"
            verify_model=os.getenv("VERIFY_MODEL", ""),
        )
