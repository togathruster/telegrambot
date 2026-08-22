# Architecture

The diagrams in the README answer "what does this do". These answer "where did
my message go", which is the question you actually have once it is running.

Almost every message ends somewhere other than "sent". That is the design, not
a failure: a missed reply costs nothing, and a wrong one goes out under your
name and cannot be recalled. Every path below that stops early is a place the
bot decided silence was the better answer, and every one of them is written to
SQLite with its reason. `/last` shows them.

## The reply pipeline

```mermaid
flowchart TD
    IN["message arrives"] --> G1{"permanent checks<br>no model calls"}
    G1 -->|"own or bot sender, mode off,<br>no mention in group, command, empty"| D1["discard, log the reason"]
    G1 -->|"passes"| Q[("queued in SQLite")]

    Q --> TICK["worker tick"]
    TICK --> READY{"burst settled?"}
    READY -->|"still arriving<br>(10s debounce, 90s cap)"| Q
    READY -->|"queued over 45 min"| D2["expire unanswered"]
    READY -->|"ready"| G2{"temporary checks"}

    G2 -->|"quiet hours, cooldown,<br>globally off"| Q
    G2 -->|"never spoken here,<br>daily cap reached"| D3["drop, log the reason"]
    G2 -->|"passes"| CLS["CLASSIFY qwen3:4b"]

    CLS -->|"FACT, MONEY, PLAN,<br>SENSITIVE, OTHER"| D4["stay silent, log the label"]
    CLS -->|"SAFE"| RET["RETRIEVE nomic-embed-text<br>how you answered before"]
    RET --> WR["WRITE qwen3:14b<br>persona, facts, examples,<br>retrieved, live history"]

    WR --> CLEAN{"usable draft?"}
    CLEAN -->|"skip sentinel, empty,<br>repeats a recent reply"| D5["drop, log the reason"]
    CLEAN -->|"yes"| CRIT{"CHECK critic qwen3:14b"}
    CRIT -->|"FAIL, retries left"| WR
    CRIT -->|"FAIL, out of retries"| D6["drop, log the reason"]
    CRIT -->|"PASS"| SH{"SHADOW=1?"}

    SH -->|"yes"| D7["log what it would<br>have sent, send nothing"]
    SH -->|"no"| MODE{"chat mode"}
    MODE -->|"draft"| SAV["post to Saved Messages,<br>you reply ok or no"]
    MODE -->|"auto"| SEND["typing delay, then send"]
    SAV --> LOG[("every decision logged")]
    SEND --> LOG

    classDef stop fill:#7f1d1d,stroke:#ef4444,color:#fff
    classDef store fill:#1e3a5f,stroke:#60a5fa,color:#fff
    class D1,D2,D3,D4,D5,D6,D7 stop
    class Q,LOG store
```

### The two gates

They look similar and do opposite things.

`should_enqueue` in `bot/gate.py` runs on every incoming message, makes no
network calls, and its rejections are **permanent** — the message is dropped
and never reconsidered. It covers things that will not change by waiting: the
sender is you or a bot, the chat is off, nobody mentioned you in a group.

`may_dispatch` runs when a batch is about to be answered, and most of its
rejections are **temporary**. Quiet hours and cooldowns put the batch back in
the queue to be retried on a later tick. Three do not: a chat you have never
spoken in, and the daily caps, per chat and global, are dropped rather than
held, because holding a message for something that will not become true again
today just fills the queue.

### Why messages wait

The bot never answers on arrival. A batch is only ready once the sender has
been quiet for `queue_debounce_seconds`, so a burst still being typed is not
answered halfway through, or once the oldest message has waited
`queue_max_wait_seconds`, so a fast typist is not ignored forever. Everything
pending for one chat is then coalesced into a single reply.

When several chats are ready at once, `choose` ranks them: DMs first, then
replies to you, then plain mentions, then whoever waited longest.

### The retry loop

The critic in `bot/verify.py` reads the draft back against the conversation
and answers PASS or FAIL with a reason. On FAIL the reason is handed back to
the writer as feedback and it tries again, up to `verify_max_retries`. If it
still fails, nothing is sent. This costs an extra model call per reply, which
is the trade `verify_enabled` exists to let you refuse.

## First-run setup

```mermaid
flowchart TD
    A["python -m bot.main"] --> B{"what is missing?"}
    B -->|"nothing"| RUN(["start the bot"])
    B -->|"something"| MOD{"Ollama models pulled?"}
    MOD -->|"no"| MX["stop and print the<br>ollama pull commands"]
    MOD -->|"yes"| L{"session already exists?"}

    L -->|"yes"| PICK
    L -->|"no"| LOGIN["phone number, code, 2FA"]
    LOGIN --> NAME["save as<br>secrets/username.session"]
    NAME --> PICK["pick chats from a<br>numbered list: 1,4,7 or all"]

    PICK --> HARV["read your history,<br>keep pairs where you replied"]
    HARV --> THIN{"fewer than 12<br>pairs in a group?"}
    THIN -->|"yes"| LOOSE["also pair adjacent<br>messages, and say so"]
    THIN -->|"no"| EXJ
    LOOSE --> EXJ[("examples.jsonl")]

    EXJ --> DRAFT["qwen3:14b reads them<br>and drafts your voice"]
    DRAFT --> FLAG["flag any line the<br>evidence does not support"]
    FLAG --> REV{"accept, regenerate<br>or edit?"}
    REV -->|"regenerate"| DRAFT
    REV -->|"edit"| ED["open in $EDITOR"]
    ED --> REV
    REV -->|"accept"| PM[("persona.md")]

    PM --> EMB["embed every exchange"]
    EMB --> MEM[("memory table")]
    MEM --> PROF["profile each chat<br>with qwen3:14b"]
    PROF --> FACTS[("facts/chat_id.md")]
    FACTS --> CFG["write picked chats<br>into config.yaml as auto"]
    CFG --> RUN

    classDef stop fill:#7f1d1d,stroke:#ef4444,color:#fff
    classDef file fill:#1e3a5f,stroke:#60a5fa,color:#fff
    class MX stop
    class EXJ,PM,MEM,FACTS file
```

The steps are gated on the files they produce, so setup is resumable: run it
again after a crash and it skips whatever already exists. `--setup` forces
every step to run again.

Two details worth knowing. The Ollama check happens *before* the first
question, so you cannot answer the whole interview and then discover the model
was never pulled. And the login writes to a temporary session file, because
Telegram only says who you are after you have authenticated — the file is
renamed to `secrets/<username>.session` once the account is known.

### Driving setup from something other than a terminal

No setup step prints or reads input itself. Every question goes through the
`Prompter` protocol in `bot/setup.py`, and `ConsolePrompter` is the only
implementation that touches a terminal. Telethon's login callbacks are async
for the same reason. A web frontend means writing a second `Prompter`; none of
the steps change.

## Where the code lives

See the file map in [README.md](README.md#files).
