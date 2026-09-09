"""Single source of truth for everything the clinic knows about itself.

Every later phase reads from here: the FAQ handler quotes these facts verbatim so the
LLM cannot invent them, and Phase 3's slot search derives its candidates from the same
hours and slot grid.

Time zone is injected rather than imported from settings, so these rules stay pure and
testable. Callers pass ``settings.tz``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterator, Literal
from zoneinfo import ZoneInfo

CLINIC_NAME = "BrightCare Clinic"
CLINIC_ADDRESS = "12 Orchard Rd"

# --- Opening hours -----------------------------------------------------------------
OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)
# date.weekday(): Monday is 0, Sunday is 6. Saturday and Sunday are closed.
BUSINESS_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
HOURS_HUMAN = "Monday to Friday, 9:00 AM to 6:00 PM"

# --- Appointment grid --------------------------------------------------------------
SLOT_MINUTES = 30
SLOT_DURATION = timedelta(minutes=SLOT_MINUTES)
# Appointments begin on the hour or the half hour, never at arbitrary minutes.
SLOT_START_MINUTES = (0, 30)

# --- Policies ----------------------------------------------------------------------
WALK_INS_ACCEPTED = False
PARKING_AVAILABLE = True

# --- Phase 3 booking rule ----------------------------------------------------------
# Machine-checkable form of the rule, so the slot search cannot drift from the prose.
ROLL_OVER_TO_NEXT_DAY = False
NEAREST_AVAILABLE_RULE = (
    "The nearest available appointment is the soonest free 30-minute slot at or after "
    "the requested time, on the same business day as the request. If no slot remains "
    "that day, say so -- never roll the booking over to the next day."
)

FaqTopic = Literal[
    "location", "hours", "walk_ins", "cancellation", "parking", "appointment_length"
]

# The FAQ handler passes exactly one of these into the prompt as the only permitted
# source of fact. Keep each entry a complete, standalone statement.
FAQ_FACTS: dict[str, str] = {
    "location": f"{CLINIC_NAME} is located at {CLINIC_ADDRESS}.",
    "hours": f"The clinic is open {HOURS_HUMAN}, and closed on Saturday and Sunday.",
    "walk_ins": (
        "The clinic does not accept walk-ins. Visits are by appointment only."
    ),
    "cancellation": (
        "To cancel or move an appointment, just tell me here. I'll ask which email "
        "address you booked with, send a 6-digit code to it to check it's you, and make "
        "the change once you've entered the code."
    ),
    "parking": "On-site parking is available at the clinic.",
    "appointment_length": f"Each appointment is {SLOT_MINUTES} minutes long.",
}

CAPABILITIES = (
    "I can answer questions about the clinic -- our location, opening hours, parking, "
    "and how appointments work -- and I can book an appointment for you, move an "
    "existing one, or cancel it."
)

WELCOME = (
    f"Hello, and welcome to {CLINIC_NAME}. {CAPABILITIES}\n\n"
    "Try asking \"where are you located?\", \"can I book Monday at 2pm?\" or "
    "\"I need to move my appointment\"."
)


def is_business_day(day: date) -> bool:
    """True when the clinic is open on this calendar date."""
    return day.weekday() in BUSINESS_WEEKDAYS


def business_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Opening and closing instants for ``day``, or None when closed.

    Both returned datetimes are timezone-aware.
    """
    if not is_business_day(day):
        return None
    return (
        datetime.combine(day, OPEN_TIME, tzinfo=tz),
        datetime.combine(day, CLOSE_TIME, tzinfo=tz),
    )


def slot_starts(day: date, tz: ZoneInfo) -> Iterator[datetime]:
    """Every valid appointment start on ``day``, earliest first.

    A slot must finish by closing time, so the last start on a 09:00-18:00 day is
    17:30. Yields nothing when the clinic is closed.
    """
    window = business_window(day, tz)
    if window is None:
        return
    opens, closes = window
    current = opens
    while current + SLOT_DURATION <= closes:
        yield current
        current += SLOT_DURATION


def is_valid_slot_start(moment: datetime) -> bool:
    """True when ``moment`` lands on the clinic's slot grid within opening hours.

    Requires an aware datetime: a naive one cannot be checked against a real window.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("is_valid_slot_start requires a timezone-aware datetime")
    if not is_business_day(moment.date()):
        return False
    if moment.minute not in SLOT_START_MINUTES or moment.second or moment.microsecond:
        return False
    return OPEN_TIME <= moment.time() and _fits_before_close(moment)


def _fits_before_close(moment: datetime) -> bool:
    end = (moment + SLOT_DURATION).time()
    # A slot ending exactly at closing time is allowed; one crossing it is not.
    return end <= CLOSE_TIME and moment.time() < CLOSE_TIME
