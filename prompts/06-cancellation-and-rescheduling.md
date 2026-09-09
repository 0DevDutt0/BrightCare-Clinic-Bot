# Phase 6 — cancelling and rescheduling, verified by a one-time code

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

11. **Lookup runs two queries every time, and the exact match is done in code.** An exact
    `privateExtendedProperty` filter, which cannot miss on tokenisation; and Google's
    free-text `q`, which is the only thing that sees events already on the real calendar
    from Phases 3–4, since those carry the address in their prose description alone and
    `q` does not search extended properties. Neither is a superset of the other, so both
    run — concurrently, merged on event id.

    This started as "property first, `q` only if that came up empty", which is one
    request cheaper and wrong: a patient with one legacy appointment and one tagged one
    would have been shown only the tagged one, with no way to cancel the other, for as
    long as the booking horizon. The live check below is what caught it — both shapes now
    come back from one lookup.

    Whatever either returns is then matched exactly on the address, because `q` tokenises
    and offering someone else's appointment for cancellation is the one mistake this must
    not make.

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

`327 → 509` tests. Twenty deliberate mutations were applied to the new code and the suite
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

### Against the live Google Calendar

Run on 2026-09-09 against the real calendar. Two throwaway events were created 45 days
out under a unique `@example.invalid` address — one tagged with `privateExtendedProperty`,
one in the pre-Phase-6 description-only shape — then looked up, cancelled, and cleaned up.
**16/16 checks passed:**

- both generations of event come back from a single `find_upcoming_by_email`
- starts arrive timezone-aware, in the clinic's zone
- the name round-trips via the property, *and* parses out of legacy prose
- a near-miss address (`not-<addr>`) matches nothing
- `events.delete` removes both; the lookup then returns zero and `freeBusy` reports the
  slot free again
- a second delete raises `EventNotFound`, not a generic `CalendarError`

That run is what turned up assumption 11's original ordering bug. It also confirms the
service account has the write scope cancellation needs, which booking never exercised —
it only ever created.

**Still not exercised live:** Gmail SMTP for the two new messages. Phase 4 proved the
transport with a real App Password and this reuses `_send` unchanged, so the risk is in
how the code mail and the `METHOD:CANCEL` `.ics` *render*, not in whether they send.

## Out of scope

**Rescheduling.** "Move my appointment" still classifies as `booking`, which proposes a
new slot and leaves the old one standing. Cancel-then-rebook is now possible in two
turns, but the bot does not join them up.

**Cancelling on the clinic's behalf.** There is no staff path, and no way to cancel
without access to the patient's inbox.

**A rate limit across flows.** The caps here are per-flow. Restarting costs a
classification, which is the only thing bounding repetition, and a real deployment
wanting more would put it at the transport.

---

# Rescheduling, and knowing which one they meant

## Instruction given

> also add reshedule that is canceling an appointment and booking another appointment.
> reshedule that is to change the appointment which is already there to new appointment
> if there is any convinients. when the user didnot directky say cancel or any terms
> related to that then u can ask the user that should u reshedule the appointment or
> cancel the appointment

## What was built

Rescheduling, as specified: a cancellation with a booking attached. And the
disambiguation the second half of that instruction asks for — when the message does not
say which, the bot asks instead of guessing.

`cancellation.py` became `changes.py`, because the two flows are the same conversation
until the very end. Both find the appointment, both prove who is asking, and only the
last step differs. Keeping them apart would have meant two copies of the one-time code.

```
idle
  -> ask which address it was booked with       awaiting_change_email
  -> (several matches) which one?               awaiting_change_choice
  -> (mode unknown) move it or cancel it?       awaiting_change_intent
     |
     +-- cancel      -> read it back, confirm   awaiting_cancel_confirmation
     |
     +-- reschedule  -> what day and time?      awaiting_reschedule_time
                     -> propose a free slot     awaiting_reschedule_confirmation
  -> email a code, ask for it                   awaiting_change_code
  -> verify, then do it, and return to          idle
```

Three Layer 1 intents open it: `cancel`, `reschedule`, and `change_appointment` for
"I can't make Monday" — a message that reports a problem without instructing anything.

## Assumptions made

1. **Rescheduling books before it cancels.** Cancelling first and then failing to book
   leaves a patient with no appointment and no warning. Booking first and then failing to
   cancel leaves them with two, which is visible, recoverable, and said out loud:
   *"I couldn't release the earlier one … please call the clinic so they can clear it."*
   A test asserts the call order, and another asserts the half-done case is reported.

2. **Rescheduling needs the same one-time code as cancelling.** It destroys an
   appointment; that it also creates one does not make it less destructive. Every reason
   cancelling needs proof applies unchanged.

3. **Verification survives inside the flow, and only inside it.** If the new slot is
   taken between entering the code and creating the event, the user picks another time
   without proving themselves twice — same person, same flow, same appointment.
   `reset_flow` clears the flag. Both halves are tested, and the second is the sharper
   one: a flag that outlived the flow would let one code authorise cancelling every
   appointment the chat could subsequently find, under any address.

4. **The mode question waits until the appointment is in view.** Asking "reschedule or
   cancel?" before knowing which appointment would cost a turn. Asked after, it merges
   with the confirmation into one message: *"I found an appointment for Dev on Monday 7
   September at 2:00 PM. Would you like to move it to another time, or cancel it
   altogether?"*

5. **`"cancel"` at that prompt is an answer, not an escape.** The escape hatch matches the
   whole message, and `cancel` is in it — so without an exemption the bot would ask "move
   it or cancel it?", hear "cancel", abandon the flow and reply *"nothing has been
   cancelled"* to someone in the middle of cancelling. `_ESCAPE_EXEMPT` carves out that
   one word at that one stage; `stop`, `quit` and the rest still work there.

6. **A message naming both actions is not guessed at.** "don't cancel it, just move it"
   returns None and re-asks. Picking one from a sentence that named the other is how a
   booking the user meant to keep gets destroyed.

7. **The reschedule flow is the one continuation that costs a model call.** Resolving
   "Friday at 3pm" is Layer 2. Every other in-flow message in the app is free, and the
   orchestrator logs and tests that, so the handler returns a `FlowResult` carrying
   `used_llm` rather than letting the caller assume.

8. **Booking's slot search was extracted, not copied.** `search_for_slot` resolves a
   phrase, checks it against the clinic's rules and finds a free slot; both flows call
   it, and a refused time is refused in the same words. A second copy of that sequence is
   a second place for the opening hours to be got wrong.

9. **Asking to move to the time you already have is answered as such.** The patient's own
   appointment is on the calendar, so the naive answer is the slot *after* it —
   technically true and baffling. Detected on the requested time, before availability.

10. **Declining the offered slot returns to the time prompt, not out of the flow.** They
    still want to move it; they just do not want that slot.

11. **The `.ics` UID is carried forward, and the SEQUENCE is bumped.** This is what makes
    the move a move in the *patient's* calendar rather than a second entry beside the
    first. The clinic's event id necessarily changes — it is a new event — so the UID is
    stored on the event as a private property, along with a sequence number. Without the
    sequence the carry-forward works exactly once: a client ignores an update whose
    SEQUENCE has not increased, so a second reschedule would silently leave the patient
    looking at the first new time. Both are verified against the live calendar below.

12. **One email for a move, not a cancellation plus a confirmation.** Two mails for one
    action are noise, and worse, they race — arriving out of order they read as
    "cancelled" last.

13. **`event_id` was renamed to `ics_uid` throughout the email service.** It was only ever
    used as the calendar UID, and after a reschedule the two are different things. A name
    that is accurate until the first reschedule is a name that will mislead someone.

## Verified

`462 → 509` tests. Twelve further mutations were run against the reschedule code; all
twelve turn the suite red, including:

| mutation | |
|---|---|
| cancel the old appointment before booking the new one | caught |
| skip the re-check on the new slot | caught |
| give the moved appointment a fresh calendar UID | caught |
| reuse the same SEQUENCE on every move | caught |
| stay quiet when the old appointment could not be released | caught |
| guess "cancel" when the user named both actions | caught |
| let the escape hatch swallow "cancel" at the which-one prompt | caught |
| report the reschedule continuation as free | caught |
| make them verify again after a lost race | caught |
| offer the slot after the one they already hold | caught |
| **drop the verified flag out of `reset_flow`** | **initially MISSED** |

That last row was a genuine hole, and the worst kind: the mutation leaves every test
green while letting one code authorise cancelling any appointment the chat finds
afterwards. Two tests now close it.

### Against the live Google Calendar

An appointment was booked, moved, and cleaned up on the real calendar. **14/14 checks
passed**, including the parts no fake can prove:

- a fresh booking's UID *is* its event id, and its sequence starts at 0
- after the move exactly one appointment remains, at the new time, under a genuinely new
  event id — but **carrying the original UID**, with the sequence bumped to 1
- the earlier slot is free again
- `ics_uid` and `ics_sequence` survive the round trip through Google's
  `extendedProperties` (the sequence comes back as the string `"1"`, which is why it is
  parsed defensively)

### A fake that lied

Worth recording. `FakeCalendarService` numbered created events `evt-{len(created)}`,
which collided with ids a test had seeded — so a reschedule cancelled the event it had
just created, and the fake reported success. Three tests caught it, but only because they
asserted what was *left on the calendar* rather than what was returned. The fake now
skips ids already in use.

## Out of scope, still

**Cancelling or moving on the clinic's behalf.** There is no staff path, and no way to
change an appointment without access to the patient's inbox.

**Rescheduling beyond the horizon.** The new time is bound by the same 90-day rule as a
booking.

**A rate limit across flows.** The caps here are per-flow. Restarting costs a
classification, which is the only thing bounding repetition.
