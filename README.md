# BrightCare Clinic — Telegram appointment agent

A conversational agent that answers questions about a fictional clinic and books
appointments over Telegram, backed by Google Calendar with an email confirmation.

**Phases 1–3 of 5 complete.** The bot books real appointments on a real Google
Calendar. Intent classification, datetime resolution, business-hours validation,
availability search, event creation, conversation state and both Telegram transports
are done. Email confirmation is a typed interface awaiting Phase 4.

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
Step 2  intent (Layer 1)   one Groq call, temperature 0, JSON
Step 3  dispatch           greeting | faq | booking | out_of_scope
          └─ booking only: Layer 2 resolves the time phrase     second Groq call
```

Greetings, FAQs and refusals cost exactly one call. Booking costs two, both on the
first turn. A message that never reaches Step 2 costs none — and neither does any turn
of the confirmation flow below.

### The booking conversation

```
idle
  -> propose a genuinely free slot      awaiting_slot_confirmation
  -> collect the patient's address      awaiting_email
  -> read the whole thing back          awaiting_final_confirmation
  -> create the event, return to        idle
```

Every transition is deterministic — classifying "yes" would double the cost of a booking
and add a failure mode to the least ambiguous message in the conversation. Yes/no
parsing requires *every* word to be recognised, so "yeah go ahead" confirms but
"ok Tuesday" does not: it names a day the bot never proposed, and confirming the old
slot on it would book the wrong appointment. Anything unrecognised goes back to the
classifier, so "actually can we do 4pm?" re-proposes instead of being told to say yes
or no.

**Availability is re-checked immediately before the event is created.** A slot that was
free when proposed can be taken while the user types their email, and booking on the
earlier answer is how two patients end up in one slot.

**Step 1 before Step 2 is the load-bearing decision.** If the bot has asked "is 2pm
alright?" and the user replies "yes", classifying that message in isolation returns
`greeting` — confidently, and wrongly. The conversation stage decides who owns a
message; the words alone cannot. A deterministic escape hatch (`cancel`, `stop`,
`start over`, …) clears a stuck flow without spending a classification on every
in-flow message.

Layer 1 captures the time phrase **verbatim** (`"Monday at 2pm"`) and does not resolve
it. Layer 2 does that, with the current date and time injected into its prompt — which
is the whole reason the layers are separate. "Tomorrow" is meaningless at classification
time, when the message is being sorted rather than scheduled.

### Two layers, split by what each is trusted with

| | resolves | decides |
|---|---|---|
| **Layer 2** (`agent/resolver.py`) | words → a date and clock time | nothing |
| **Scheduling** (`domain/scheduling.py`) | nothing | whether that time is bookable |

The model is never told the opening hours, and a test asserts the prompt doesn't mention
them. A model that misreads "Monday" produces a wrong *date* the user can see and
correct; a model trusted with policy produces a wrong *rule* they cannot.

Validation refuses rather than reroutes. Weekends, past dates, and after-hours requests
are declined with an explanation — `next_open_day` only ever *suggests* an alternative,
because `NEAREST_AVAILABLE_RULE` forbids rolling a booking onto another day. Within a
day it does move forward: 14:15 becomes 14:30 (always rounding up, never offering a slot
before the one asked for), and asking for 9am at 11:30 offers 11:30 with the reason
stated. A silent shift is how a patient turns up an hour out.

Full assumption list in `prompts/02-datetime-resolution.md`.

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

**`max_tokens` is 1024, and the reason is not obvious.** Reasoning models spend hidden
reasoning tokens *inside* that budget before writing any answer — measured at 346–376
on this task. The original 300 left no room for the JSON, and Groq rejected the call
with HTTP 400 `json_validate_failed` and an empty `failed_generation`. It failed on
exactly the inputs a classifier most needs to get right ("2:15 on Monday", "7pm on
Tuesday") and passed on easy ones, so it presented as flakiness. No unit test could
catch it, since every test fakes the model; `tests/test_llm.py` asserts the floor and
records why.

**Naive datetimes raise at construction** rather than being caught by convention. A
naive value reaching the slot search either raises deep in the call stack or, worse,
silently represents the wrong wall clock.

**Calendar events never carry attendees.** A service account without Domain-Wide
Delegation gets `HTTP 403 forbiddenForServiceAccounts`, and that fails the *entire*
event creation rather than degrading — one attendee field would break every booking.
Delegation needs a Workspace domain a personal calendar cannot grant, so the patient's
address goes in the event description and their confirmation is Phase 4's SMTP mail.
The event is the clinic's record; the email is the patient's.

**Slot overlap is half-open at both ends**, so back-to-back appointments are not
clashes. Treating a shared boundary as a collision would lose the slot either side of
every existing event — about a third of a working day, for nothing.

**No `google-api-python-client`.** It is synchronous and builds its own HTTP stack;
only four endpoints are needed, so they are called over httpx directly. `google-auth`
is installed without its `[requests]` extra for the same reason, with an httpx
transport supplied in `calendar_service.py`.

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

300 tests, Groq faked at the `complete_json` seam — the narrowest point that still
exercises parsing, validation and error handling. Several assert work *not* done: a
sticker costs zero model calls, a mid-flow reply skips the classifier entirely, and a
booking with no time phrase never reaches Layer 2.

Google is faked at the HTTP layer with respx, so request bodies are asserted rather
than assumed — that attendees are never sent, that the slot end comes from the domain's
slot length, and that a per-calendar `freeBusy` error is not mistaken for a free day.

Verified by mutation, not just by passing: rounding slots down, dropping the weekend
check, rolling a late request to the next day, acting on a low-confidence resolution,
leaving a placeholder unsubstituted in a prompt, skipping the pre-booking re-check,
closing the overlap boundaries, or accepting "ok Tuesday" as a yes each turns the
suite red.

## Layout

```
app/
  config.py            settings, credential resolution, ZoneInfo
  logging_config.py    JSON logs, secret redaction
  main.py              FastAPI, lifespan, /health, webhook route
  telegram/            client, handle_update entrypoint, polling runner
  agent/               llm, router (Layer 1), resolver (Layer 2), orchestrator,
                       prompts, handlers/
  state/               ConversationState, StateStore + InMemoryStateStore
  domain/business.py   clinic facts, hours, slot grid, booking rule
  domain/scheduling.py business-hours validation, slot alignment
  services/            calendar (live) + email interface (Phase 4)
prompts/               the prompt driving each phase
tests/
```

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Scaffold, transports, routing layer | complete |
| 2 | Datetime resolution, business-hours validation | complete |
| 3 | Calendar availability, slot search, event creation | complete |
| 4 | Email confirmation | interface only |
| 5 | Deployment | not started |
