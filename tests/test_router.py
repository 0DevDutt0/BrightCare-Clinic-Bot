"""Layer 1 parsing and validation, including every way the model can misbehave."""

from __future__ import annotations

import json

import pytest

from app.agent.llm import LLMError
from app.agent.router import MIN_CONFIDENCE, Classification, IntentRouter, RoutingError

from tests.conftest import FakeGroqClient, classification


async def test_valid_output_produces_a_classification() -> None:
    llm = FakeGroqClient()
    llm.queue(classification("booking", 0.91, raw_datetime_text="Monday at 2pm"))

    result = await IntentRouter(llm).classify("can I book Monday at 2pm?")

    assert result.intent == "booking"
    assert result.confidence == 0.91
    assert result.raw_datetime_text == "Monday at 2pm"
    assert result.is_confident


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "{unclosed",
        "",
        "[]",                      # valid JSON, wrong shape
        '{"intent": "banana", "confidence": 0.9}',
        '{"intent": "faq"}',       # confidence missing
        '{"intent": "faq", "confidence": 7}',  # out of range
    ],
)
async def test_unusable_output_raises_routing_error(payload: str) -> None:
    """Every malformed shape must converge on one catchable error type."""
    llm = FakeGroqClient()
    llm.queue(payload)

    with pytest.raises(RoutingError):
        await IntentRouter(llm).classify("hello")


async def test_transport_failure_becomes_a_routing_error() -> None:
    llm = FakeGroqClient(error=LLMError("timeout"))

    with pytest.raises(RoutingError):
        await IntentRouter(llm).classify("hello")


async def test_unknown_faq_topic_is_dropped_not_fatal() -> None:
    """A bad topic still came with a good intent; keep the intent."""
    llm = FakeGroqClient()
    llm.queue(classification("faq", faq_topic="astrology"))

    result = await IntentRouter(llm).classify("what's my sign?")

    assert result.intent == "faq"
    assert result.faq_topic is None


@pytest.mark.parametrize("null_ish", ["null", "none", "", "  ", "N/A"])
async def test_stringified_nulls_become_none(null_ish: str) -> None:
    llm = FakeGroqClient()
    llm.queue(
        json.dumps(
            {
                "intent": "greeting",
                "confidence": 0.9,
                "faq_topic": null_ish,
                "raw_datetime_text": null_ish,
            }
        )
    )

    result = await IntentRouter(llm).classify("hi")

    assert result.faq_topic is None
    assert result.raw_datetime_text is None


async def test_code_fenced_json_is_still_parsed() -> None:
    """JSON mode should prevent fences, but models add them anyway."""
    llm = FakeGroqClient()
    llm.queue("```json\n" + json.dumps(classification("greeting")) + "\n```")

    result = await IntentRouter(llm).classify("hi")

    assert result.intent == "greeting"


async def test_extra_fields_are_ignored() -> None:
    llm = FakeGroqClient()
    llm.queue(
        json.dumps({**classification("greeting"), "reasoning": "chatty model"})
    )

    result = await IntentRouter(llm).classify("hi")

    assert result.intent == "greeting"


async def test_the_user_message_is_sent_not_the_prompt_only() -> None:
    llm = FakeGroqClient()
    llm.queue(classification("greeting"))

    await IntentRouter(llm).classify("  hello there  ")

    system_prompt, user_message = llm.prompts[0]
    assert user_message == "hello there"          # trimmed
    assert "intent" in system_prompt              # schema instructions present


async def test_overlong_input_is_truncated_before_the_api_call() -> None:
    llm = FakeGroqClient()
    llm.queue(classification("out_of_scope"))

    await IntentRouter(llm).classify("x" * 10_000)

    _, user_message = llm.prompts[0]
    assert len(user_message) == 2000


@pytest.mark.parametrize(
    "confidence, expected",
    [(0.0, False), (0.59, False), (MIN_CONFIDENCE, True), (1.0, True)],
)
def test_confidence_threshold_boundary(confidence: float, expected: bool) -> None:
    result = Classification(intent="faq", confidence=confidence)
    assert result.is_confident is expected
