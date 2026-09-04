"""Business-hours validation and slot alignment.

These are the rules a wrong answer actually harms a patient with, so they are tested
exhaustively and without the model anywhere in the picture.

Reference: Friday 2026-09-04 11:30 IST. 2026-09-05 is a Saturday, 2026-09-07 a Monday.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.domain.scheduling import (
    BOOKING_HORIZON_DAYS,
    SlotDecision,
    Verdict,
    align_to_slot,
    evaluate_request,
    format_day,
    format_slot,
    last_slot_start,
    next_open_day,
)

from tests.conftest import FIXED_NOW, TZ

MONDAY = date(2026, 9, 7)
SATURDAY = date(2026, 9, 5)
SUNDAY = date(2026, 9, 6)
TODAY = date(2026, 9, 4)  # Friday


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


# ------------------------------------------------------------- slot alignment

@pytest.mark.parametrize(
    "given, expected",
    [
        ((14, 0), (14, 0)),    # already on the grid
        ((14, 1), (14, 30)),   # rounds up, never down
        ((14, 15), (14, 30)),
        ((14, 29), (14, 30)),
        ((14, 30), (14, 30)),
        ((14, 31), (15, 0)),
        ((14, 59), (15, 0)),
    ],
)
def test_alignment_always_rounds_up(
    given: tuple[int, int], expected: tuple[int, int]
) -> None:
    """The booking rule says at or after the requested time, so never round back."""
    assert align_to_slot(at(MONDAY, *given)) == at(MONDAY, *expected)


def test_alignment_discards_seconds() -> None:
    moment = datetime(2026, 9, 7, 14, 0, 30, tzinfo=TZ)
    assert align_to_slot(moment) == at(MONDAY, 14, 30)


def test_alignment_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        align_to_slot(datetime(2026, 9, 7, 14, 0))


def test_last_slot_start_leaves_room_for_the_appointment() -> None:
    assert last_slot_start(MONDAY, TZ) == at(MONDAY, 17, 30)
    assert last_slot_start(SATURDAY, TZ) is None


# --------------------------------------------------------------- happy paths

def test_a_normal_request_is_bookable() -> None:
    decision = evaluate_request(MONDAY, time(14, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(MONDAY, 14, 0)
    assert not decision.was_adjusted


def test_a_day_with_no_time_starts_at_opening() -> None:
    """"Monday" means the whole day, so the candidate is the first slot on it."""
    decision = evaluate_request(MONDAY, None, FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(MONDAY, 9, 0)


def test_the_last_slot_of_the_day_is_bookable() -> None:
    decision = evaluate_request(MONDAY, time(17, 30), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(MONDAY, 17, 30)


def test_today_later_today_is_bookable() -> None:
    decision = evaluate_request(TODAY, time(15, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(TODAY, 15, 0)


# ------------------------------------------------------------- adjustments

def test_an_off_grid_time_moves_to_the_next_slot() -> None:
    decision = evaluate_request(MONDAY, time(14, 15), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(MONDAY, 14, 30)
    assert decision.was_adjusted
    assert decision.adjusted_from == at(MONDAY, 14, 15)


def test_a_time_before_opening_moves_to_opening() -> None:
    decision = evaluate_request(MONDAY, time(7, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(MONDAY, 9, 0)
    assert decision.was_adjusted


def test_a_time_already_past_today_moves_to_the_next_slot_today() -> None:
    """Asking for 9am at 11:30 is still 'at or after' on the same day."""
    decision = evaluate_request(TODAY, time(9, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK
    assert decision.slot == at(TODAY, 11, 30)
    assert decision.was_adjusted


def test_a_request_for_right_now_takes_the_current_slot_boundary() -> None:
    decision = evaluate_request(TODAY, time(11, 30), FIXED_NOW, TZ)

    assert decision.slot == at(TODAY, 11, 30)


# ---------------------------------------------------------------- rejections

def test_a_past_day_is_rejected() -> None:
    decision = evaluate_request(date(2026, 9, 1), time(14, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.IN_PAST
    assert decision.slot is None


@pytest.mark.parametrize("closed", [SATURDAY, SUNDAY])
def test_weekends_are_rejected_not_rolled_forward(closed: date) -> None:
    """NEAREST_AVAILABLE_RULE forbids silently moving the booking to another day."""
    decision = evaluate_request(closed, time(10, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.CLOSED_DAY
    assert decision.slot is None


def test_after_the_last_slot_is_rejected_not_rolled_to_tomorrow() -> None:
    decision = evaluate_request(MONDAY, time(18, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.TOO_LATE
    assert decision.slot is None


def test_a_slot_that_would_overrun_closing_is_rejected() -> None:
    """17:45 would finish at 18:15, past closing -- and 18:00 has no room either."""
    decision = evaluate_request(MONDAY, time(17, 45), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.TOO_LATE


def test_today_after_closing_is_rejected() -> None:
    evening = datetime(2026, 9, 4, 19, 0, tzinfo=TZ)

    decision = evaluate_request(TODAY, time(19, 30), evening, TZ)

    assert decision.verdict is Verdict.TOO_LATE


def test_beyond_the_horizon_is_rejected() -> None:
    far = date(2027, 6, 1)

    decision = evaluate_request(far, time(10, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.TOO_FAR


def test_the_horizon_boundary_is_inclusive() -> None:
    from datetime import timedelta

    edge = TODAY + timedelta(days=BOOKING_HORIZON_DAYS)
    while edge.weekday() > 4:  # nudge onto a weekday so only the horizon is under test
        edge -= timedelta(days=1)

    decision = evaluate_request(edge, time(10, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.OK


def test_no_day_asks_for_one() -> None:
    decision = evaluate_request(None, time(14, 0), FIXED_NOW, TZ)

    assert decision.verdict is Verdict.NEEDS_DAY


def test_evaluate_refuses_a_naive_reference_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_request(MONDAY, time(14, 0), datetime(2026, 9, 4, 11, 30), TZ)


# ------------------------------------------------------ every decision stays aware

@pytest.mark.parametrize(
    "day, clock",
    [
        (MONDAY, time(14, 0)),
        (MONDAY, None),
        (MONDAY, time(14, 15)),
        (TODAY, time(9, 0)),
        (MONDAY, time(7, 0)),
    ],
)
def test_every_produced_slot_is_timezone_aware(day: date, clock: time | None) -> None:
    decision = evaluate_request(day, clock, FIXED_NOW, TZ)

    assert decision.slot is not None
    assert decision.slot.tzinfo is not None
    assert decision.slot.tzinfo.utcoffset(decision.slot) is not None


# ------------------------------------------------------------------- helpers

def test_next_open_day_skips_the_weekend() -> None:
    assert next_open_day(date(2026, 9, 4)) == MONDAY   # Friday -> Monday
    assert next_open_day(SATURDAY) == MONDAY
    assert next_open_day(MONDAY) == date(2026, 9, 8)   # Monday -> Tuesday


def test_slot_formatting_is_human_readable() -> None:
    assert format_slot(at(MONDAY, 14, 0)) == "Monday 7 September at 2:00 PM"
    assert format_slot(at(MONDAY, 9, 30)) == "Monday 7 September at 9:30 AM"
    assert format_slot(at(MONDAY, 12, 0)) == "Monday 7 September at 12:00 PM"


def test_day_formatting_is_human_readable() -> None:
    assert format_day(MONDAY) == "Monday 7 September"


def test_decision_convenience_properties() -> None:
    rejected = SlotDecision(Verdict.CLOSED_DAY)

    assert rejected.ok is False
    assert rejected.was_adjusted is False
