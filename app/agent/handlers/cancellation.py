"""Cancellation: find the appointment, prove who is asking, then delete it.

Booking is forgiving -- a wrong slot is a message away from being fixed. Cancelling is
not: the appointment is gone, the clinic has resold the time, and nobody finds out until
someone turns up. So the flow is built around one question booking never has to ask:
*is the person typing entitled to do this?*

Telegram answers a different question. It tells us this chat is the same chat as
yesterday; it says nothing about which patient that is. The link between a chat and an
appointment is the email address the booking was made with -- and an address a stranger
can type is a claim, not proof. A one-time code sent to that address is what settles it:
whoever replies with it controls the inbox the appointment was booked from.

    idle
      -> ask which address it was booked with       awaiting_cancel_email
      -> (several matches) which one?               awaiting_cancel_choice
      -> read the appointment back                  awaiting_cancel_confirmation
      -> email a code, ask for it                   awaiting_cancel_code
      -> verify, delete the event, return to        idle

Four details worth their weight:

*An unrecognised reply re-asks; it never reroutes.* Booking hands a puzzling reply back
to the classifier, because "actually can we do 4pm?" is a changed request. Nothing at a
cancellation prompt reinterprets that way, and silently dropping the flow at the point
where a destructive action is being confirmed is the wrong instinct. The escape hatch in
the orchestrator is how a user leaves.

*"cancel" cannot be read as "no" here.* In a booking, "cancel it" means don't book. In
this flow the same two words mean do the thing I asked for. The word is passed to
:func:`~app.agent.parsing.read_yes_no` as neutral, so it decides nothing on its own while
"yes cancel it" and "no don't cancel" both still land where they should.

*A code that cannot be delivered ends the flow.* Parking someone at a prompt for a code
that was never sent is worse than telling them the truth.

*Nothing is revealed after a failed code.* The appointment is described before the code
is sent -- the requester supplied the address, so that discloses nothing they did not
already assert -- and after that, a wrong code gets a wrong-code reply and nothing else.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.agent.parsing import CANCEL_IS_NEUTRAL, extract_email, read_yes_no
from app.domain.business import CLINIC_ADDRESS
from app.domain.otp import MAX_SENDS, OTP_TTL, OtpVerdict, issue, read_code
from app.domain.scheduling import BOOKING_HORIZON_DAYS, format_slot
from app.services.calendar_service import (
    Appointment,
    CalendarError,
    CalendarService,
    EventNotFound,
)
from app.services.email_service import EmailError, EmailService
from app.state.models import CancelCandidate, ConversationState

logger = logging.getLogger(__name__)

# Booking reaches 90 days ahead, so nothing this bot created can start later. The extra
# day covers the boundary, where "now + 90 days" lands mid-morning and the appointment
# it should match is that afternoon.
CANCEL_LOOKAHEAD = timedelta(days=BOOKING_HORIZON_DAYS + 1)

# More than this and the list stops being readable in a chat bubble. A patient with six
# upcoming appointments has a different problem.
MAX_CANCEL_CHOICES = 5

# Addresses that may be tried in one flow. A miss keeps the prompt open -- someone with
# two mailboxes should not have to start over -- but an open prompt is a free calendar
# query per message, and free is what makes address enumeration worth attempting.
# Restarting the flow costs a classification, so this turns free probing back into paid.
MAX_LOOKUPS = 5

_OTP_MINUTES = int(OTP_TTL.total_seconds() // 60)

ASK_FOR_CANCEL_EMAIL = (
    "I can help with that. Which email address was the appointment booked with? "
    "I'll send a code there to check it's you."
)
CANCEL_EMAIL_NOT_VALID = (
    "That doesn't look like a valid email address. Could you type it again? "
    "Say \"stop\" if you'd rather leave it."
)
LOOKUP_TROUBLE = (
    "I couldn't reach the appointment calendar just then. Could you try again in "
    "a moment?"
)
CONFIRM_UNCLEAR = (
    "Sorry, I need a clear yes or no before I cancel anything. Should I go ahead? "
    "Say \"stop\" if you'd rather leave it."
)
CHOICE_UNCLEAR = (
    "Sorry, I didn't catch which one. Reply with its number."
)
CANCELLATION_DECLINED = (
    "No problem - I haven't cancelled anything, and your appointment is still booked."
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
    "That code has expired, so I've stopped there - your appointment is still booked. "
    "Tell me you'd like to cancel again and I'll send a fresh one."
)
TOO_MANY_ATTEMPTS = (
    "That's three incorrect codes, so I've stopped there for safety - your appointment "
    "is still booked. Start again whenever you're ready and I'll send a new code."
)
TOO_MANY_CODES = (
    "I've already sent that address several codes. I've stopped there - your "
    "appointment is still booked. Please start again in a few minutes."
)
TOO_MANY_LOOKUPS = (
    "I haven't been able to find an appointment under any of those addresses, so "
    "I've stopped there. If you're sure you have one booked, please call the clinic "
    "and the team will sort it out."
)
ALREADY_GONE = (
    "That appointment is no longer on our calendar - it looks like it was already "
    "cancelled. Nothing more to do."
)
CANCEL_TROUBLE_AFTER_CODE = (
    "Your code was right, but I couldn't reach the calendar to cancel it just then. "
    "Send the code once more and I'll try again."
)

_APOSTROPHES = str.maketrans("", "", ".,!?'’")
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


# --------------------------------------------------------------------- helpers

def _normalise(text: str) -> str:
    """Lowercase, drop punctuation and apostrophes, collapse whitespace."""
    return " ".join(text.lower().translate(_APOSTROPHES).split())


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


def _describe(start: datetime, name: str | None) -> str:
    who = f"for {name} " if name else ""
    return f"{who}on {format_slot(start)}"


# ------------------------------------------------------------------ flow entry

def start_cancellation(state: ConversationState) -> str:
    """A fresh cancellation request: ask which address it was booked with.

    The address is always asked for, even when this chat booked something earlier in the
    same conversation and ``patient_email`` is sitting right there. Reusing it would skip
    the only step that establishes who is asking, and reduce the code to a formality
    posted to an address the requester never had to know.
    """
    state.reset_flow()
    state.stage = "awaiting_cancel_email"
    state.touch()
    return ASK_FOR_CANCEL_EMAIL


# -------------------------------------------------------------- flow continuation

async def continue_cancellation(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> str:
    """Handle a reply inside an active cancellation flow.

    Always returns a reply. Unlike :func:`~app.agent.handlers.booking.continue_booking`
    there is no reclassify path: see the module docstring.
    """
    if state.stage == "awaiting_cancel_email":
        return await _handle_lookup(state, text, calendar, now)
    if state.stage == "awaiting_cancel_choice":
        return _handle_choice(state, text)
    if state.stage == "awaiting_cancel_confirmation":
        return await _handle_confirmation(state, text, email, now)
    if state.stage == "awaiting_cancel_code":
        return await _handle_code(state, text, calendar, email, now)

    logger.warning("cancellation.unknown_stage", extra={"stage": state.stage})
    state.reset_flow()
    return LOOKUP_TROUBLE


async def _handle_lookup(
    state: ConversationState, text: str, calendar: CalendarService, now: datetime
) -> str:
    address = extract_email(text)
    if address is None:
        return CANCEL_EMAIL_NOT_VALID

    try:
        found = await calendar.find_upcoming_by_email(address, now, now + CANCEL_LOOKAHEAD)
    except CalendarError:
        logger.exception("cancellation.lookup_failed")
        state.reset_flow()
        return LOOKUP_TROUBLE

    state.cancel_lookups += 1
    logger.info(
        "cancellation.lookup",
        extra={"match_count": len(found), "attempt": state.cancel_lookups},
    )
    if not found:
        if state.cancel_lookups >= MAX_LOOKUPS:
            state.reset_flow()
            return TOO_MANY_LOOKUPS
        # The prompt stays open, so the offer below is one the bot can actually keep.
        state.touch()
        return (
            f"I couldn't find an upcoming appointment booked with {address}. "
            "If you booked with a different address, send that one and I'll look "
            "again - or say \"stop\" to leave it."
        )

    state.cancel_email = address
    if len(found) == 1:
        return _offer(state, found[0].event_id, found[0].start, found[0].patient_name)

    return _offer_choice(state, found)


def _offer_choice(state: ConversationState, found: list[Appointment]) -> str:
    shown = found[:MAX_CANCEL_CHOICES]
    state.cancel_candidates = [
        CancelCandidate(
            event_id=item.event_id, start=item.start, patient_name=item.patient_name
        )
        for item in shown
    ]
    state.stage = "awaiting_cancel_choice"
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
        f"I found {len(found)} upcoming appointments booked with {state.cancel_email}:"
        f"\n\n{lines}{more}\n\nWhich one should I cancel? Reply with its number."
    )


def _handle_choice(state: ConversationState, text: str) -> str:
    index = _read_choice(text, len(state.cancel_candidates))
    if index is None:
        return CHOICE_UNCLEAR
    chosen = state.cancel_candidates[index]
    return _offer(state, chosen.event_id, chosen.start, chosen.patient_name)


def _offer(
    state: ConversationState, event_id: str, start: datetime, name: str | None
) -> str:
    """Read one appointment back and ask whether it is the one to cancel."""
    state.cancel_event_id = event_id
    state.cancel_start = start
    state.cancel_name = name
    state.cancel_candidates = []
    state.stage = "awaiting_cancel_confirmation"
    state.touch()

    return (
        f"I found an appointment {_describe(start, name)} at {CLINIC_ADDRESS}, booked "
        f"with {state.cancel_email}.\n\nShall I cancel it? I'll email a 6-digit code "
        "to that address first, to check it's you."
    )


async def _handle_confirmation(
    state: ConversationState, text: str, email: EmailService, now: datetime
) -> str:
    answer = read_yes_no(text, neutral=CANCEL_IS_NEUTRAL)
    if answer is None:
        return CONFIRM_UNCLEAR
    if not answer:
        state.reset_flow()
        return CANCELLATION_DECLINED
    return await _send_code(state, email, now)


async def _send_code(
    state: ConversationState, email: EmailService, now: datetime
) -> str:
    """Mint a code, email it, and park the user at the code prompt.

    The plaintext lives in one local variable here and nowhere else: state keeps only
    the salted hash, and no branch below logs or echoes it.
    """
    if state.cancel_start is None or not state.cancel_email:  # defensive
        logger.warning("cancellation.code_without_appointment")
        state.reset_flow()
        return LOOKUP_TROUBLE

    if state.cancel_code_sends >= MAX_SENDS:
        logger.info("cancellation.send_budget_spent")
        state.reset_flow()
        return TOO_MANY_CODES

    challenge, code = issue(now)
    try:
        await email.send_cancellation_code(
            to_email=state.cancel_email,
            code=code,
            appointment_start=state.cancel_start,
            patient_name=state.cancel_name,
        )
    except EmailError:
        # No code reached the patient, so there is nothing for them to type. Ending the
        # flow is kinder than a prompt that can never be satisfied.
        logger.exception("cancellation.code_email_failed")
        state.reset_flow()
        return CODE_EMAIL_FAILED

    state.cancel_otp = challenge
    state.cancel_code_sends += 1
    state.stage = "awaiting_cancel_code"
    state.touch()
    logger.info("cancellation.code_sent", extra={"send_number": state.cancel_code_sends})

    return (
        f"I've emailed a 6-digit code to {state.cancel_email}. Reply with it here and "
        f"I'll cancel your appointment on {format_slot(state.cancel_start)}. "
        f"It expires in {_OTP_MINUTES} minutes."
    )


async def _handle_code(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
    now: datetime,
) -> str:
    if _normalise(text) in _RESEND_PHRASES:
        return await _send_code(state, email, now)

    challenge = state.cancel_otp
    if challenge is None or state.cancel_event_id is None:  # defensive
        logger.warning("cancellation.code_without_challenge")
        state.reset_flow()
        return LOOKUP_TROUBLE

    attempt = read_code(text)
    if attempt is None:
        # Not a code at all -- a question, a typo, a stray word. Spending one of three
        # attempts on something that was never a guess would be punishing noise.
        return CODE_NOT_UNDERSTOOD

    verdict = challenge.verify(attempt, now)
    logger.info(
        "cancellation.code_checked",
        extra={"verdict": verdict.value, "attempts_left": challenge.remaining_attempts},
    )

    if verdict is OtpVerdict.EXPIRED:
        state.reset_flow()
        return CODE_EXPIRED
    if verdict is OtpVerdict.EXHAUSTED:
        state.reset_flow()
        return TOO_MANY_ATTEMPTS
    if verdict is OtpVerdict.WRONG:
        left = challenge.remaining_attempts
        state.touch()
        return (
            f"That code isn't right. You have {left} "
            f"{'attempt' if left == 1 else 'attempts'} left, or reply \"resend\" for a "
            "new one."
        )

    return await _cancel_it(state, calendar, email)


async def _cancel_it(
    state: ConversationState, calendar: CalendarService, email: EmailService
) -> str:
    """The code checked out. Delete the event, then report what happened."""
    event_id = state.cancel_event_id
    start = state.cancel_start
    if event_id is None or start is None:  # defensive
        state.reset_flow()
        return LOOKUP_TROUBLE

    try:
        await calendar.cancel_event(event_id)
    except EventNotFound:
        # Somebody got there first, or the clinic removed it by hand. The patient's goal
        # is met either way, so this is news rather than a failure.
        logger.info("cancellation.already_gone", extra={"event_id": event_id})
        state.reset_flow()
        return ALREADY_GONE
    except CalendarError:
        # Verified but not yet cancelled. Stay at the code prompt so retrying costs one
        # message: a correct code spends no attempt, so resending it is free.
        logger.exception("cancellation.delete_failed", extra={"event_id": event_id})
        return CANCEL_TROUBLE_AFTER_CODE

    # The appointment is gone. As with booking, the mail is a separate promise and a
    # broken SMTP server must not be reported as a failed cancellation.
    email_sent = False
    if state.cancel_email:
        try:
            await email.send_cancellation_confirmation(
                to_email=state.cancel_email,
                appointment_start=start,
                patient_name=state.cancel_name,
                event_id=event_id,
            )
            email_sent = True
        except EmailError:
            logger.exception(
                "cancellation.confirmation_email_failed", extra={"event_id": event_id}
            )

    logger.info(
        "cancellation.confirmed", extra={"event_id": event_id, "email_sent": email_sent}
    )
    state.reset_flow()

    cancelled = (
        f"Done - your appointment on {format_slot(start)} is cancelled and the slot "
        "is free again."
    )
    if email_sent:
        return f"{cancelled} I've emailed you a confirmation."
    return (
        f"{cancelled} I couldn't send the confirmation email just now, but the "
        "cancellation itself went through."
    )
