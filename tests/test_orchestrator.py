"""Routing behaviour: pre-checks, state precedence, intent dispatch."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.handlers.faq import TOPIC_PROMPT
from app.agent.orchestrator import (
    CLARIFY_MESSAGE,
    EMPTY_MESSAGE,
    FLOW_CLEARED,
    NON_TEXT_MESSAGE,
    TROUBLE_MESSAGE,
    UNKNOWN_COMMAND,
    Orchestrator,
)
from app.agent.router import IntentRouter
from app.domain.business import CLINIC_ADDRESS, FAQ_FACTS, WELCOME
from app.state.models import ConversationState

from tests.conftest import (
    FIXED_NOW,
    TZ,
    FakeCalendarService,
    FakeEmailService,
    FakeGroqClient,
    classification,
    resolution,
)

CHAT_ID = 555


# --------------------------------------------------------------- intent dispatch

@pytest.mark.parametrize(
    "intent, faq_topic, expected_fragment",
    [
        ("greeting", None, "Hello!"),
        ("faq", "location", CLINIC_ADDRESS),
        ("faq", "walk_ins", "appointment only"),
        ("out_of_scope", None, "can't help with that"),
        ("cancel", None, "email address"),
    ],
)
async def test_each_intent_routes_to_its_handler(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    intent: str,
    faq_topic: str | None,
    expected_fragment: str,
) -> None:
    fake_llm.queue(classification(intent, faq_topic=faq_topic))

    reply = await orchestrator.handle(CHAT_ID, {"text": "anything"})

    assert reply.intent == intent
    assert reply.used_llm is True
    assert expected_fragment in reply.text


async def test_faq_answers_verbatim_from_domain_constants(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    """The reply is the constant itself, so the model cannot reword a fact."""
    fake_llm.queue(classification("faq", faq_topic="parking"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "is there parking?"})

    assert reply.text == FAQ_FACTS["parking"]


async def test_faq_without_topic_asks_which_topic(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    fake_llm.queue(classification("faq", faq_topic=None))

    reply = await orchestrator.handle(CHAT_ID, {"text": "tell me about the clinic"})

    assert reply.text == TOPIC_PROMPT


async def test_booking_resolves_the_phrase_and_stores_the_slot(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Layer 1 captures the phrase, Layer 2 resolves it, the slot lands in state."""
    fake_llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    fake_llm.queue(resolution("2026-09-07", "14:00"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "can I book Monday at 2pm?"})

    assert "Monday 7 September at 2:00 PM" in reply.text
    assert "Shall I book it?" in reply.text

    state = await store.get(CHAT_ID)
    assert state.raw_datetime_text == "Monday at 2pm"
    assert state.requested_start == datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
    assert state.proposed_start == datetime(2026, 9, 7, 14, 0, tzinfo=TZ)


async def test_booking_costs_exactly_two_model_calls(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    """One to classify, one to resolve -- and no more."""
    fake_llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    fake_llm.queue(resolution("2026-09-07", "14:00"))

    await orchestrator.handle(CHAT_ID, {"text": "can I book Monday at 2pm?"})

    assert fake_llm.call_count == 2


async def test_booking_without_a_time_phrase_skips_layer_two(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    """Nothing to resolve means no second call."""
    fake_llm.queue(classification("booking", raw_datetime_text=None))

    reply = await orchestrator.handle(CHAT_ID, {"text": "I'd like an appointment"})

    assert fake_llm.call_count == 1
    assert "What day and time" in reply.text


async def test_a_successful_proposal_opens_the_confirmation_stage(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Phase 3 does park the user in a flow -- because there is now one to advance."""
    fake_llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    fake_llm.queue(resolution("2026-09-07", "14:00"))

    await orchestrator.handle(CHAT_ID, {"text": "book me Monday at 2pm"})

    assert (await store.get(CHAT_ID)).stage == "awaiting_slot_confirmation"


async def test_a_rejected_request_leaves_the_conversation_idle(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Saturday is refused, so there is no flow to be stuck in."""
    fake_llm.queue(classification("booking", raw_datetime_text="tomorrow"))
    fake_llm.queue(resolution("2026-09-05", None))  # Saturday

    await orchestrator.handle(CHAT_ID, {"text": "book me tomorrow"})

    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------- state precedence over intent

async def test_cancelling_costs_one_model_call_and_never_reaches_layer_two(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Which appointment is settled by the calendar and a numbered choice, not by
    resolving a phrase and hoping it names exactly one."""
    fake_llm.queue(classification("cancel", raw_datetime_text="Monday"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "cancel my Monday appointment"})

    assert fake_llm.call_count == 1
    assert reply.intent == "cancel"
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_email"


async def test_asking_how_to_cancel_answers_instead_of_starting_the_flow(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """A question deserves an answer, not an unrequested destructive flow."""
    fake_llm.queue(classification("faq", faq_topic="cancellation"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "how do I cancel?"})

    assert reply.text == FAQ_FACTS["cancellation"]
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_starting_a_cancellation_abandons_a_booking_in_flight(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Two flows cannot share one stage field, and the newer request is the live one."""
    fake_llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    fake_llm.queue(resolution("2026-09-07", "14:00"))
    await orchestrator.handle(CHAT_ID, {"text": "book Monday at 2pm"})

    fake_llm.queue(classification("cancel"))
    await orchestrator.handle(CHAT_ID, {"text": "actually I need to cancel one"})

    state = await store.get(CHAT_ID)
    assert state.stage == "awaiting_cancel_email"
    assert state.proposed_start is None


@pytest.mark.parametrize(
    "stage",
    [
        "awaiting_slot_confirmation",
        "awaiting_email",
        "awaiting_final_confirmation",
        "awaiting_cancel_email",
        "awaiting_cancel_confirmation",
        "awaiting_cancel_code",
    ],
)
async def test_active_flow_bypasses_the_classifier_entirely(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store, stage: str
) -> None:
    """"yes" in a flow is an answer, not a greeting -- and costs no model call."""
    state = ConversationState(chat_id=CHAT_ID, stage=stage)
    await store.save(state)

    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert fake_llm.call_count == 0
    assert reply.used_llm is False
    assert reply.intent == f"continuation.{stage}"


async def test_escape_phrase_clears_a_stuck_flow(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    await store.save(ConversationState(chat_id=CHAT_ID, stage="awaiting_email"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "cancel"})

    assert reply.text == FLOW_CLEARED
    assert fake_llm.call_count == 0
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_idle_conversation_does_reach_the_classifier(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    """Counterpart to the bypass test: idle must not skip classification."""
    fake_llm.queue(classification("greeting"))

    await orchestrator.handle(CHAT_ID, {"text": "hi"})

    assert fake_llm.call_count == 1


# ------------------------------------------------------ pre-checks, no LLM call

async def test_non_text_message_never_reaches_the_llm(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    reply = await orchestrator.handle(CHAT_ID, {"sticker": {"file_id": "abc"}})

    assert reply.text == NON_TEXT_MESSAGE
    assert reply.used_llm is False
    assert fake_llm.call_count == 0


@pytest.mark.parametrize("command", ["/start", "/help", "/start@BrightCareBot"])
async def test_commands_never_reach_the_llm(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, command: str
) -> None:
    reply = await orchestrator.handle(CHAT_ID, {"text": command})

    assert reply.text == WELCOME
    assert fake_llm.call_count == 0


async def test_start_clears_an_active_flow(
    orchestrator: Orchestrator, store
) -> None:
    await store.save(ConversationState(chat_id=CHAT_ID, stage="awaiting_email"))

    await orchestrator.handle(CHAT_ID, {"text": "/start"})

    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_unknown_command_never_reaches_the_llm(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    reply = await orchestrator.handle(CHAT_ID, {"text": "/wharglebargle"})

    assert reply.text == UNKNOWN_COMMAND
    assert fake_llm.call_count == 0


@pytest.mark.parametrize("blank", ["", "   ", "\n\t  "])
async def test_blank_text_asks_for_clarification_without_the_llm(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, blank: str
) -> None:
    reply = await orchestrator.handle(CHAT_ID, {"text": blank})

    assert reply.text == EMPTY_MESSAGE
    assert fake_llm.call_count == 0


# ------------------------------------------------------------- failure handling

async def test_low_confidence_asks_rather_than_guessing(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    fake_llm.queue(classification("booking", confidence=0.42))

    reply = await orchestrator.handle(CHAT_ID, {"text": "ok"})

    assert reply.text == CLARIFY_MESSAGE
    assert reply.intent == "unclear"


async def test_malformed_llm_output_falls_back_without_raising(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    fake_llm.queue("this is not JSON at all")

    reply = await orchestrator.handle(CHAT_ID, {"text": "hello"})

    assert reply.text == TROUBLE_MESSAGE
    assert reply.intent == "error"


async def test_llm_transport_failure_falls_back_without_raising(
    store,
) -> None:
    from app.agent.llm import LLMError
    from app.agent.resolver import DatetimeResolver

    failing = FakeGroqClient(error=LLMError("groq is down"))
    subject = Orchestrator(
        router=IntentRouter(failing),
        store=store,
        resolver=DatetimeResolver(failing),
        calendar=FakeCalendarService(),
        email=FakeEmailService(),
        tz=TZ,
        now=lambda: FIXED_NOW,
    )

    reply = await subject.handle(CHAT_ID, {"text": "hello"})

    assert reply.text == TROUBLE_MESSAGE


async def test_history_records_both_sides_of_the_exchange(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    fake_llm.queue(classification("greeting"))

    await orchestrator.handle(CHAT_ID, {"text": "hi"})

    history = (await store.get(CHAT_ID)).history
    assert [turn.role for turn in history] == ["user", "bot"]
