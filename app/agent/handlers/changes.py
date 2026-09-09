"""Changing an appointment that already exists: cancelling it, or moving it.

One module, because the two are the same conversation until the very end. Both have to
find the appointment, and both have to answer a question booking never asks: *is the
person typing entitled to do this?* Rescheduling is a cancellation with a booking
attached, so it inherits every reason cancelling needs proof.

Telegram answers a different question. It proves this chat is the same chat as
yesterday; it says nothing about which patient that is. The only link between a chat and
an appointment is the email address it was booked with -- and an address a stranger can
type is a claim, not proof. A one-time code sent to that address settles it: whoever
replies with it controls the inbox the appointment was booked from.

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

Six details worth their weight:

*The mode can arrive unknown.* "I can't make Monday" is a problem with an appointment,
not an instruction about it, and guessing wrong either destroys a booking the user meant
to keep or leaves one they meant to drop. When Layer 1 cannot tell, the flow asks -- but
not until it has the appointment in view, so the question and the confirmation are one
message rather than two.

*Rescheduling books before it cancels.* The other order risks leaving a patient with no
appointment at all if the new slot fails; this order risks leaving them with two, which
is recoverable and is reported in plain words when it happens.

*Verification survives inside the flow, not beyond it.* If the new slot is taken between
entering the code and creating the event, the user picks another time without proving
themselves again. ``reset_flow`` clears the flag, so it only ever authorises the one
appointment this flow already identified.

*An unrecognised reply re-asks; it never reroutes.* Booking hands a puzzling reply back
to the classifier, because "actually can we do 4pm?" is a changed request. Nothing at one
of these prompts reinterprets that way, and silently dropping the flow while confirming a
destructive action is the wrong instinct. The escape hatch is how a user leaves.

*"cancel" cannot be read as "no" here.* In a booking, "cancel it" means don't book. In
these flows the same two words mean do the thing I asked for. The word is passed to
:func:`~app.agent.parsing.read_yes_no` as neutral, so it decides nothing on its own while
"yes cancel it" and "no don't cancel" both still land where they should.

*A code that cannot be delivered ends the flow.* Parking someone at a prompt for a code
that was never sent is worse than telling them the truth.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agent.handlers.booking import description_for, search_for_slot, summary_for
from app.agent.parsing import CANCEL_IS_NEUTRAL, extract_email, read_yes_no
from app.agent.resolver import DatetimeResolver
from app.domain.business import CLINIC_ADDRESS, SLOT_MINUTES
from app.domain.otp import MAX_SENDS, OTP_TTL, OtpVerdict, issue, read_code
from app.domain.scheduling import BOOKING_HORIZON_DAYS, format_slot
from app.services.calendar_service import (
    Appointment,
    CalendarError,
    CalendarService,
    EventNotFound,
)
from app.services.email_service import EmailError, EmailService
from app.state.models import ChangeCandidate, ChangeMode, ConversationState

logger = logging.getLogger(__name__)

# Booking reaches 90 days ahead, so nothing this bot created can start later. The extra
# day covers the boundary, where "now + 90 days" lands mid-morning and the appointment it
# should match is that afternoon.
CHANGE_LOOKAHEAD = timedelta(days=BOOKING_HORIZON_DAYS + 1)

# More than this and the list stops being readable in a chat bubble. A patient with six
# upcoming appointments has a different problem.
MAX_CHANGE_CHOICES = 5

# Addresses that may be tried in one flow. A miss keeps the prompt open -- someone with
# two mailboxes should not have to start over -- but an open prompt is a free calendar
# query per message, and free is what makes address enumeration worth attempting.
# Restarting the flow costs a classification, so this turns free probing back into paid.
MAX_LOOKUPS = 5

_OTP_MINUTES = int(OTP_TTL.total_seconds() // 60)


@dataclass(frozen=True)
class FlowResult:
    """A reply, and whether producing it cost a model call.

    Every other flow continuation is free, and the orchestrator logs and tests that
    property. Rescheduling is the exception: resolving "Friday at 3pm" needs Layer 2, so
    the cost has to be reported from the one place that knows it was paid.
    """

    text: str
    used_llm: bool = False


# ------------------------------------------------------------------------ messages

ASK_FOR_EMAIL = (
    "I can help with that. Which email address was the appointment booked with? "
    "I'll send a code there to check it's you."
)
EMAIL_NOT_VALID = (
    "That doesn't look like a valid email address. Could you type it again? "
    "Say \"stop\" if you'd rather leave it."
)
LOOKUP_TROUBLE = (
    "I couldn't reach the appointment calendar just then. Could you try again in "
    "a moment?"
)
TOO_MANY_LOOKUPS = (
    "I haven't been able to find an appointment under any of those addresses, so "
    "I've stopped there. If you're sure you have one booked, please call the clinic "
    "and the team will sort it out."
)
CHOICE_UNCLEAR = "Sorry, I didn't catch which one. Reply with its number."
CHANGE_INTENT_UNCLEAR = (
    "Sorry, I'm not sure which you'd like. Reply \"reschedule\" to move it to another "
    "time, or \"cancel\" to call it off. Say \"stop\" to leave it as it is."
)
CONFIRM_UNCLEAR = (
    "Sorry, I need a clear yes or no before I cancel anything. Should I go ahead? "
    "Say \"stop\" if you'd rather leave it."
)
RESCHEDULE_CONFIRM_UNCLEAR = (
    "Sorry, I need a clear yes or no before I move anything. Shall I go ahead? "
    "Say \"stop\" if you'd rather leave it."
)
ASK_FOR_NEW_TIME = (
    "What day and time would suit you instead? For example, \"Thursday at 3pm\"."
)
CANCELLATION_DECLINED = (
    "No problem - I haven't cancelled anything, and your appointment is still booked."
)
RESCHEDULE_DECLINED = (
    "No problem, nothing has changed. What day and time would you prefer instead?"
)
CODE_EMAIL_FAILED = (
    "I couldn't send the confirmation code just now, so I've stopped there - your "
    "appointment is unchanged. Please try again shortly, or call the clinic."
)
CODE_NOT_UNDERSTOOD = (
    "I'm looking for the 6-digit code from that email. Could you send just the digits? "
    "Reply \"resend\" if it hasn't arrived."
)
CODE_EXPIRED = (
    "That code has expired, so I've stopped there - your appointment is unchanged. "
    "Tell me again what you'd like to do and I'll send a fresh one."
)
TOO_MANY_ATTEMPTS = (
    "That's three incorrect codes, so I've stopped there for safety - your appointment "
    "is unchanged. Start again whenever you're ready and I'll send a new code."
)
TOO_MANY_CODES = (
    "I've already sent that address several codes. I've stopped there - your "
    "appointment is unchanged. Please start again in a few minutes."
)
ALREADY_GONE = (
    "That appointment is no longer on our calendar - it looks like it was already "
    "cancelled. Nothing more to do."
)
CANCEL_TROUBLE_AFTER_CODE = (
    "Your code was right, but I couldn't reach the calendar to cancel it just then. "
    "Send the code once more and I'll try again."
)
RESCHEDULE_TROUBLE_AFTER_CODE = (
    "Your code was right, but I couldn't reach the calendar just then, so nothing has "
    "changed. Send the code once more and I'll try again."
)

_PUNCTUATION = str.maketrans("", "", ".,!?'’")
_CHOICE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b")

# Checked before the code is read, so asking for a new one never spends an attempt.
_RESEND_PHRASES = frozenset(
    {
        "resend", "resend it", "resend code", "resend the code", "send again",
        "send it again", "send another", "send another code", "another code",
        "new code", "again", "didnt get it", "i didnt get it", "didnt receive it",
        "i didnt receive it", "not received", "no code", "nothing arrived",
        "nothing came", "havent got it", "i havent got it",
    }
)

# Words that pick between the two, once the bot has asked which. "cancel" decides here --
# unlike at a yes/no prompt, the question asked was literally "reschedule or cancel?".
_RESCHEDULE_WORDS = frozenset(
    {
        "reschedule", "rescheduled", "rescheduling", "move", "moved", "change",
        "changed", "shift", "postpone", "rebook", "switch", "different", "another",
        "later", "earlier", "new",
    }
)
_CANCEL_WORDS = frozenset(
    {"cancel", "cancelled", "cancelling", "delete", "remove", "drop", "scrap"}
)


# ------------------------------------------------------------------------- helpers

def _normalise(text: str) -> str:
    """Lowercase, drop punctuation and apostrophes, collapse whitespace."""
    return " ".join(text.lower().translate(_PUNCTUATION).split())


def _read_choice(text: str, count: int) -> int | None:
    """Zero-based index of the appointment picked, or None.

    Only short replies are considered, so an address retyped at this prompt cannot be
    mined for a stray digit and read as a choice.
    """
    normalised = _normalise(text)
    if not normalised or len(normalised.split()) > 3:
        return None
    match = _CHOICE.search(normalised)
    if match is None:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < count else None


def read_change_mode(text: str) -> ChangeMode | None:
    """Read "reschedule" or "cancel" out of a reply, or None when it says neither.

    A message carrying both -- "don't cancel it, just move it" -- is None rather than a
    guess. Picking one from a sentence that named the other is how a booking the user
    meant to keep gets destroyed.
    """
    words = set(_normalise(text).split())
    wants_move = bool(words & _RESCHEDULE_WORDS)
    wants_cancel = bool(words & _CANCEL_WORDS)
    if wants_move == wants_cancel:
        return None
    return "reschedule" if wants_move else "cancel"


def _describe(start: datetime, name: str | None) -> str:
    who = f"for {name} " if name else ""
    return f"{who}on {format_slot(start)}"


# ---------------------------------------------------------------------- flow entry

def start_change(state: ConversationState, mode: ChangeMode | None) -> str:
    """A fresh request to change an appointment. ``mode`` is None when it is not clear.

    The address is always asked for, even when this chat booked something earlier in the
    same conversation and ``patient_email`` is sitting right there. Reusing it would skip
    the only step that establishes who is asking, and reduce the code to a formality
    posted to an address the requester never had to know.
    """
    state.reset_flow()
    state.change_mode = mode
    state.stage = "awaiting_change_email"
    state.touch()
    return ASK_FOR_EMAIL


# ------------------------------------------------------------------ flow continuation

async def continue_change(
    state: ConversationState,
    text: str,
    resolver: DatetimeResolver,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
    tz: object,
) -> FlowResult:
    """Handle a reply inside an active change flow.

    Always returns a reply. Unlike :func:`~app.agent.handlers.booking.continue_booking`
    there is no reclassify path: see the module docstring.
    """
    if state.stage == "awaiting_change_email":
        return await _handle_lookup(state, text, calendar, now)
    if state.stage == "awaiting_change_choice":
        return _handle_choice(state, text)
    if state.stage == "awaiting_change_intent":
        return _handle_change_intent(state, text)
    if state.stage == "awaiting_cancel_confirmation":
        return await _handle_cancel_confirmation(state, text, calendar, email, now)
    if state.stage == "awaiting_reschedule_time":
        return await _handle_reschedule_time(state, text, resolver, calendar, now, tz)
    if state.stage == "awaiting_reschedule_confirmation":
        return await _handle_reschedule_confirmation(state, text, calendar, email, now)
    if state.stage == "awaiting_change_code":
        return await _handle_code(state, text, calendar, email, now)

    logger.warning("change.unknown_stage", extra={"stage": state.stage})
    state.reset_flow()
    return FlowResult(LOOKUP_TROUBLE)


# ------------------------------------------------------------- finding the appointment

async def _handle_lookup(
    state: ConversationState, text: str, calendar: CalendarService, now: datetime
) -> FlowResult:
    address = extract_email(text)
    if address is None:
        return FlowResult(EMAIL_NOT_VALID)

    try:
        found = await calendar.find_upcoming_by_email(
            address, now, now + CHANGE_LOOKAHEAD
        )
    except CalendarError:
        logger.exception("change.lookup_failed")
        state.reset_flow()
        return FlowResult(LOOKUP_TROUBLE)

    state.change_lookups += 1
    logger.info(
        "change.lookup",
        extra={"match_count": len(found), "attempt": state.change_lookups},
    )
    if not found:
        if state.change_lookups >= MAX_LOOKUPS:
            state.reset_flow()
            return FlowResult(TOO_MANY_LOOKUPS)
        # The prompt stays open, so the offer below is one the bot can actually keep.
        state.touch()
        return FlowResult(
            f"I couldn't find an upcoming appointment booked with {address}. "
            "If you booked with a different address, send that one and I'll look "
            "again - or say \"stop\" to leave it."
        )

    state.change_email = address
    if len(found) == 1:
        return FlowResult(_offer(state, found[0]))
    return FlowResult(_offer_choice(state, found))


def _offer_choice(state: ConversationState, found: list[Appointment]) -> str:
    shown = found[:MAX_CHANGE_CHOICES]
    state.change_candidates = [
        ChangeCandidate(
            event_id=item.event_id,
            start=item.start,
            patient_name=item.patient_name,
            ics_uid=item.ics_uid,
        )
        for item in shown
    ]
    state.stage = "awaiting_change_choice"
    state.touch()

    lines = "\n".join(
        f"  {number}. {format_slot(item.start)}"
        for number, item in enumerate(shown, start=1)
    )
    more = (
        f"\n\n(You have {len(found)} upcoming in total; these are the soonest "
        f"{len(shown)}.)"
        if len(found) > len(shown)
        else ""
    )
    return (
        f"I found {len(found)} upcoming appointments booked with {state.change_email}:"
        f"\n\n{lines}{more}\n\nWhich one did you mean? Reply with its number."
    )


def _handle_choice(state: ConversationState, text: str) -> FlowResult:
    index = _read_choice(text, len(state.change_candidates))
    if index is None:
        return FlowResult(CHOICE_UNCLEAR)
    chosen = state.change_candidates[index]
    return FlowResult(
        _offer(
            state,
            Appointment(
                event_id=chosen.event_id,
                start=chosen.start,
                summary="",
                patient_email=state.change_email or "",
                patient_name=chosen.patient_name,
                ics_uid=chosen.ics_uid,
            ),
        )
    )


def _offer(state: ConversationState, appointment: Appointment) -> str:
    """Pin down which appointment this is, then ask whatever the mode still needs.

    Where the mode is unknown this is the only place it can be asked without wasting a
    turn: the appointment is in view, so "is this the one?" and "what shall I do with
    it?" collapse into one question.
    """
    state.change_event_id = appointment.event_id
    state.change_start = appointment.start
    state.change_name = appointment.patient_name
    state.change_ics_uid = appointment.ics_uid or appointment.event_id
    state.change_ics_sequence = appointment.ics_sequence
    state.change_candidates = []
    state.touch()

    found = (
        f"I found an appointment {_describe(appointment.start, appointment.patient_name)}"
        f" at {CLINIC_ADDRESS}, booked with {state.change_email}."
    )

    if state.change_mode is None:
        state.stage = "awaiting_change_intent"
        return (
            f"{found}\n\nWould you like to move it to another time, or cancel it "
            "altogether?"
        )
    if state.change_mode == "cancel":
        state.stage = "awaiting_cancel_confirmation"
        return (
            f"{found}\n\nShall I cancel it? I'll email a 6-digit code to that address "
            "first, to check it's you."
        )

    state.stage = "awaiting_reschedule_time"
    return f"{found}\n\n{ASK_FOR_NEW_TIME}"


def _handle_change_intent(state: ConversationState, text: str) -> FlowResult:
    mode = read_change_mode(text)
    if mode is None:
        return FlowResult(CHANGE_INTENT_UNCLEAR)

    state.change_mode = mode
    state.touch()
    if mode == "cancel":
        state.stage = "awaiting_cancel_confirmation"
        when = format_slot(state.change_start) if state.change_start else "that time"
        return FlowResult(
            f"Right - cancelling your appointment on {when}. Shall I go ahead? "
            "I'll email a 6-digit code first, to check it's you."
        )
    state.stage = "awaiting_reschedule_time"
    return FlowResult(ASK_FOR_NEW_TIME)


# ------------------------------------------------------------------- cancelling

async def _handle_cancel_confirmation(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> FlowResult:
    answer = read_yes_no(text, neutral=CANCEL_IS_NEUTRAL)
    if answer is None:
        return FlowResult(CONFIRM_UNCLEAR)
    if not answer:
        state.reset_flow()
        return FlowResult(CANCELLATION_DECLINED)
    return await _authorise(state, calendar, email, now)


# ------------------------------------------------------------------ rescheduling

async def _handle_reschedule_time(
    state: ConversationState,
    text: str,
    resolver: DatetimeResolver,
    calendar: CalendarService,
    now: datetime,
    tz: object,
) -> FlowResult:
    """Turn the new time into a genuinely free slot, reusing booking's rules wholesale.

    The one model call in either flow. It stays at this stage on every failure, so a
    refused time is answered and re-asked rather than dropping a half-finished change.
    """
    search = await search_for_slot(text, resolver, calendar, now, tz)  # type: ignore[arg-type]

    # Their own appointment is on the calendar, so asking to move to the time they
    # already hold would otherwise be answered with the slot *after* it -- technically
    # true, and baffling.
    if search.requested is not None and search.requested == state.change_start:
        return FlowResult(
            f"That's already when your appointment is - "
            f"{format_slot(state.change_start)}. Did you have a different day or time "
            "in mind?",
            used_llm=True,
        )

    if search.slot is None:
        state.touch()
        return FlowResult(search.refusal or ASK_FOR_NEW_TIME, used_llm=True)

    state.reschedule_start = search.slot
    state.stage = "awaiting_reschedule_confirmation"
    state.touch()

    when = format_slot(search.slot)
    was = format_slot(state.change_start) if state.change_start else "your appointment"
    lead = (
        f"The nearest free appointment is {when} - that's the first opening at or after "
        "the time you asked for."
        if search.moved
        else f"{when} is free."
    )
    return FlowResult(
        f"{lead} It runs {SLOT_MINUTES} minutes.\n\nShall I move your appointment from "
        f"{was} to then? I'll email a 6-digit code first, to check it's you.",
        used_llm=True,
    )


async def _handle_reschedule_confirmation(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> FlowResult:
    answer = read_yes_no(text, neutral=CANCEL_IS_NEUTRAL)
    if answer is None:
        return FlowResult(RESCHEDULE_CONFIRM_UNCLEAR)
    if not answer:
        # Back to asking for a time rather than out of the flow: they still want to
        # move it, they just do not want *this* slot.
        state.reschedule_start = None
        state.stage = "awaiting_reschedule_time"
        state.touch()
        return FlowResult(RESCHEDULE_DECLINED)
    return await _authorise(state, calendar, email, now)


# ------------------------------------------------------------------- verification

async def _authorise(
    state: ConversationState,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> FlowResult:
    """Go straight to it if the code has already been accepted, else ask for one."""
    if state.change_verified:
        return await _execute(state, calendar, email)
    return await _send_code(state, email, now)


async def _send_code(
    state: ConversationState, email: EmailService, now: datetime
) -> FlowResult:
    """Mint a code, email it, and park the user at the code prompt.

    The plaintext lives in one local variable here and nowhere else: state keeps only
    the salted hash, and no branch below logs or echoes it.
    """
    if state.change_start is None or not state.change_email:  # defensive
        logger.warning("change.code_without_appointment")
        state.reset_flow()
        return FlowResult(LOOKUP_TROUBLE)

    if state.change_code_sends >= MAX_SENDS:
        logger.info("change.send_budget_spent")
        state.reset_flow()
        return FlowResult(TOO_MANY_CODES)

    challenge, code = issue(now)
    try:
        await email.send_cancellation_code(
            to_email=state.change_email,
            code=code,
            appointment_start=state.change_start,
            patient_name=state.change_name,
        )
    except EmailError:
        # No code reached the patient, so there is nothing for them to type. Ending the
        # flow is kinder than a prompt that can never be satisfied.
        logger.exception("change.code_email_failed")
        state.reset_flow()
        return FlowResult(CODE_EMAIL_FAILED)

    state.change_otp = challenge
    state.change_code_sends += 1
    state.stage = "awaiting_change_code"
    state.touch()
    logger.info(
        "change.code_sent",
        extra={"send_number": state.change_code_sends, "mode": state.change_mode},
    )

    if state.change_mode == "reschedule" and state.reschedule_start is not None:
        what = (
            f"move your appointment from {format_slot(state.change_start)} to "
            f"{format_slot(state.reschedule_start)}"
        )
    else:
        what = f"cancel your appointment on {format_slot(state.change_start)}"
    return FlowResult(
        f"I've emailed a 6-digit code to {state.change_email}. Reply with it here and "
        f"I'll {what}. It expires in {_OTP_MINUTES} minutes."
    )


async def _handle_code(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> FlowResult:
    if _normalise(text) in _RESEND_PHRASES:
        return await _send_code(state, email, now)

    challenge = state.change_otp
    if challenge is None or state.change_event_id is None:  # defensive
        logger.warning("change.code_without_challenge")
        state.reset_flow()
        return FlowResult(LOOKUP_TROUBLE)

    attempt = read_code(text)
    if attempt is None:
        # Not a code at all -- a question, a typo, a stray word. Spending one of three
        # attempts on something that was never a guess would be punishing noise.
        return FlowResult(CODE_NOT_UNDERSTOOD)

    verdict = challenge.verify(attempt, now)
    logger.info(
        "change.code_checked",
        extra={"verdict": verdict.value, "attempts_left": challenge.remaining_attempts},
    )

    if verdict is OtpVerdict.EXPIRED:
        state.reset_flow()
        return FlowResult(CODE_EXPIRED)
    if verdict is OtpVerdict.EXHAUSTED:
        state.reset_flow()
        return FlowResult(TOO_MANY_ATTEMPTS)
    if verdict is OtpVerdict.WRONG:
        left = challenge.remaining_attempts
        state.touch()
        return FlowResult(
            f"That code isn't right. You have {left} "
            f"{'attempt' if left == 1 else 'attempts'} left, or reply \"resend\" for a "
            "new one."
        )

    state.change_verified = True
    state.touch()
    return await _execute(state, calendar, email)


# ---------------------------------------------------------------------- doing it

async def _execute(
    state: ConversationState, calendar: CalendarService, email: EmailService
) -> FlowResult:
    if state.change_mode == "reschedule":
        return await _do_reschedule(state, calendar, email)
    return await _do_cancel(state, calendar, email)


async def _do_cancel(
    state: ConversationState, calendar: CalendarService, email: EmailService
) -> FlowResult:
    """The code checked out. Delete the event, then report what happened."""
    event_id = state.change_event_id
    start = state.change_start
    if event_id is None or start is None:  # defensive
        state.reset_flow()
        return FlowResult(LOOKUP_TROUBLE)

    try:
        await calendar.cancel_event(event_id)
    except EventNotFound:
        # Somebody got there first, or the clinic removed it by hand. The patient's goal
        # is met either way, so this is news rather than a failure.
        logger.info("change.already_gone", extra={"event_id": event_id})
        state.reset_flow()
        return FlowResult(ALREADY_GONE)
    except CalendarError:
        # Verified but not yet cancelled. Stay at the code prompt so retrying costs one
        # message: a correct code spends no attempt, so resending it is free.
        logger.exception("change.delete_failed", extra={"event_id": event_id})
        return FlowResult(CANCEL_TROUBLE_AFTER_CODE)

    # The appointment is gone. As with booking, the mail is a separate promise and a
    # broken SMTP server must not be reported as a failed cancellation.
    email_sent = False
    if state.change_email:
        try:
            await email.send_cancellation_confirmation(
                to_email=state.change_email,
                appointment_start=start,
                patient_name=state.change_name,
                ics_uid=state.change_ics_uid,
                ics_sequence=state.change_ics_sequence + 1,
            )
            email_sent = True
        except EmailError:
            logger.exception(
                "change.cancellation_email_failed", extra={"event_id": event_id}
            )

    logger.info(
        "change.cancelled", extra={"event_id": event_id, "email_sent": email_sent}
    )
    state.reset_flow()

    cancelled = (
        f"Done - your appointment on {format_slot(start)} is cancelled and the slot "
        "is free again."
    )
    if email_sent:
        return FlowResult(f"{cancelled} I've emailed you a confirmation.")
    return FlowResult(
        f"{cancelled} I couldn't send the confirmation email just now, but the "
        "cancellation itself went through."
    )


async def _do_reschedule(
    state: ConversationState, calendar: CalendarService, email: EmailService
) -> FlowResult:
    """Book the new time first, then release the old one.

    The order is the safety property. Cancelling first and then failing to book leaves a
    patient with no appointment and no warning; booking first and then failing to cancel
    leaves them with two, which is visible, recoverable, and said out loud below.
    """
    old_id, old_start = state.change_event_id, state.change_start
    new_start = state.reschedule_start
    if old_id is None or old_start is None or new_start is None:  # defensive
        state.reset_flow()
        return FlowResult(LOOKUP_TROUBLE)

    try:
        # Re-checked for the same reason booking re-checks: the slot could have gone
        # while they were fetching the code out of their inbox.
        if not await calendar.is_free(new_start):
            logger.info("change.new_slot_taken")
            state.reschedule_start = None
            state.stage = "awaiting_reschedule_time"
            state.touch()
            return FlowResult(
                f"Sorry - {format_slot(new_start)} was taken while we were talking, so "
                f"nothing has changed. Your appointment is still {format_slot(old_start)}"
                ". What other time would suit you?"
            )
        new_id = await calendar.create_event(
            start=new_start,
            summary=summary_for(state.change_name),
            description=description_for(
                state.change_name, f"Rescheduled from {format_slot(old_start)}."
            ),
            attendee_email=state.change_email,
            patient_name=state.change_name,
            # Carried so the patient's own calendar moves the entry it already has
            # instead of filing a second one beside it.
            ics_uid=state.change_ics_uid,
            ics_sequence=state.change_ics_sequence + 1,
        )
    except CalendarError:
        # Nothing has changed: the original appointment is untouched. Stay at the code
        # prompt so one message retries the whole thing.
        logger.exception("change.reschedule_create_failed")
        return FlowResult(RESCHEDULE_TROUBLE_AFTER_CODE)

    # The new appointment exists. From here nothing may throw, or the patient is told a
    # move failed that has half happened.
    released = True
    try:
        await calendar.cancel_event(old_id)
    except EventNotFound:
        logger.info("change.old_already_gone", extra={"event_id": old_id})
    except CalendarError:
        released = False
        logger.exception("change.release_failed", extra={"event_id": old_id})

    email_sent = False
    if state.change_email:
        try:
            await email.send_reschedule_confirmation(
                to_email=state.change_email,
                previous_start=old_start,
                appointment_start=new_start,
                patient_name=state.change_name,
                ics_uid=state.change_ics_uid,
                ics_sequence=state.change_ics_sequence + 1,
            )
            email_sent = True
        except EmailError:
            logger.exception("change.reschedule_email_failed", extra={"event_id": new_id})

    logger.info(
        "change.rescheduled",
        extra={
            "event_id": new_id,
            "previous_event_id": old_id,
            "released": released,
            "email_sent": email_sent,
        },
    )
    state.reset_flow()

    if not released:
        # Two appointments now exist. Saying so is the only honest option: the patient
        # is the one who will be standing in reception at the wrong time otherwise.
        return FlowResult(
            f"Your appointment is booked for {format_slot(new_start)}. I couldn't "
            f"release the earlier one on {format_slot(old_start)} though, so it may "
            "still be on our calendar - please call the clinic so they can clear it."
        )

    moved = (
        f"Done - your appointment has moved from {format_slot(old_start)} to "
        f"{format_slot(new_start)}, at {CLINIC_ADDRESS}. The earlier time is free again."
    )
    if email_sent:
        return FlowResult(f"{moved} I've emailed you a confirmation.")
    return FlowResult(
        f"{moved} I couldn't send the confirmation email just now, but the change "
        "itself went through."
    )
