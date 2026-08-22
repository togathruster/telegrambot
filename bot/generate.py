"""Reply generation. One interface, swappable backends.

`generate_reply` returns None whenever the model declines or the output looks
unusable. None always means "stay silent" — never a fallback message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger(__name__)

SKIP = "<skip>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MAX_REPLY_CHARS = 500

# opening quote -> its matching closing quote, for unwrapping model output
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",  # “ ”
    "‘": "’",  # ‘ ’
    "«": "»",  # « »
}


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


class Backend(Protocol):
    async def chat(self, system: str, messages: list[dict]) -> str: ...


def _norm(s: str) -> str:
    return " ".join(_PUNCT_RE.sub("", s.lower()).split())


def is_repetitive(reply: str, recent: list[str], window: int = 6) -> bool:
    """True if this reply repeats something we recently said in this chat.

    Short replies are the dangerous ones: the model sees its own "alr" in the
    history, decides "alr" is what this account says, and says it forever.
    Longer replies are allowed one recurrence before being blocked, since
    "let me check" legitimately comes up more than once.
    """
    n = _norm(reply)
    if not n:
        return True

    seen = [_norm(r) for r in recent[:window] if r]
    if not seen:
        return False

    # Never say the same thing twice in a row, whatever its length.
    if n == seen[0]:
        return True

    # One- and two-word fillers ("alr", "yea", "ok") loop hardest — block any
    # recurrence. Three words is already a phrase, not filler.
    if len(n.split()) <= 2:
        return n in seen

    # Longer phrases like "let me check" may recur, but not habitually.
    return seen.count(n) >= 2


@dataclass
class Outcome:
    """A reply, or nothing plus the reason why. The reason goes to the log.

    `raw` is the model's untouched output, kept so prompt dumps can show what
    was actually generated before cleaning threw it away.
    """

    text: Optional[str]
    reason: str
    raw: str = ""

    def __bool__(self) -> bool:
        return self.text is not None


def clean(raw: str) -> Outcome:
    """Strip reasoning traces and chatbot tells."""
    if not raw:
        return Outcome(None, "model returned nothing", raw)

    text = _THINK_RE.sub("", raw)
    if "<think>" in text.lower():
        return Outcome(
            None,
            "truncated inside a <think> block — thinking is on but num_predict "
            "is too low. Raise OLLAMA_NUM_PREDICT or set OLLAMA_THINK=0",
            raw,
        )
    text = text.strip()

    if not text:
        return Outcome(None, "only a reasoning block, no reply", raw)
    if SKIP in text.lower():
        return Outcome(None, "model chose to skip", raw)

    # models like to wrap replies in quotes; strip one matched layer
    if len(text) > 1 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
        if not text:
            return Outcome(None, "nothing left after unwrapping quotes", raw)

    # a lone code fence or a refusal preamble is not a reply
    if text.startswith("```"):
        return Outcome(None, "output was a code block", raw)
    lowered = text.lower()
    for tell in (
        "as an ai",
        "i'm an ai",
        "i cannot",
        "i can't help",
        "here's a reply",
        "here is a reply",
        "sure! here",
        "reply:",
    ):
        if lowered.startswith(tell):
            return Outcome(None, f"assistant tell: {tell!r}", raw)

    if len(text) > _MAX_REPLY_CHARS:
        return Outcome(None, f"too long ({len(text)} chars)", raw)

    return Outcome(text, "ok", raw)


class OllamaBackend:
    """Thinking is OFF by default and that is the right setting here.

    A three-word Telegram reply gains nothing from chain-of-thought, it
    triples latency, and a model that has just reasoned at length is far more
    inclined to produce *something* than to stay silent — which is the exact
    opposite of what this bot needs. OLLAMA_THINK=1 turns it on anyway, and
    raises the token budget so the reasoning has room to finish.
    """

    def __init__(self, host: str, model: str, temperature: float = 0.8,
                 num_predict: Optional[int] = None) -> None:
        from ollama import AsyncClient  # imported lazily so tests need no server

        self.client = AsyncClient(host=host)
        self.model = model
        self.temperature = temperature
        self.think = os.getenv("OLLAMA_THINK", "0") in ("1", "true", "True")
        self.keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

        # An explicit budget wins: the default below is sized for a Telegram
        # reply, and anything writing a document needs far more room. Without
        # this, persona.md and facts/ were silently cut off mid-sentence.
        if num_predict is None:
            default_predict = 1024 if self.think else 160
            num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", default_predict))
        self.num_predict = int(num_predict)
        if self.think and self.num_predict < 512:
            log.warning(
                "OLLAMA_THINK=1 with num_predict=%d — reasoning will likely be "
                "truncated and every reply dropped. Use 1024 or more.",
                self.num_predict,
            )
        self._supports_think = True

    async def chat(self, system: str, messages: list[dict]) -> str:
        payload = [{"role": "system", "content": system}, *messages]
        options = {
            "temperature": self.temperature,
            "top_p": 0.9,
            "num_predict": self.num_predict,
            "repeat_penalty": 1.05,
        }

        kwargs = dict(
            model=self.model,
            messages=payload,
            options=options,
            keep_alive=self.keep_alive,  # avoid a 9GB reload on every message
        )
        if self._supports_think:
            kwargs["think"] = self.think

        try:
            resp = await self.client.chat(**kwargs)
        except TypeError:
            # older ollama client without the `think` kwarg
            self._supports_think = False
            kwargs.pop("think", None)
            if self.think:
                log.warning(
                    "your ollama package is too old to accept think=; the model "
                    "will reason inline and may be truncated. pip install -U ollama"
                )
            resp = await self.client.chat(**kwargs)

        # "length" means the model was still writing when the budget ran out.
        # Silent truncation is how persona.md ended mid-bullet, so say so.
        if resp.get("done_reason") == "length":
            log.warning(
                "%s hit the %d-token limit and was cut off mid-output — "
                "raise num_predict for this call",
                self.model, self.num_predict,
            )

        message = resp.get("message") or {}
        # newer Ollama returns reasoning separately; content is already clean
        return message.get("content", "") or ""


class CannedBackend:
    """Deterministic backend for testing without a model running."""

    def __init__(self, reply: str = "sounds good") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, system: str, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.reply


async def check_models(host: str, required: list[str]) -> list[str]:
    """Which of these models Ollama doesn't have. Empty list means all present.

    Worth doing at startup: a missing model surfaces per-request as a 404 deep
    inside a handler, where it looks like a bad classification rather than a
    setup problem. One check up front turns an evening of confusing logs into
    a single actionable line.
    """
    from ollama import AsyncClient

    try:
        resp = await AsyncClient(host=host).list()
    except Exception:  # noqa: BLE001
        log.warning("could not reach Ollama at %s to verify models", host)
        return []  # let the real call produce the real error

    have: set[str] = set()
    for m in resp.get("models", []) or []:
        name = m.get("model") or m.get("name") or ""
        if name:
            have.add(name)
            have.add(name.split(":")[0])

    missing = []
    for want in required:
        if not want:
            continue
        if want in have or want.split(":")[0] in have:
            continue
        missing.append(want)
    return missing


# Writing persona.md or a chat profile is a different job from writing a
# three-word reply: it needs room to finish, and less randomness than the
# voice-matching the reply temperature is tuned for.
LONGFORM_PREDICT = 2048
LONGFORM_TEMPERATURE = 0.5


def make_backend(name: str, host: str, model: str,
                 num_predict: Optional[int] = None,
                 temperature: float = 0.8) -> Backend:
    if name == "canned":
        return CannedBackend()
    if name == "ollama":
        return OllamaBackend(host=host, model=model, temperature=temperature,
                             num_predict=num_predict)
    raise SystemExit(f"Unknown BACKEND={name!r}. Use 'ollama' or 'canned'.")


def make_longform_backend(name: str, host: str, model: str) -> Backend:
    """Backend for documents — persona.md, chat profiles — not chat replies."""
    return make_backend(name, host, model, num_predict=LONGFORM_PREDICT,
                        temperature=LONGFORM_TEMPERATURE)


async def generate_reply(
    backend: Backend,
    system: str,
    messages: list[dict],
    *,
    timeout: int = 90,
    feedback: Optional[str] = None,
) -> Outcome:
    """`feedback` is appended to the system prompt on a retry, carrying the
    critic's objection to the previous attempt."""
    if not messages:
        return Outcome(None, "no conversation to reply to")
    if feedback:
        system = system + feedback
    try:
        raw = await asyncio.wait_for(backend.chat(system, messages), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("generation timed out after %ss", timeout)
        return Outcome(None, f"timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        log.exception("generation failed")
        return Outcome(None, f"backend error: {type(exc).__name__}: {exc}")
    return clean(raw)
