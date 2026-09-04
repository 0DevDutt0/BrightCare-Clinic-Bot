# Phase 2 — datetime resolution and business-hours validation

## Instruction given

No full prompt was written for this phase. The scope came from one line at the end of
the Phase 1 prompt:

> When Phase 1 passes its acceptance criteria, come back and I'll give you Phase 2
> (datetime resolution and business-hours validation).

followed by:

> go ahead with phase 2 from the outline

with the instruction to build it to my own reading and flag every assumption. Those
assumptions are recorded below so the decisions are reviewable rather than buried.

## Architecture

Two layers, split by what each is trusted with:

| | resolves | decides |
|---|---|---|
| **Layer 2** (`app/agent/resolver.py`) | words → a date and a clock time | nothing |
| **Scheduling** (`app/domain/scheduling.py`) | nothing | whether that time is bookable |

The model is never told the opening hours — a test asserts the prompt does not mention
them. A model that misreads "Monday" produces a wrong *date* the user can see and
correct; a model trusted with policy produces a wrong *rule* the user cannot. This
mirrors the Phase 1 decision to answer FAQs from constants.

Layer 2 is separate from Layer 1 because that is what makes the reference point
available. "Tomorrow" is meaningless at classification time, when the message is being
sorted rather than scheduled. The current date and time are injected into the Layer 2
prompt per call.

## Assumptions made

Each of these was a judgement call, not a stated requirement.

1. **Resolution is an LLM call (Layer 2), not a date-parsing library.** `dateutil`
   handles "2026-09-07" but not "tomorrow morning" or "next Tuesday". Phase 1's prompt
   named intent classification "LAYER 1" and deferred resolution to Phase 2, which
   implies a second layer. Cost: booking now takes two model calls instead of one.

2. **A bare weekday means the next occurrence, strictly after today.** "Friday at 9"
   said on a Friday means next Friday, not today. Debatable — the opposite reading is
   defensible — but "strictly after" never books someone into a slot they have already
   walked past.

3. **Parts of the day map to fixed times**: morning 09:00, noon 12:00, afternoon 14:00,
   evening 17:00. Defined in `scheduling.PART_OF_DAY_DEFAULTS` and repeated in the
   prompt so the mapping is testable rather than a per-response whim.

4. **A day with no time means "from opening onwards"**, not an error. "Monday" yields a
   candidate of Monday 09:00, which the booking rule reads as the soonest slot at or
   after the start of that day.

5. **90-day booking horizon.** Beyond it, `TOO_FAR`. Catches a mistyped year rather
   than accepting an appointment in 2027.

6. **A time before opening, or already past earlier today, moves forward within the
   same day.** Asking for 9am at 11:30 offers 11:30, and the reply says why. This is
   permitted by `NEAREST_AVAILABLE_RULE` — "at or after the requested time, on the same
   business day" — and is not a rollover.

7. **Off-grid times round up, never down.** 14:15 becomes 14:30. Rounding back to 14:00
   would offer a slot *before* the requested time, which the rule forbids.

8. **Weekends, past days, and after-hours requests are refused, never rolled over.**
   `next_open_day` only ever appears as a suggestion in reply text. The user has to ask
   again for another day.

9. **`stage` still stays `idle`.** Same reasoning as Phase 1: Phase 3 sets
   `awaiting_slot_confirmation` when it has a real slot to confirm. No new stage was
   added to the `Stage` literal, so the Phase 1 contract is unchanged.

10. **A clarifying question invites a complete restatement** ("Could you give me a day
    and time, like 'Tuesday at 3pm'?") rather than carrying a partial datetime in
    state. A bare "2pm" reply is classified fresh by Layer 1 and resolved against
    today, so asking for the whole thing avoids losing the day. Carrying partial
    context would need a new stage — deferred to Phase 3, which needs one anyway.

## Bug found by live testing

`max_tokens=300`, carried over from Phase 1, caused intermittent HTTP 400
`json_validate_failed` responses with an empty `failed_generation`.

`gpt-oss` is a reasoning model, and on Groq `max_tokens` bounds reasoning tokens *and*
the answer together. Measured reasoning on this task ran to 346–376 tokens, so on
harder inputs the budget was exhausted before a single character of JSON was written.
It failed on exactly the inputs a classifier most needs to get right — "2:15 on Monday",
"7pm on Tuesday" — and passed on easy ones, so it looked like flakiness.

Raised to 1024. `reasoning_effort="low"` also fixes it and is about four times cheaper,
but it read "2:15 on Monday" as 02:15 rather than 14:15 — not a trade worth making for
appointment times.

No unit test could have caught this: every test fakes the model. `tests/test_llm.py`
now asserts the floor and documents why.

## Verified against the real API

Reference Friday 2026-09-04 11:30 IST, `openai/gpt-oss-20b`:

| message | reply |
|---|---|
| book me for 2:15 on Monday | nearest slot Monday 7 September at 2:30 PM, says why it moved |
| I need an appointment at 7pm on Tuesday | no slots left that late, quotes opening hours |
| can I book Monday at 2pm? | Monday 7 September at 2:00 PM |
| do you have anything Saturday? | closed, suggests Monday 7 September |
| can I come in last Tuesday? | that date has already passed |
| how about sometime next week? | asks which date "next week" means |
| can I book at 9am today? | nearest slot Friday 4 September at 11:30 AM |

Layer 2 resolution accuracy: 9/10 phrases resolved exactly, the tenth being a rate-limit
blip rather than a wrong answer.

## Out of scope, still

Calendar availability, event creation, email. `booking.py` says
"I can't confirm it against the calendar yet" because it genuinely cannot: the candidate
slot is validated against clinic *rules* only, not against what is actually free.
