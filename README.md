# BrightCare Clinic — Telegram appointment agent

A conversational agent that answers questions about a fictional clinic and books
appointments over Telegram, backed by Google Calendar with an email confirmation.

**Phase 1 of 5 — the routing layer.** Intent classification, conversation state, and
both Telegram transports are complete. Calendar and email are typed interfaces awaiting
Phases 3 and 4.

## Quick start

```bash
python -m venv venv
venv/Scripts/pip install -r requirements-dev.txt   # Linux/macOS: venv/bin/pip
cp .env.example .env                               # then fill in the blanks
venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Then message the bot on Telegram. `GET /health` reports the resolved run mode.

> `tzdata` is a real dependency, not a nicety. Windows ships no system time zone
> database, so `ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` without it and
> the app cannot construct a single valid appointment time.

## Configuration

Every value comes from the environment; see `.env.example` for the full list. Five keys
are required and validated at startup: `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`,
`GROQ_MODEL`, `GOOGLE_CALENDAR_ID`, `TIMEZONE`.

Google credentials resolve in order:

1. `GOOGLE_SERVICE_ACCOUNT_JSON` — inline JSON, used in deployment.
2. `GOOGLE_SERVICE_ACCOUNT_FILE` — resolved against the **project root**, not the
   working directory, so the app starts identically from anywhere.
3. Neither present → startup fails, rather than deferring to the first calendar call.

In a `.env` file the inline JSON **must be wrapped in single quotes**. Double quotes
make dotenv expand the `\n` escapes inside `private_key` into real newlines, and a raw
newline inside a JSON string is invalid JSON.

SMTP is optional until Phase 4; `settings.email_configured` reports whether it is
complete, so Phase 4 needs no configuration change.

## Transports

Both modes call the **same** `UpdateHandler.handle_update`. Nothing below that line
knows which transport delivered a message, so the agent is testable without either and
deployment switches modes with an env var.

| `RUN_MODE` | Mechanism | When |
|---|---|---|
| `polling` | background `getUpdates` loop | local dev — no public URL needed |
| `webhook` | `POST /telegram/webhook/{secret}` | deployment |

Webhook mode requires `TELEGRAM_WEBHOOK_SECRET` and `PUBLIC_BASE_URL`, checked at
startup. The route **always returns 200**: a non-200 makes Telegram redeliver with
backoff, so a bug that reliably 500s becomes a retry storm that outlives the deploy.

## How a message is routed

```
Step 0  message shape      non-text, /start, /help, empty      no model call, ever
Step 1  conversation state stage != idle → active flow owns it  no model call
Step 2  intent             one Groq call, temperature 0, JSON   the only model call
Step 3  dispatch           greeting | faq | booking | out_of_scope
```

**Step 1 before Step 2 is the load-bearing decision.** If the bot has asked "is 2pm
alright?" and the user replies "yes", classifying that message in isolation returns
`greeting` — confidently, and wrongly. The conversation stage decides who owns a
message; the words alone cannot. A deterministic escape hatch (`cancel`, `stop`,
`start over`, …) clears a stuck flow without spending a classification on every
in-flow message.

Layer 1 captures the time phrase **verbatim** (`"Monday at 2pm"`) and does not resolve
it. Resolution belongs in Phase 2, where the timezone and business-hours context needed
to interpret "tomorrow" actually exists.

Below `confidence < 0.6` the bot asks a clarifying question instead of acting on a
guess. On a timeout, malformed JSON, or schema mismatch it replies gracefully and logs
the exception alongside the raw model output — without which schema drift is invisible
after the fact.

## Design decisions worth defending

**FAQ answers are returned verbatim from `app/domain/business.py`, not phrased by the
model.** Both are permitted; this makes invented clinic facts structurally impossible
rather than merely discouraged, and keeps an FAQ answer at exactly one model call.
Swapping to model phrasing is local to `handlers/faq.py`.

**`StateStore` is async despite the in-memory backend needing no I/O.** The intended
swap is Redis, whose clients are async. A sync interface would force every handler to
change when that lands.

**The OpenAI SDK's own retries are disabled** (`max_retries=0`). Left on, the required
"one retry" silently becomes several, multiplying the wall-clock time a user waits.

**Naive datetimes raise at construction** rather than being caught by convention. A
naive value that reaches Phase 3's slot search either raises deep in the call stack or,
worse, silently represents the wrong wall clock.

**Deduplication is bounded** — a deque plus a set, capped. Telegram redelivers until
acknowledged, and an unbounded set grows for the life of the process.

## Secrets

`.env` and `credentials/` are gitignored. Secrets are `SecretStr` and the service
account payload is a `PrivateAttr`, keeping both out of `repr()`, `model_dump()` and
tracebacks. Logging defends the same data twice: callers log `text_fingerprint()`
(length plus a short hash) rather than message bodies, and a filter scrubs credential
patterns at every level plus emails from user-content fields at INFO and above. `httpx`
is held at WARNING because its INFO request line contains the bot token.

## Tests

```bash
venv/Scripts/python -m pytest
```

110 tests, Groq faked at the `complete_json` seam — the narrowest point that still
exercises parsing, validation and error handling. Several assert work *not* done: a
sticker costs zero model calls, and a mid-flow reply skips the classifier entirely.

## Layout

```
app/
  config.py            settings, credential resolution, ZoneInfo
  logging_config.py    JSON logs, secret redaction
  main.py              FastAPI, lifespan, /health, webhook route
  telegram/            client, handle_update entrypoint, polling runner
  agent/               llm, router (Layer 1), orchestrator, prompts, handlers/
  state/               ConversationState, StateStore + InMemoryStateStore
  domain/business.py   clinic facts, hours, slot grid, booking rule
  services/            calendar + email interfaces (Phases 3-4)
prompts/               the prompt driving each phase
tests/
```

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Scaffold, transports, routing layer | complete |
| 2 | Datetime resolution, business-hours validation | not started |
| 3 | Calendar availability, slot search, event creation | interface only |
| 4 | Email confirmation | interface only |
| 5 | Deployment | not started |
