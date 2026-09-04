# Project: BrightCare Clinic — Telegram appointment booking agent (Phase 1)

## Context
I'm building a take-home project: a conversational agent that books appointments for a
fictional clinic over Telegram, backed by a real Google Calendar, with an email
confirmation. This is **Phase 1 of 5**. Build ONLY what's in "Phase 1 scope" below, but
create the folder structure and interfaces so later phases slot in without refactoring.

## Stack & constraints
- Python 3.11+, FastAPI. Pin dependencies in `pyproject.toml` or `requirements.txt`.
- LLM: Groq via the OpenAI-compatible SDK. Model name from env, never hardcoded.
- pydantic v2 + pydantic-settings for config and all LLM structured output.
- No secrets in code. Everything via `.env`, with a committed `.env.example`.
- Structured logging (stdlib `logging`). Log every inbound update, the routed intent,
  and latency. Never log credentials or message bodies containing emails at INFO.
- Make small, meaningful git commits as you go — the repo history is graded.

## Repo layout to create
````
app/
  main.py                 # FastAPI app, lifespan, health check, webhook route
  config.py               # Settings via pydantic-settings
  logging_config.py
  telegram/
    client.py             # send_message, set_webhook, get_updates
    poller.py             # long-polling runner
    handler.py            # handle_update() — single entrypoint, transport-agnostic
  agent/
    llm.py                # Groq wrapper: JSON-mode call + retry + timeout
    router.py             # LAYER 1: intent classification
    orchestrator.py       # dispatch: state check -> intent -> handler
    prompts.py            # all system prompts as constants
    handlers/
      greeting.py
      faq.py
      booking.py          # Phase 1: stub only
      out_of_scope.py
  state/
    models.py             # ConversationState
    store.py              # StateStore ABC + InMemoryStateStore
  domain/
    business.py           # clinic constants, hours, FAQ content, booking rules
  services/
    calendar_service.py   # Phase 3: interface + NotImplementedError stub
    email_service.py      # Phase 4: interface + NotImplementedError stub
tests/
prompts/                  # save each phase's prompt here as a .md file
credentials/              # gitignored
.env.example
README.md
````

## Config loading (`app/config.py`)
Use pydantic-settings. Required at startup — fail fast with a clear error naming the
missing key: `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`, `GROQ_MODEL`, `GOOGLE_CALENDAR_ID`,
`TIMEZONE`.

Service account credentials resolve in this order:
1. If `GOOGLE_SERVICE_ACCOUNT_JSON` is set and non-empty, parse it as JSON.
2. Else load the file at `GOOGLE_SERVICE_ACCOUNT_FILE`, resolved relative to the
   **project root**, not the current working directory.
3. If neither is available, raise at startup — do not defer to the first calendar call.

Never log credential contents or any part of `private_key`.

SMTP settings are optional in Phase 1 (email lands in Phase 4), but define the fields
now and expose a `settings.email_configured` boolean so Phase 4 changes nothing here.

`TIMEZONE` loads once into a `ZoneInfo` object. Every datetime in the app is tz-aware —
add a test asserting no naive datetimes are produced.

## `.env.example` (must match this exactly)
````
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
RUN_MODE=polling
PUBLIC_BASE_URL=
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
GOOGLE_CALENDAR_ID=
GOOGLE_SERVICE_ACCOUNT_FILE=credentials/service-account.json
GOOGLE_SERVICE_ACCOUNT_JSON=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
FROM_EMAIL=
FROM_NAME=BrightCare Clinic
TIMEZONE=Asia/Kolkata
LOG_LEVEL=INFO
````

## `.gitignore` (create in this phase)
`.env`, `credentials/`, `__pycache__/`, `.venv/`, `.pytest_cache/`, `*.pyc`

Before the first commit, verify with `git status` that `credentials/service-account.json`
is untracked. If it is tracked, stop and tell me — the key must be rotated, not just
removed from the index.

## Telegram transport — build both, switch by env
Long polling for local dev (no tunnel needed), webhook for deployment.
- `RUN_MODE=polling` → background runner calling `getUpdates` in a loop.
- `RUN_MODE=webhook` → `POST /telegram/webhook/{secret}`, comparing `{secret}` against
  `TELEGRAM_WEBHOOK_SECRET`.
- **Both paths call the exact same `handle_update(update)` function.** The agent core
  must be transport-agnostic — I need to justify this design in an interview.
- The webhook route **always returns HTTP 200**, even on internal errors, so Telegram
  doesn't retry-storm. Catch, log, and reply to the user with a graceful failure message.
- Deduplicate by `update_id` using a bounded in-memory set — Telegram redelivers.
- Add `GET /health` returning app status and resolved run mode.

## Domain constants (`app/domain/business.py`)
Single source of truth, read by every later phase:
- Name: BrightCare Clinic
- Hours: Mon–Fri 09:00–18:00, closed Saturday and Sunday
- Slot length: 30 minutes; slots start on :00 and :30
- Timezone: from `TIMEZONE`
- FAQ facts: location `12 Orchard Rd`; walk-ins NOT accepted (appointment only); to
  cancel, message the clinic; on-site parking available
- Booking rule constant (used in Phase 3): "nearest available" = the soonest free
  30-minute slot **at or after** the requested time, **on the same business day**. If
  none remain that day, tell the user — do NOT roll over to the next day.

## Phase 1 scope — the routing layer
Every inbound message goes through the orchestrator in this order.

**Step 0 — deterministic pre-checks, no LLM call:**
- Ignore `edited_message` and channel posts. Non-text messages (photo, voice, sticker) →
  "I can only read text messages right now."
- `/start` and `/help` → static welcome describing what the bot can do.
- Empty or whitespace-only text → ask for clarification.

**Step 1 — state check BEFORE intent classification.**
This is critical. If `state.stage != "idle"`, the message is a *continuation* of an
active flow (the user replying "yes" to a proposed slot, or sending their email) and
must route to the active flow handler, NOT be re-classified as a fresh intent. "yes" is
not a greeting, an FAQ, or a booking request on its own. Include an escape hatch: if the
user clearly changes topic mid-flow, clear the state. The flows are stubs in Phase 1,
but the wiring must exist and be covered by a test.

**Step 2 — intent classification (LAYER 1).**
One Groq call, temperature 0, JSON mode, returning exactly:
````json
{
  "intent": "greeting" | "faq" | "booking" | "out_of_scope",
  "confidence": 0.0,
  "faq_topic": "location|hours|walk_ins|cancellation|parking|appointment_length|null",
  "raw_datetime_text": "verbatim time phrase from the message, or null"
}
````
- Do NOT parse or resolve the datetime here — capture the raw phrase only. Resolution
  happens in Phase 2.
- Validate with a pydantic model. On parse failure, timeout, or API error: fall back to
  a "having trouble right now" reply and log the exception alongside the raw LLM output.
- One retry with backoff on transient errors, then give up. Hard timeout on the call.
- If `confidence < 0.6`, ask a clarifying question rather than guessing.

**Step 3 — dispatch:**
- `greeting` → short friendly reply plus one line on what the bot can do.
- `faq` → answer from `business.py` constants. **The LLM must not invent clinic facts.**
  Pass the relevant fact into the prompt and let it phrase the reply, or return a
  template. If `faq_topic` is null but intent is `faq`, ask what they'd like to know.
- `booking` → Phase 1 stub: acknowledge, echo the captured `raw_datetime_text`, reply
  "Booking isn't wired up yet." Store `raw_datetime_text` in state.
- `out_of_scope` → politely decline and redirect. Anything that isn't greeting, FAQ, or
  booking stops here and goes no further. Never attempt to answer it.

## State model (`app/state/models.py`)
````python
stage: Literal["idle", "awaiting_slot_confirmation", "awaiting_email",
               "awaiting_final_confirmation"]
chat_id: int
requested_start: datetime | None
proposed_start: datetime | None
patient_email: str | None
patient_name: str | None
history: list[Turn]          # last N turns, capped
updated_at: datetime
````
`StateStore` is an abstract interface with an `InMemoryStateStore` implementation (dict
keyed by `chat_id`, TTL expiry after ~30 minutes idle). I want to swap in Redis later
without touching handler code.

## Tests (`tests/`)
pytest, Groq client mocked. Cover:
- Each intent routes to the correct handler.
- Non-idle state bypasses the classifier entirely.
- Malformed / non-JSON LLM output falls back safely without raising.
- Duplicate `update_id` is dropped.
- `/start` and non-text messages never reach the LLM.
- No naive datetimes are produced anywhere.

## Acceptance criteria
`RUN_MODE=polling` starts the app, and in the real Telegram bot:
- "hi" → greeting
- "where are you located?" → 12 Orchard Rd
- "do you take walk-ins?" → no, appointment only
- "can I book Monday at 2pm?" → booking stub echoing "Monday at 2pm"
- "what's the weather in Paris?" → polite refusal, no attempt to answer
- send a sticker → "text only" reply, zero LLM calls in the logs

## Out of scope for Phase 1 — do NOT build yet
Google Calendar calls, datetime resolution, slot search, event creation, email sending,
deployment config. Leave `calendar_service.py` and `email_service.py` as typed
interfaces raising `NotImplementedError`.

## First steps
1. Show me the planned file tree and `requirements.txt` before writing implementation
   code, so I can confirm the dependency choices.
2. Then build in this order: config → logging → domain → state → telegram client →
   handler → llm → router → handlers → orchestrator → main → tests.
3. Save this prompt verbatim to `prompts/01-scaffold-and-router.md` and commit it.

---

Two things worth checking by hand once it runs: that `/health` responds, and that a
sticker produces zero Groq calls in the logs. The second one tells you the deterministic
pre-checks are actually short-circuiting rather than falling through to the classifier.

When Phase 1 passes its acceptance criteria, come back and I'll give you Phase 2
(datetime resolution and business-hours validation).
