"""No naive datetimes anywhere.

A naive datetime is the failure mode that does not announce itself: it compares
cleanly against an aware one only by raising TypeError deep in Phase 3's slot search,
or worse, silently represents the wrong wall clock. These tests assert the invariant
at every point the app produces a datetime.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain import business
from app.state.models import ConversationState, Turn, utc_now

TZ = ZoneInfo("Asia/Kolkata")


def assert_aware(value: datetime, label: str) -> None:
    assert value.tzinfo is not None, f"{label} is naive"
    assert value.tzinfo.utcoffset(value) is not None, f"{label} has no offset"


def test_utc_now_is_aware() -> None:
    assert_aware(utc_now(), "utc_now()")


def test_state_defaults_are_aware() -> None:
    state = ConversationState(chat_id=1)
    assert_aware(state.updated_at, "updated_at")


def test_turn_default_is_aware() -> None:
    assert_aware(Turn(role="user", text="hi").at, "Turn.at")


@pytest.mark.parametrize("field", ["requested_start", "proposed_start", "updated_at"])
def test_naive_datetime_is_rejected_on_construction(field: str) -> None:
    with pytest.raises(ValidationError):
        ConversationState(chat_id=1, **{field: datetime(2026, 9, 7, 14, 0)})


def test_aware_datetime_is_accepted() -> None:
    state = ConversationState(
        chat_id=1, requested_start=datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
    )
    assert_aware(state.requested_start, "requested_start")


def test_add_turn_keeps_updated_at_aware() -> None:
    state = ConversationState(chat_id=1)
    state.add_turn("user", "hi")
    assert_aware(state.updated_at, "updated_at after add_turn")


def test_reset_flow_keeps_updated_at_aware() -> None:
    state = ConversationState(chat_id=1, stage="awaiting_email")
    state.reset_flow()
    assert_aware(state.updated_at, "updated_at after reset_flow")


def test_history_is_capped() -> None:
    state = ConversationState(chat_id=1)
    for i in range(50):
        state.add_turn("user", f"message {i}")

    assert len(state.history) == 10
    assert state.history[-1].text == "message 49"


# ------------------------------------------------------- domain helpers stay aware

def test_every_slot_start_across_a_full_week_is_aware() -> None:
    monday = date(2026, 9, 7)
    produced = 0
    for offset in range(7):
        day = monday + timedelta(days=offset)
        for slot in business.slot_starts(day, TZ):
            assert_aware(slot, f"slot on {day}")
            produced += 1
    # Five business days, eighteen 30-minute slots each between 09:00 and 18:00.
    assert produced == 5 * 18


def test_business_window_is_aware_and_closed_at_weekends() -> None:
    window = business.business_window(date(2026, 9, 7), TZ)
    assert window is not None
    assert_aware(window[0], "opening")
    assert_aware(window[1], "closing")

    assert business.business_window(date(2026, 9, 5), TZ) is None  # Saturday
    assert business.business_window(date(2026, 9, 6), TZ) is None  # Sunday


def test_slot_validation_refuses_a_naive_datetime() -> None:
    """Silently answering False would hide the bug; raising surfaces it."""
    with pytest.raises(ValueError, match="timezone-aware"):
        business.is_valid_slot_start(datetime(2026, 9, 7, 14, 0))


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime(2026, 9, 7, 9, 0, tzinfo=TZ), True),    # opening slot
        (datetime(2026, 9, 7, 17, 30, tzinfo=TZ), True),  # last slot that fits
        (datetime(2026, 9, 7, 14, 15, tzinfo=TZ), False),  # off the :00/:30 grid
        (datetime(2026, 9, 7, 8, 30, tzinfo=TZ), False),   # before opening
        (datetime(2026, 9, 7, 18, 0, tzinfo=TZ), False),   # would end after close
        (datetime(2026, 9, 5, 10, 0, tzinfo=TZ), False),   # Saturday
    ],
)
def test_slot_grid_boundaries(moment: datetime, expected: bool) -> None:
    assert business.is_valid_slot_start(moment) is expected
