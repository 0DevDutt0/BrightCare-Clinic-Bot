# Phase 3 — calendar availability, slot search, event creation

## Instruction given

As with Phase 2, no full prompt was written. The scope came from the Phase 1 layout note:

> `services/calendar_service.py`  # Phase 3: interface + NotImplementedError stub

and the phase list:

> | 3 | Calendar availability, slot search, event creation |

followed by:

> go ahead with phase 3 from the outline

Assumptions are recorded below.

## What was built

The booking conversation, end to end. `GoogleCalendarService` is live, and the stage
machine fixed in Phase 1 now runs:

```
idle
  -> propose a genuinely free slot      awaiting_slot_confirmation
  -> collect the patient's address      awaiting_email
  -> read the whole thing back          awaiting_final_confirmation
  -> create the event, return to        idle
```

No new stage was added. The four in the Phase 1 `Stage` literal were exactly enough.

## Assumptions made

1. **Email collection belongs to Phase 3, not Phase 4.** The patient's address has to go
   in the event *description* — attendees are impossible, see below — so collecting it
   is calendar work. Phase 4 then only implements `SmtpEmailService.send_confirmation`
   and calls it after creation, changing no flow.

2. **The whole flow is deterministic below the first turn.** Classifying "yes" would
   double the cost of every booking and add a failure mode to the least ambiguous
   message in the conversation. A booking costs two model calls total, both on turn one.

3. **Yes/no requires every word to be recognised.** "yeah go ahead" is a yes;
   "ok Tuesday" is not, because it names a day the bot never proposed and confirming
   the old slot on it would book the wrong appointment. Unrecognised replies go back to
   the classifier rather than being answered with "please say yes or no" — a user
   replying "actually can we do 4pm?" is changing the request, not failing to answer it.

4. **Availability is re-checked immediately before creating.** A slot free when proposed
   can be taken while the user types their email.

5. **The patient's name comes from Telegram's `from.first_name`**, not from a question.
   Telegram already knows; asking would waste a turn. It is never logged.

6. **A calendar failure at the final step keeps the user in the flow** rather than
   dropping them to idle, so they can retry without re-picking a slot. A failure while
   *proposing* does return to idle, because there is nothing yet to hold onto.

7. **Overlap is half-open at both ends.** Back-to-back appointments are not clashes.
   Treating a shared boundary as a collision would lose the slot either side of every
   existing event — roughly a third of a working day, for nothing.

8. **90-day horizon and the no-rollover rule from Phase 2 are unchanged.** A fully
   booked day is reported, and the next open day is *suggested* in words, never booked.

## Constraint discovered by testing, not by reading

A service account **cannot invite attendees**:

```
HTTP 403 forbiddenForServiceAccounts:
Service accounts cannot invite attendees without Domain-Wide Delegation of Authority.
```

Delegation requires a Google Workspace domain, which a personal `gmail.com` calendar
cannot grant. This is not a degraded mode: one `attendees` field fails the *entire*
event creation, so a booking that tried it would fail outright.

`create_event` therefore never sends attendees and writes the address into the
description instead. The event is the clinic's record; the SMTP mail in Phase 4 is the
patient's. They are independent, which also means a failed email must not roll back a
created appointment.

## Dependency choices

`google-auth` **without** the `[requests]` extra. Its default transport would pull a
second HTTP stack into an app that already uses httpx, so
`calendar_service.HttpxAuthTransport` supplies an httpx one. Its `refresh` is
synchronous, so it runs in a thread behind a lock — once an hour, not per request.

**No `google-api-python-client`.** It is synchronous and builds its own transport. Only
four endpoints are needed (`freeBusy`, `events.list`, `events.insert`, `events.delete`),
so they are called directly.

`email-validator`, via pydantic's `EmailStr` machinery. A regex finds the candidate; the
library decides whether it is real. A wrong address means the confirmation silently
never arrives.

## Verified end to end

Real Groq, real Google Calendar, Friday 2026-09-04 13:44 IST:

```
USER  can I book Monday at 2pm?
BOT   Monday 7 September at 2:00 PM is free. It runs 30 minutes. Shall I book it?   [2 calls]
USER  yes please
BOT   Lovely. What email address should I send the confirmation to?                 [0 calls]
USER  devduttshoji123@gmail.com
BOT   Thanks. To confirm: Monday 7 September at 2:00 PM at 12 Orchard Rd ...        [0 calls]
USER  yes
BOT   Booked. Your appointment is Monday 7 September at 2:00 PM at 12 Orchard Rd.   [0 calls]
```

The event appeared on the real calendar:

```
summary  : Appointment - Dev
start    : 2026-09-07T14:00:00+05:30
end      : 2026-09-07T14:30:00+05:30
attendees: none (as designed)
descr    : Booked via the BrightCare Clinic Telegram assistant.
           Patient: Dev

           Patient email: devduttshoji123@gmail.com
```

and was deleted again by the test's cleanup.

## Out of scope, still

Email sending. `SmtpEmailService.send_confirmation` remains `NotImplementedError`, and
the booking reply promises "a confirmation email will follow shortly" — which is a lie
until Phase 4 lands. Worth fixing first thing in that phase, either by sending it or by
softening the sentence.
