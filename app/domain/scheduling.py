"""Business-hours validation and slot alignment.

Pure, deterministic, and the only authority on whether a requested time is bookable.
The model resolves *what the user meant* (Layer 2); this module decides *whether the
clinic can honour it*. Keeping policy out of the model is the same choice made for FAQ
facts in Phase 1: a rule the model could reword is a rule that will eventually be
reworded wrongly.

Phase 3 layers calendar availability on top of the candidate this produces. The split
is deliberate -- "is 2pm a legal appointment time?" needs no network call, so it is
answered before one is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from app.domain.business import (
    CLOSE_TIME,
    OPEN_TIME,
    SLOT_DURATION,
    SLOT_MINUTES,
    business_window,
    is_business_day,
)

# How far ahead a booking may be made. Assumption: 90 days. Beyond this a clinic
# calendar is usually not yet published, and a typo ("2027") should be caught rather
# than silently accepted.
BOOKING_HORIZON_DAYS = 90

# Clock times the model maps vague parts of the day onto. Kept here rather than in the
# prompt so the mapping is testable and cannot drift per model response.
PART_OF_DAY_DEFAULTS = {
    "morning": time(9, 0),
    "afternoon": time(14, 0),
    "evening": time(17, 0),
    "noon": time(12, 0),
}


class Verdict(str, Enum):
    """Why a requested time can or cannot become an appointment."""

    OK = "ok"
    NEEDS_DAY = "needs_day"          # no resolvable date in the message
    IN_PAST = "in_past"              # the day itself has gone
    CLOSED_DAY = "closed_day"        # weekend
    TOO_LATE = "too_late"            # no slot left on that day
    TOO_FAR = "too_far"              # beyond the booking horizon


@dataclass(frozen=True)
class SlotDecision:
    """The outcome of validating one requested time.

    ``slot`` is the candidate appointment start: grid-aligned, inside opening hours,
    and on the requested day. It is what Phase 3 will check the calendar against. It
    is None for every verdict except OK.

    ``adjusted_from`` records the time the user actually asked for when the candidate
    had to move -- off the :00/:30 grid, before opening, or already past today. The
    reply quotes it so the shift is visible rather than silent.
    """

    verdict: Verdict
    slot: datetime | None = None
    adjusted_from: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK

    @property
    def was_adjusted(self) -> bool:
        return self.adjusted_from is not None and self.adjusted_from != self.slot


def _require_aware(moment: datetime, label: str) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{label} requires a timezone-aware datetime")


def align_to_slot(moment: datetime) -> datetime:
    """The earliest slot start at or after ``moment``.

    Appointments begin on the hour or half hour, so 14:15 becomes 14:30 and 14:00
    stays put. Rounding *up* is what the booking rule requires: the nearest available
    slot is at or after the requested time, never before it.
    """
    _require_aware(moment, "align_to_slot")
    floor = moment.replace(
        minute=(moment.minute // SLOT_MINUTES) * SLOT_MINUTES, second=0, microsecond=0
    )
    return floor if floor == moment else floor + SLOT_DURATION


def last_slot_start(day: date, tz: ZoneInfo) -> datetime | None:
    """The latest start that still finishes by closing time, or None when closed."""
    window = business_window(day, tz)
    if window is None:
        return None
    return window[1] - SLOT_DURATION


def evaluate_request(
    requested_day: date | None,
    requested_time: time | None,
    now: datetime,
    tz: ZoneInfo,
    horizon_days: int = BOOKING_HORIZON_DAYS,
) -> SlotDecision:
    """Decide whether a resolved date/time can become an appointment.

    ``requested_time`` of None means "any time that day", which the booking rule reads
    as "from opening onwards" -- the soonest slot at or after the requested time, where
    the requested time is the start of the day.
    """
    _require_aware(now, "evaluate_request")

    if requested_day is None:
        return SlotDecision(Verdict.NEEDS_DAY)

    today = now.astimezone(tz).date()
    if requested_day < today:
        return SlotDecision(Verdict.IN_PAST)
    if (requested_day - today).days > horizon_days:
        return SlotDecision(Verdict.TOO_FAR)
    if not is_business_day(requested_day):
        return SlotDecision(Verdict.CLOSED_DAY)

    window = business_window(requested_day, tz)
    assert window is not None  # is_business_day already established this
    opens, closes = window

    asked_for = datetime.combine(
        requested_day, requested_time or OPEN_TIME, tzinfo=tz
    )

    # The rule is "at or after the requested time", so a request that lands before
    # opening, or that has already gone by today, moves forward rather than failing.
    candidate_floor = max(asked_for, opens)
    if requested_day == today:
        candidate_floor = max(candidate_floor, now.astimezone(tz))

    candidate = align_to_slot(candidate_floor)

    if candidate + SLOT_DURATION > closes:
        # Never roll over to the next day -- see NEAREST_AVAILABLE_RULE.
        return SlotDecision(Verdict.TOO_LATE, adjusted_from=asked_for)

    return SlotDecision(Verdict.OK, slot=candidate, adjusted_from=asked_for)


def next_open_day(after: date, limit: int = 7) -> date | None:
    """The next day the clinic is open, strictly after ``after``.

    Used only to *suggest* an alternative in the reply. It never silently rebooks:
    the booking rule forbids rolling a request onto another day.
    """
    for offset in range(1, limit + 1):
        candidate = after + timedelta(days=offset)
        if is_business_day(candidate):
            return candidate
    return None


def format_slot(moment: datetime) -> str:
    """Human-readable appointment time, e.g. 'Monday 7 September at 2:00 PM'."""
    _require_aware(moment, "format_slot")
    hour = moment.strftime("%I").lstrip("0") or "12"
    return (
        f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')} "
        f"at {hour}:{moment.strftime('%M %p')}"
    )


def format_day(day: date) -> str:
    """Human-readable date without a time, e.g. 'Monday 7 September'."""
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def opening_hours_sentence() -> str:
    """One line describing when the clinic is open, for use in error replies."""
    open_h = OPEN_TIME.strftime("%I").lstrip("0")
    close_h = CLOSE_TIME.strftime("%I").lstrip("0")
    return (
        f"We're open Monday to Friday, {open_h}:{OPEN_TIME.strftime('%M %p')} to "
        f"{close_h}:{CLOSE_TIME.strftime('%M %p')}."
    )
