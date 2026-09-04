"""Layer 2 parsing and validation, plus the booking replies built on it."""

from __future__ import annotations

import json
from datetime import date, datetime, time

import pytest

from app.agent.handlers.booking import (
    ASK_FOR_TIME,
    NOT_WIRED_YET,
    TROUBLE_RESOLVING,
    booking_reply,
)
from app.agent.llm import LLMError
from app.agent.prompts import build_resolver_prompt
from app.agent.resolver import (
    MIN_RESOLUTION_CONFIDENCE,
    DatetimeResolver,
    Resolution,
    ResolutionError,
)
from app.state.models import ConversationState

from tests.conftest import FIXED_NOW, TZ, FakeGroqClient, resolution


# --------------------------------------------------------------- parsing

async def test_a_full_datetime_is_parsed() -> None:
    llm = FakeGroqClient()
    llm.queue(resolution("2026-09-07", "14:00"))

    result = await DatetimeResolver(llm).resolve("Monday at 2pm", FIXED_NOW)

    assert result.day == date(2026, 9, 7)
    assert result.clock == time(14, 0)
    assert result.is_confident


async def test_a_day_without_a_time_is_a_valid_answer() -> None:
    """"Monday" resolves to a date and no clock -- not a failure."""
    llm = FakeGroqClient()
    llm.queue(resolution("2026-09-07", None))

    result = await DatetimeResolver(llm).resolve("Monday", FIXED_NOW)

    assert result.day == date(2026, 9, 7)
    assert result.clock is None
    assert result.has_day


async def test_an_unresolvable_phrase_yields_no_day() -> None:
    llm = FakeGroqClient()
    llm.queue(resolution(None, None, confidence=0.2))

    result = await DatetimeResolver(llm).resolve("sometime soon", FIXED_NOW)

    assert result.has_day is False
    assert result.is_confident is False


@pytest.mark.parametrize("null_ish", ["null", "none", "", "  ", "unknown"])
async def test_stringified_nulls_become_none(null_ish: str) -> None:
    llm = FakeGroqClient()
    llm.queue(json.dumps({"date": null_ish, "time": null_ish, "confidence": 0.3}))

    result = await DatetimeResolver(llm).resolve("whenever", FIXED_NOW)

    assert result.day is None
    assert result.clock is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{unclosed",
        '{"date": "2026-13-45", "time": "14:00", "confidence": 0.9}',  # month 13
        '{"date": "2026-09-07", "time": "25:00", "confidence": 0.9}',  # hour 25
        '{"date": "2026-09-07", "time": "14:00"}',                     # no confidence
        '{"date": "2026-09-07", "time": "14:00", "confidence": 5}',    # out of range
    ],
)
async def test_unusable_output_raises_resolution_error(payload: str) -> None:
    """An impossible date must fail loudly, not become a nonsense appointment."""
    llm = FakeGroqClient()
    llm.queue(payload)

    with pytest.raises(ResolutionError):
        await DatetimeResolver(llm).resolve("Monday", FIXED_NOW)


async def test_transport_failure_becomes_a_resolution_error() -> None:
    llm = FakeGroqClient(error=LLMError("timeout"))

    with pytest.raises(ResolutionError):
        await DatetimeResolver(llm).resolve("Monday", FIXED_NOW)


async def test_code_fenced_json_is_still_parsed() -> None:
    llm = FakeGroqClient()
    llm.queue("```json\n" + json.dumps(resolution("2026-09-07", "14:00")) + "\n```")

    result = await DatetimeResolver(llm).resolve("Monday at 2pm", FIXED_NOW)

    assert result.day == date(2026, 9, 7)


async def test_a_naive_reference_time_is_refused() -> None:
    llm = FakeGroqClient()

    with pytest.raises(ValueError, match="timezone-aware"):
        await DatetimeResolver(llm).resolve("Monday", datetime(2026, 9, 4, 11, 30))


def test_confidence_threshold_boundary() -> None:
    assert Resolution(confidence=MIN_RESOLUTION_CONFIDENCE).is_confident
    assert not Resolution(confidence=MIN_RESOLUTION_CONFIDENCE - 0.01).is_confident


# --------------------------------------------------------------- the prompt

def test_the_prompt_carries_the_reference_moment() -> None:
    """Without today's date in the prompt, "tomorrow" cannot be resolved at all."""
    prompt = build_resolver_prompt(FIXED_NOW)

    assert "2026-09-04" in prompt
    assert "Friday" in prompt
    assert "Asia/Kolkata" in prompt


def test_the_prompt_does_not_leak_opening_hours() -> None:
    """Layer 2 resolves words; whether a time is bookable is decided in code."""
    prompt = build_resolver_prompt(FIXED_NOW).lower()

    assert "09:00 to 18:00" not in prompt
    assert "closed" not in prompt


# ------------------------------------------------- booking replies end to end

async def _reply(queued: object, phrase: str | None = "Monday at 2pm") -> str:
    llm = FakeGroqClient()
    if queued is not None:
        llm.queue(queued)  # type: ignore[arg-type]
    state = ConversationState(chat_id=1)
    return await booking_reply(state, phrase, DatetimeResolver(llm), FIXED_NOW, TZ)


async def test_no_time_phrase_asks_for_one_without_calling_the_model() -> None:
    llm = FakeGroqClient()
    state = ConversationState(chat_id=1)

    reply = await booking_reply(state, None, DatetimeResolver(llm), FIXED_NOW, TZ)

    assert reply == ASK_FOR_TIME
    assert llm.call_count == 0


async def test_a_bookable_time_is_confirmed_in_words() -> None:
    reply = await _reply(resolution("2026-09-07", "14:00"))

    assert "Monday 7 September at 2:00 PM" in reply
    assert NOT_WIRED_YET in reply


async def test_an_adjusted_time_says_so() -> None:
    """A silent shift is how a patient turns up at the wrong time."""
    reply = await _reply(resolution("2026-09-07", "14:15"))

    assert "2:30 PM" in reply
    assert "at or after" in reply


async def test_a_weekend_request_is_refused_and_suggests_a_weekday() -> None:
    reply = await _reply(resolution("2026-09-05", "10:00"))  # Saturday

    assert "closed" in reply.lower()
    assert "Monday 7 September" in reply


async def test_a_past_date_is_refused() -> None:
    reply = await _reply(resolution("2026-09-01", "10:00"))

    assert "already passed" in reply


async def test_a_late_request_is_refused_without_rolling_over() -> None:
    reply = await _reply(resolution("2026-09-07", "18:30"))

    assert "left that late" in reply
    # It must not quietly offer the next morning instead.
    assert "Tuesday" not in reply


async def test_a_far_future_request_is_refused() -> None:
    reply = await _reply(resolution("2027-06-01", "10:00"))

    assert "90 days" in reply


async def test_an_ambiguous_phrase_asks_for_a_specific_day() -> None:
    reply = await _reply(resolution("2026-09-07", "14:00", confidence=0.3))

    assert "not sure which date" in reply


async def test_a_resolver_failure_degrades_gracefully() -> None:
    llm = FakeGroqClient(error=LLMError("groq down"))
    state = ConversationState(chat_id=1)

    reply = await booking_reply(
        state, "Monday at 2pm", DatetimeResolver(llm), FIXED_NOW, TZ
    )

    assert reply == TROUBLE_RESOLVING


async def test_a_bookable_slot_is_stored_as_an_aware_datetime() -> None:
    llm = FakeGroqClient()
    llm.queue(resolution("2026-09-07", "14:00"))
    state = ConversationState(chat_id=1)

    await booking_reply(state, "Monday at 2pm", DatetimeResolver(llm), FIXED_NOW, TZ)

    assert state.requested_start == datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
    assert state.requested_start.tzinfo is not None


async def test_a_rejected_request_stores_no_slot() -> None:
    llm = FakeGroqClient()
    llm.queue(resolution("2026-09-05", "10:00"))  # Saturday
    state = ConversationState(chat_id=1)

    await booking_reply(state, "Saturday at 10", DatetimeResolver(llm), FIXED_NOW, TZ)

    assert state.requested_start is None
