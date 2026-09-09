# Phase 6 — cancellation, verified by a one-time code

## Instruction given

> i need to add cancelation by first checking and confirming the email with appointment,
> user, time and send a confirmation email(like random otp ) and the telegram bot should
> ask the user to enter the OTP and confirm with OTP send through email. then cancel the
> appointment and send a message accordingly.

Assumptions recorded below.

## What was built

A second conversational flow beside booking, and the first one where the bot does
something it cannot take back.

```
idle
  -> ask which address it was booked with       awaiting_cancel_email
  -> (several matches) which one?               awaiting_cancel_choice
  -> read the appointment back                  awaiting_cancel_confirmation
  -> email a 6-digit code, ask for it           awaiting_cancel_code
  -> verify, delete the event, return to        idle
```

New pieces:

| file | what it is |
|---|---|
| `app/domain/otp.py` | code generation, hashing, expiry, the attempt budget |
| `app/agent/parsing.py` | `read_yes_no` / `extract_email`, lifted out of `booking.py` so both flows share one reader |
| `app/agent/handlers/cancellation.py` | the flow |
| `calendar_service.find_upcoming_by_email` | lookup by patient address |
| `calendar_service.cancel_event` | the delete, plus `EventNotFound` |
| `email_service.send_cancellation_code` | the code mail |
| `email_service.send_cancellation_confirmation` | the receipt, with a `METHOD:CANCEL` `.ics` |
| `cancel` intent | Layer 1, with the FAQ boundary spelled out in the prompt |

The whole flow costs **one model call** — the classification on the first message.
Nothing after it is a judgement call.

## The problem this flow exists to solve

Telegram tells us that this chat is the same chat as yesterday. It does not tell us
which patient that is. The only link between a chat and an appointment is the email
address the booking was made with, and an address anyone can type is a claim, not proof.

The code settles it: whoever answers with it controls the inbox the appointment was
booked from. That is the whole design, and everything else follows from it.

## Assumptions made

1. **The email is asked for every time**, even when the same conversation booked
   something ten messages earlier and `patient_email` is sitting in state. Reusing it
   would skip the step that establishes who is asking and reduce the code to a formality
   posted to an address the requester never had to know. Tested directly.

2. **Six digits, three attempts, ten minutes.** The strength is in the attempt budget,
   not the length: three guesses against 10⁶ is three in a million, which is the number
   that matters. The expiry bounds something different — how long a code left in an inbox
   stays useful.

3. **Only a salted hash is stored.** The plaintext exists in one local variable, long
   enough to be put in an email, and is then dropped. Six digits is a small space, so a
   salted hash of one is brute-forceable in milliseconds by anyone who already holds the
   state — this defends against the code being *seen* in a Redis snapshot, a log line or
   a traceback, not against an attacker who owns the process. A test asserts the code
   appears in neither `model_dump_json()` nor the conversation history.

4. **A correct code spends no attempt; an unreadable reply spends none either.** Only a
   well-formed 6-digit guess costs one of the three. "what?" or "12345" is noise, not a
   guess, and punishing it would burn a third of the budget for a typo. A correct code
   costing nothing is what lets a retry after a transient calendar failure work without a
   fresh code.

5. **`"cancel"` cannot mean "no" inside a cancellation.** In a booking, "cancel it" is a
   refusal. Here the same two words mean *do the thing I asked for*. `read_yes_no` takes
   a `neutral` set, and this flow passes `{"cancel"}` — so "cancel it" decides nothing
   and re-asks, while "yes cancel it" is still a yes and "no don't cancel" is still a no.
   Simply dropping the word from the known list would have turned all three into
   "unclear", throwing away two answers the user gave plainly.

6. **Backing out gets its own sentence.** The existing escape hatch replies *"No problem,
   I've cleared that"*, which is fine after a booking and reads as *"cleared your
   appointment"* after this. Mid-cancellation it now says the appointment is still
   booked. This is the one sentence in the app a patient must not misread.

7. **An unrecognised reply re-asks; it never reroutes.** Booking hands a puzzling reply
   back to the classifier because "actually can we do 4pm?" is a changed request. Nothing
   at a cancellation prompt reinterprets that way, and silently dropping the flow while
   confirming a destructive action is the wrong instinct. The escape hatch is how a user
   leaves.

8. **A code that cannot be sent ends the flow.** Parking someone at a prompt for a code
   that never left the building is worse than telling them. It also means an unconfigured
   SMTP deployment cannot cancel at all — booking degrades to "no email"; cancellation
   stops, because the code has nowhere to go.

9. **No code is ever sent to an address with no appointment.** This falls out of the
   ordering and is worth stating: the bot cannot be used to make mail arrive at an
   arbitrary address.

10. **The lookup prompt stays open after a miss, but only five times.** A miss keeps the
    prompt open so someone with a work address and a personal one need not start over —
    and an open prompt is a free calendar query per message, which is exactly what makes
    address probing worth attempting. Restarting the flow costs a classification, so the
    cap turns free probing back into paid.

11. **Lookup is two queries, and the exact match is done in code.** An exact
    `privateExtendedProperty` filter first; Google's free-text `q` as a fallback, because
    events already on the real calendar from Phases 3–4 carry the address only in their
    prose description. Whatever either returns is then matched exactly on the address,
    because `q` tokenises and offering someone else's appointment for cancellation is the
    one mistake this must not make.

12. **`create_event` now tags events** with a lowercased `patient_email` private
    property. Invisible to anyone reading the calendar, and an exact server-side filter —
    unlike the description, which can only be searched as prose.

13. **Delete, not `status: cancelled`.** Google hides cancelled events from `freeBusy`
    either way, so the slot returns in both cases. Delete is the operation that says what
    happened, and a clinic needing an audit trail needs a record of its own, not a
    tombstone on a calendar Google eventually purges.

14. **`EventNotFound` subclasses `CalendarError`**, so every existing `except
    CalendarError` still catches it while cancellation can single it out and say "already
    cancelled" rather than "something went wrong". A 500 must never be reported as
    "already cancelled" — that would leave a live appointment behind. Tested both ways.

15. **A failed receipt does not undo a real cancellation** — the same rule Phase 4 set
    for booking, and the reply says which of the two happened.

16. **The `.ics` UID is now derived from the Google event id.** It was `uuid4()`, which
    meant nothing could ever refer back to it. The cancellation attaches
    `METHOD:CANCEL` + `SEQUENCE:1` with the *same* UID, so a client that honours the
    pairing removes the appointment instead of leaving a ghost. Support is uneven across
    mail clients, so the prose says "cancelled" too — the retraction is worth sending,
    not worth relying on. `event_id` is an optional keyword throughout, so nothing that
    did not pass it changed.

17. **Multiple matches are disambiguated by a numbered list, not by Layer 2.** Resolving
    "Monday" and hoping it names exactly one appointment is a model call spent to become
    less certain. The choice reader only considers short replies, so a question that
    happens to contain a digit cannot be mined for one.

18. **`/cancel` was not added as a command.** For a clinic user it would naturally mean
    "cancel my appointment"; for a bot user it conventionally means "abort". Ambiguous
    either way, so it stays out — `CAPABILITIES` now mentions cancelling instead, and an
    unknown command replies with it.

19. **The `cancellation` FAQ fact was rewritten.** It said the team would take care of
    it. The bot can now do it, and the classifier prompt separates *cancelling* from
    *asking how to cancel* — a question deserves an answer, not an unrequested
    destructive flow.

## Known trade-off, deliberately accepted

**The appointment is described before the code is sent.** Type an address that has a
booking and the bot names the patient and the time before any verification happens. That
discloses whether a given address has an appointment, and to whom.

It is the flow as specified — *"first checking and confirming the email with appointment,
user, time"* — and the requester has already asserted the address, so nothing is revealed
that they did not claim to know. The alternative, if it is ever wanted, is a two-line
change: reply "if that address has an appointment, I've sent it a code" and move the
details to *after* verification. That costs the ability to say "no, wrong one" before a
code is sent.

Two things bound it as it stands: no mail is generated for an address with no
appointment, and after a wrong code nothing further is revealed.

## Verified

`327 → 462` tests. Twenty deliberate mutations were applied to the new code and the suite
was run against each; all twenty turn it red:

| mutation | |
|---|---|
| read `"cancel"` as a refusal inside a cancellation | caught |
| leave the one-time code alive across flows | caught |
| let the plaintext code reach serialised state | caught |
| draw codes from a predictable source | caught |
| spend an attempt on a reply that was never a code | caught |
| expire a code only *after* the attempt is spent | caught |
| keep the flow alive after three wrong codes | caught |
| keep sending the old code after a resend | caught |
| let the resend budget run forever | caught |
| delete the event before checking the code | caught |
| trust Google's free-text search without an exact re-check | caught |
| skip the no-match branch entirely | caught |
| let the lookup prompt stay open forever | caught |
| reset to idle after a miss, breaking the "send that one" offer | caught |
| report a calendar 500 as "already cancelled" | caught |
| park the user at a code prompt when the mail failed | caught |
| say "I've cleared that" when backing out of a cancellation | caught |
| reuse the booking address instead of asking | caught |
| mine a long reply for a stray number at the choice prompt | caught |
| give the cancellation `.ics` a fresh UID | caught |

Two of those started as holes in the suite — the last two rows — and the tests that close
them were written in response.

The conversation, driven through the real orchestrator against fakes:

```
USER  I need to cancel my appointment
BOT   I can help with that. Which email address was the appointment booked with?
      I'll send a code there to check it's you.                          [1 call]

USER  dev@example.com
BOT   I found an appointment for Dev on Monday 7 September at 2:00 PM at 12 Orchard
      Rd, booked with dev@example.com.
      Shall I cancel it? I'll email a 6-digit code to that address first, to check
      it's you.                                                          [0 calls]

USER  yes
BOT   I've emailed a 6-digit code to dev@example.com. Reply with it here and I'll
      cancel your appointment on Monday 7 September at 2:00 PM. It expires in 10
      minutes.                                                           [0 calls]

USER  421052
BOT   Done - your appointment on Monday 7 September at 2:00 PM is cancelled and the
      slot is free again. I've emailed you a confirmation.               [0 calls]
```

Not yet exercised against the live Google Calendar and Gmail SMTP; Phases 3 and 4 were,
and this reuses their transport unchanged, but the two new endpoints — `events.list` with
`privateExtendedProperty`, and `events.delete` — have only been driven against `respx`.

## Out of scope

**Rescheduling.** "Move my appointment" still classifies as `booking`, which proposes a
new slot and leaves the old one standing. Cancel-then-rebook is now possible in two
turns, but the bot does not join them up.

**Cancelling on the clinic's behalf.** There is no staff path, and no way to cancel
without access to the patient's inbox.

**A rate limit across flows.** The caps here are per-flow. Restarting costs a
classification, which is the only thing bounding repetition, and a real deployment
wanting more would put it at the transport.
