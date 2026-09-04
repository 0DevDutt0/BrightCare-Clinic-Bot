"""Booking intent -- Phase 2: resolve the time, validate it, stop before the calendar.

The handler runs Layer 2 and then the deterministic scheduling rules, in that order:
the model says what the user meant, the rules say whether the clinic can honour it.
Neither can override the other, which is the point.

What is still missing is availability. Phase 3 takes the candidate slot this produces
and asks Google Calendar whether it is actually free, so replies here say what the
clinic *could* offer, never that a booking is confirmed.

``stage`` deliberately stays ``idle``, as in Phase 1. Advancing it would park the user
in a flow with nothing behind it. Phase 3 sets ``awaiting_slot_confirmation`` at the
point it has a real slot to confirm.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.agent.resolver import DatetimeResolver, ResolutionError
from app.domain.business import SLOT_MINUTES
from app.domain.scheduling import (
    BOOKING_HORIZON_DAYS,
    Verdict,
    evaluate_request,
    format_day,
    format_slot,
    next_open_day,
    opening_hours_sentence,
)
from app.state.models import ConversationState

logger = logging.getLogger(__name__)

NOT_WIRED_YET = "I can't confirm it against the calendar yet."

ASK_FOR_TIME = (
    "Happy to help you book an appointment. What day and time suit you? "
    "For example, \"Monday at 2pm\"."
)
TROUBLE_RESOLVING = (
    "I'm having trouble working out that time. Could you give me a day and time, "
    "like \"Tuesday at 3pm\"?"
)


def _ambiguous_reply(phrase: str) -> str:
    return (
        f"I'm not sure which date \"{phrase}\" means. Could you give me a specific "
        "day and time, like \"Tuesday at 3pm\"?"
    )


async def booking_reply(
    state: ConversationState,
    raw_datetime_text: str | None,
    resolver: DatetimeResolver,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """Resolve and validate a booking request, replying with what the clinic can do."""
    state.raw_datetime_text = raw_datetime_text
    state.touch()

    if not raw_datetime_text:
        return ASK_FOR_TIME

    try:
        resolution = await resolver.resolve(raw_datetime_text, now)
    except ResolutionError:
        # Already logged with the raw model output by the resolver.
        return TROUBLE_RESOLVING

    if not resolution.has_day or not resolution.is_confident:
        logger.info(
            "booking.unresolved",
            extra={
                "has_day": resolution.has_day,
                "confidence": round(resolution.confidence, 2),
            },
        )
        return _ambiguous_reply(raw_datetime_text)

    decision = evaluate_request(resolution.day, resolution.clock, now, tz)
    logger.info(
        "booking.evaluated",
        extra={"verdict": decision.verdict.value, "adjusted": decision.was_adjusted},
    )

    if decision.ok and decision.slot is not None:
        state.requested_start = decision.slot
        state.touch()
        return _bookable_reply(decision.slot, decision.was_adjusted, decision.adjusted_from)

    return _rejection_reply(decision.verdict, resolution.day, decision.adjusted_from)


def _bookable_reply(
    slot: datetime, was_adjusted: bool, asked_for: datetime | None
) -> str:
    """Confirm what was understood, making any shift in time visible."""
    when = format_slot(slot)
    if was_adjusted and asked_for is not None:
        # Say why the time moved: a silent shift is how a patient turns up an hour out.
        reason = (
            "that's the first slot at or after the time you asked for"
            if asked_for.date() == slot.date()
            else "that's the first slot we could offer"
        )
        return (
            f"The nearest appointment slot to that is {when} - {reason}. "
            f"Appointments run {SLOT_MINUTES} minutes. {NOT_WIRED_YET}"
        )
    return (
        f"Got it - {when}. Appointments run {SLOT_MINUTES} minutes. {NOT_WIRED_YET}"
    )


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
        suggestion = ""
        following = next_open_day(day) if day is not None else None
        if following is not None:
            # Suggest, never rebook: NEAREST_AVAILABLE_RULE forbids rolling a
            # request onto another day without the user asking.
            suggestion = f" The next day we're open is {format_day(following)}."
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


def continue_booking_flow(state: ConversationState) -> str:
    """Placeholder for an in-progress flow.

    Reached when ``state.stage`` is not ``idle``: the user is answering a question the
    bot asked, so the message must not be re-classified as a fresh intent. Phase 3 and
    Phase 4 fill in the per-stage behaviour.
    """
    return (
        "Thanks - I've noted that. The booking flow isn't wired up yet, "
        "so I can't take it further right now."
    )
