"""Booking: resolve a time, check the calendar, confirm, create the appointment.

The conversation is a small state machine, and every transition is deterministic --
no model call is spent on "yes". The stages are the ones fixed in Phase 1:

    idle
      -> propose a real free slot            awaiting_slot_confirmation
      -> collect the patient's address       awaiting_email
      -> read the whole thing back           awaiting_final_confirmation
      -> create the event, return to         idle

Two details worth their weight:

*Availability is re-checked before creating.* A slot that was free when proposed can be
taken while the user types their email. Booking on the earlier answer is how two
patients end up in one slot.

*An unrecognised reply mid-flow is handed back to the classifier* rather than answered
with "please say yes or no". A user who replies "actually can we do 4pm?" is changing
the request, not failing to answer it, and the router already knows how to read that.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email

from app.agent.resolver import DatetimeResolver, ResolutionError
from app.domain.business import CLINIC_ADDRESS, CLINIC_NAME, SLOT_MINUTES
from app.domain.scheduling import (
    BOOKING_HORIZON_DAYS,
    Verdict,
    evaluate_request,
    format_day,
    format_slot,
    next_open_day,
    opening_hours_sentence,
)
from app.services.calendar_service import CalendarError, CalendarService
from app.state.models import ConversationState

logger = logging.getLogger(__name__)

# Returned when the message is not an answer to the question the bot asked, so the
# orchestrator should clear the flow and classify it afresh.
RECLASSIFY = None

ASK_FOR_TIME = (
    "Happy to help you book an appointment. What day and time suit you? "
    "For example, \"Monday at 2pm\"."
)
TROUBLE_RESOLVING = (
    "I'm having trouble working out that time. Could you give me a day and time, "
    "like \"Tuesday at 3pm\"?"
)
CALENDAR_TROUBLE = (
    "I couldn't reach the appointment calendar just then. Could you try again in "
    "a moment?"
)
ASK_FOR_EMAIL = (
    "Lovely. What email address should I send the confirmation to?"
)
EMAIL_NOT_VALID = (
    "That doesn't look like a valid email address. Could you type it again?"
)
BOOKING_CANCELLED = (
    "No problem, I haven't booked anything. Just tell me another day and time "
    "whenever you're ready."
)

_AFFIRMATIVE_WORDS = frozenset(
    {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
     "book", "please", "perfect", "great", "correct", "right", "good", "works",
     "fine", "ahead"}
)
_NEGATIVE_WORDS = frozenset(
    {"no", "n", "nope", "nah", "cancel", "dont", "stop", "never", "not"}
)
# Words that carry no decision but commonly pad one: "yeah go ahead", "no thanks".
_FILLER_WORDS = frozenset(
    {"thanks", "thank", "you", "that", "thats", "sounds", "go", "it", "then",
     "lets", "do", "and", "me", "sound", "is", "one", "really", "all"}
)
_KNOWN_WORDS = _AFFIRMATIVE_WORDS | _NEGATIVE_WORDS | _FILLER_WORDS

# Longer than this and the message is making a request, not answering yes or no.
_MAX_SENTIMENT_WORDS = 5

# Apostrophes are dropped before matching so contractions normalise onto the word
# lists: "that's" -> thats, "don't" -> dont, "let's" -> lets.
_APOSTROPHES = str.maketrans("", "", "'’")
_WORD = re.compile(r"[a-z]+")
_EMAIL_CANDIDATE = re.compile(r"[^\s<>,;]+@[^\s<>,;]+")


# --------------------------------------------------------------------- helpers

def _sentiment(text: str) -> bool | None:
    """True for yes, False for no, None when the reply answers neither.

    Every word must be recognised before a verdict is returned. That is what keeps
    "ok Tuesday" out of the yes bucket: it opens with an affirmative but carries a
    day the bot did not propose, so it belongs to the router, not to this question.
    "yeah go ahead" is entirely known words, so it is a yes.

    A negative anywhere wins, so "no thanks" cannot be read as thanks.
    """
    words = _WORD.findall(text.lower().translate(_APOSTROPHES))
    if not words or len(words) > _MAX_SENTIMENT_WORDS:
        return None
    if not set(words) <= _KNOWN_WORDS:
        return None
    if set(words) & _NEGATIVE_WORDS:
        return False
    if set(words) & _AFFIRMATIVE_WORDS:
        return True
    return None


def _extract_email(text: str) -> str | None:
    """Pull a valid address out of a message, or None.

    Validated with email-validator rather than a regex: the regex only finds the
    candidate. A wrong address means the Phase 4 confirmation silently never arrives.
    """
    for candidate in _EMAIL_CANDIDATE.findall(text):
        try:
            return validate_email(candidate, check_deliverability=False).normalized
        except EmailNotValidError:
            continue
    return None


def _summary_for(state: ConversationState) -> str:
    return f"Appointment - {state.patient_name or 'patient'}"


def _description_for(state: ConversationState) -> str:
    parts = [f"Booked via the {CLINIC_NAME} Telegram assistant."]
    if state.patient_name:
        parts.append(f"Patient: {state.patient_name}")
    return "\n".join(parts)


# ------------------------------------------------------------------ flow entry

async def start_booking(
    state: ConversationState,
    raw_datetime_text: str | None,
    resolver: DatetimeResolver,
    calendar: CalendarService,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """A fresh booking request: resolve, validate, then propose a genuinely free slot."""
    state.raw_datetime_text = raw_datetime_text
    state.touch()

    if not raw_datetime_text:
        return ASK_FOR_TIME

    try:
        resolution = await resolver.resolve(raw_datetime_text, now)
    except ResolutionError:
        return TROUBLE_RESOLVING

    if not resolution.has_day or not resolution.is_confident:
        logger.info(
            "booking.unresolved",
            extra={
                "has_day": resolution.has_day,
                "confidence": round(resolution.confidence, 2),
            },
        )
        return (
            f"I'm not sure which date \"{raw_datetime_text}\" means. Could you give me "
            "a specific day and time, like \"Tuesday at 3pm\"?"
        )

    decision = evaluate_request(resolution.day, resolution.clock, now, tz)
    logger.info(
        "booking.evaluated",
        extra={"verdict": decision.verdict.value, "adjusted": decision.was_adjusted},
    )
    if not decision.ok or decision.slot is None:
        return _rejection_reply(decision.verdict, resolution.day, decision.adjusted_from)

    state.requested_start = decision.slot

    # The rules say this time is legal. Only now is it worth asking Google whether
    # it is actually free.
    try:
        available = await calendar.find_nearest_available(decision.slot)
    except CalendarError:
        logger.exception("booking.availability_failed")
        return CALENDAR_TROUBLE

    if available is None:
        # Nothing left that day, and the rule forbids rolling to the next one.
        day_text = format_day(decision.slot.date())
        following = next_open_day(decision.slot.date())
        suggestion = (
            f" The next day we're open is {format_day(following)}." if following else ""
        )
        return (
            f"I'm afraid we're fully booked for the rest of {day_text}.{suggestion} "
            "Would another day work?"
        )

    state.proposed_start = available
    state.stage = "awaiting_slot_confirmation"
    state.touch()

    moved = available != decision.slot or decision.was_adjusted
    when = format_slot(available)
    if moved:
        return (
            f"The nearest free appointment is {when} - that's the first opening at or "
            f"after the time you asked for. It runs {SLOT_MINUTES} minutes. "
            "Shall I book it?"
        )
    return (
        f"{when} is free. It runs {SLOT_MINUTES} minutes. Shall I book it?"
    )


# -------------------------------------------------------------- flow continuation

async def continue_booking(
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    now: datetime,
    tz: ZoneInfo,
) -> str | None:
    """Handle a reply inside an active flow.

    Returns the reply text, or ``RECLASSIFY`` (None) when the message does not answer
    the question that was asked, so the caller can route it as a fresh request.
    """
    if state.stage == "awaiting_slot_confirmation":
        return await _handle_slot_confirmation(state, text)
    if state.stage == "awaiting_email":
        return _handle_email(state, text)
    if state.stage == "awaiting_final_confirmation":
        return await _handle_final_confirmation(state, text, calendar)

    logger.warning("booking.unknown_stage", extra={"stage": state.stage})
    state.reset_flow()
    return RECLASSIFY


async def _handle_slot_confirmation(state: ConversationState, text: str) -> str | None:
    answer = _sentiment(text)
    if answer is None:
        # Not a yes or a no. Most likely a different time -- let the router read it.
        state.reset_flow()
        return RECLASSIFY
    if not answer:
        state.reset_flow()
        return BOOKING_CANCELLED

    state.stage = "awaiting_email"
    state.touch()
    return ASK_FOR_EMAIL


def _handle_email(state: ConversationState, text: str) -> str | None:
    email = _extract_email(text)
    if email is None:
        # An address is a specific thing to be asked for, so a reply without one is
        # more likely a typo than a change of subject: ask again rather than reroute.
        return EMAIL_NOT_VALID

    state.patient_email = email
    state.stage = "awaiting_final_confirmation"
    state.touch()

    when = format_slot(state.proposed_start) if state.proposed_start else "the slot"
    return (
        f"Thanks. To confirm: {when} at {CLINIC_ADDRESS}, and I'll send the "
        "confirmation to the address you gave me. Shall I go ahead and book it?"
    )


async def _handle_final_confirmation(
    state: ConversationState, text: str, calendar: CalendarService
) -> str | None:
    answer = _sentiment(text)
    if answer is None:
        state.reset_flow()
        return RECLASSIFY
    if not answer:
        state.reset_flow()
        return BOOKING_CANCELLED

    slot = state.proposed_start
    if slot is None:  # defensive: the flow cannot reach here without one
        logger.warning("booking.confirmation_without_slot")
        state.reset_flow()
        return ASK_FOR_TIME

    # Re-check: the slot may have been taken while the user typed their email.
    try:
        if not await calendar.is_free(slot):
            logger.info("booking.slot_taken_before_confirm")
            state.reset_flow()
            return (
                f"Sorry - {format_slot(slot)} was taken while we were talking. "
                "Would you like to pick another time?"
            )
        event_id = await calendar.create_event(
            start=slot,
            summary=_summary_for(state),
            description=_description_for(state),
            attendee_email=state.patient_email,
        )
    except CalendarError:
        logger.exception("booking.create_failed")
        return CALENDAR_TROUBLE

    logger.info("booking.confirmed", extra={"event_id": event_id})
    state.reset_flow()
    state.touch()
    return (
        f"Booked. Your appointment is {format_slot(slot)} at {CLINIC_ADDRESS}. "
        "A confirmation email will follow shortly."
    )


# ------------------------------------------------------------------- rejections

def _rejection_reply(
    verdict: Verdict, day: date | None, asked_for: datetime | None
) -> str:
    """Explain why a time cannot work, and give the user something to act on."""
    if verdict is Verdict.NEEDS_DAY:
        return ASK_FOR_TIME

    if verdict is Verdict.IN_PAST:
        return (
            "That date has already passed. What day and time would suit you from "
            "today onwards?"
        )

    if verdict is Verdict.CLOSED_DAY:
        following = next_open_day(day) if day is not None else None
        suggestion = (
            f" The next day we're open is {format_day(following)}." if following else ""
        )
        # Suggest, never rebook: NEAREST_AVAILABLE_RULE forbids moving the request
        # onto another day without the user asking.
        return f"We're closed that day. {opening_hours_sentence()}{suggestion}"

    if verdict is Verdict.TOO_LATE:
        when = f" on {format_day(asked_for.date())}" if asked_for else ""
        return (
            f"We don't have any appointment slots left that late{when}. "
            f"{opening_hours_sentence()} Would another day work?"
        )

    if verdict is Verdict.TOO_FAR:
        return (
            f"I can only book up to {BOOKING_HORIZON_DAYS} days ahead. "
            "Could you pick a nearer date?"
        )

    return TROUBLE_RESOLVING
