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
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.agent.parsing import extract_email, read_yes_no
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
from app.services.email_service import EmailError, EmailService
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

# --------------------------------------------------------------------- helpers

def summary_for(patient_name: str | None) -> str:
    """The event title. Shared with rescheduling, which recreates the same appointment."""
    return f"Appointment - {patient_name or 'patient'}"


def description_for(patient_name: str | None, note: str | None = None) -> str:
    """The event body. ``note`` records how it came to be, e.g. that it was moved."""
    parts = [f"Booked via the {CLINIC_NAME} Telegram assistant."]
    if patient_name:
        parts.append(f"Patient: {patient_name}")
    if note:
        parts.append(note)
    return "\n".join(parts)


# ------------------------------------------------------- turning a phrase into a slot

@dataclass(frozen=True)
class SlotSearch:
    """What a time phrase turned into: a free slot, or the reason there isn't one.

    Shared by booking and rescheduling. Both have to resolve a phrase, check it against
    the clinic's rules and then ask Google whether the time is actually free, and both
    have to explain a "no" in the same terms — a second copy of that sequence is a second
    place for the opening hours to be got wrong.

    ``refusal`` is a finished sentence, ready to send. ``requested`` is the grid-aligned
    time the user asked for, which is not always the one offered.
    """

    slot: datetime | None = None
    requested: datetime | None = None
    refusal: str | None = None
    moved: bool = False


async def search_for_slot(
    raw_datetime_text: str,
    resolver: DatetimeResolver,
    calendar: CalendarService,
    now: datetime,
    tz: ZoneInfo,
) -> SlotSearch:
    """Resolve a time phrase, validate it, and find the soonest free slot at or after it.

    Costs one Layer 2 model call, and one calendar request only if the rules already
    agree the time is legal — "is 2pm a bookable time?" needs no network.
    """
    try:
        resolution = await resolver.resolve(raw_datetime_text, now)
    except ResolutionError:
        return SlotSearch(refusal=TROUBLE_RESOLVING)

    if not resolution.has_day or not resolution.is_confident:
        logger.info(
            "booking.unresolved",
            extra={
                "has_day": resolution.has_day,
                "confidence": round(resolution.confidence, 2),
            },
        )
        return SlotSearch(
            refusal=(
                f"I'm not sure which date \"{raw_datetime_text}\" means. Could you give "
                "me a specific day and time, like \"Tuesday at 3pm\"?"
            )
        )

    decision = evaluate_request(resolution.day, resolution.clock, now, tz)
    logger.info(
        "booking.evaluated",
        extra={"verdict": decision.verdict.value, "adjusted": decision.was_adjusted},
    )
    if not decision.ok or decision.slot is None:
        return SlotSearch(
            refusal=rejection_reply(
                decision.verdict, resolution.day, decision.adjusted_from
            )
        )

    try:
        available = await calendar.find_nearest_available(decision.slot)
    except CalendarError:
        logger.exception("booking.availability_failed")
        return SlotSearch(requested=decision.slot, refusal=CALENDAR_TROUBLE)

    if available is None:
        # Nothing left that day, and the rule forbids rolling to the next one.
        day_text = format_day(decision.slot.date())
        following = next_open_day(decision.slot.date())
        suggestion = (
            f" The next day we're open is {format_day(following)}." if following else ""
        )
        return SlotSearch(
            requested=decision.slot,
            refusal=(
                f"I'm afraid we're fully booked for the rest of {day_text}.{suggestion} "
                "Would another day work?"
            ),
        )

    return SlotSearch(
        slot=available,
        requested=decision.slot,
        moved=available != decision.slot or decision.was_adjusted,
    )


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

    search = await search_for_slot(raw_datetime_text, resolver, calendar, now, tz)
    if search.requested is not None:
        state.requested_start = search.requested
    if search.slot is None:
        return search.refusal or TROUBLE_RESOLVING

    state.proposed_start = search.slot
    state.stage = "awaiting_slot_confirmation"
    state.touch()

    when = format_slot(search.slot)
    if search.moved:
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
    email: EmailService,
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
        return await _handle_final_confirmation(state, text, calendar, email)

    logger.warning("booking.unknown_stage", extra={"stage": state.stage})
    state.reset_flow()
    return RECLASSIFY


async def _handle_slot_confirmation(state: ConversationState, text: str) -> str | None:
    answer = read_yes_no(text)
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
    email = extract_email(text)
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
    state: ConversationState,
    text: str,
    calendar: CalendarService,
    email: EmailService,
) -> str | None:
    answer = read_yes_no(text)
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
            summary=summary_for(state.patient_name),
            description=description_for(state.patient_name),
            attendee_email=state.patient_email,
            patient_name=state.patient_name,
        )
    except CalendarError:
        logger.exception("booking.create_failed")
        return CALENDAR_TROUBLE

    # The appointment exists now. The email is a separate promise, and a broken SMTP
    # server must not undo a real booking -- so this is reported, never rolled back.
    email_sent = False
    if state.patient_email:
        try:
            await email.send_confirmation(
                to_email=state.patient_email,
                appointment_start=slot,
                patient_name=state.patient_name,
                # A fresh booking's UID is its event id. Rescheduling carries that UID
                # forward, so the patient's calendar later moves the entry this mail
                # created rather than filing a second one beside it.
                ics_uid=event_id,
            )
            email_sent = True
        except EmailError:
            logger.exception("booking.email_failed", extra={"event_id": event_id})

    logger.info(
        "booking.confirmed", extra={"event_id": event_id, "email_sent": email_sent}
    )
    state.reset_flow()
    state.touch()

    booked = f"Booked. Your appointment is {format_slot(slot)} at {CLINIC_ADDRESS}."
    if email_sent:
        return f"{booked} I've sent a confirmation to your email."
    # Say what actually happened: the appointment is real either way, and a patient
    # who is told to expect an email that never arrives will assume it failed.
    return (
        f"{booked} I couldn't send the confirmation email just now, but your "
        "appointment is booked and we'll see you then."
    )


# ------------------------------------------------------------------- rejections

def rejection_reply(
    verdict: Verdict, day: date | None, asked_for: datetime | None
) -> str:
    """Explain why a time cannot work, and give the user something to act on.

    Public because rescheduling refuses a time for exactly the same reasons, in exactly
    the same words. Two copies of the opening hours is one too many.
    """
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
