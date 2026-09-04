"""Routing behaviour: pre-checks, state precedence, intent dispatch."""

from __future__ import annotations

import pytest

from app.agent.handlers.booking import NOT_WIRED_YET
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

from tests.conftest import FakeGroqClient, classification

CHAT_ID = 555


# --------------------------------------------------------------- intent dispatch

@pytest.mark.parametrize(
    "intent, faq_topic, expected_fragment",
    [
        ("greeting", None, "Hello!"),
        ("faq", "location", CLINIC_ADDRESS),
        ("faq", "walk_ins", "appointment only"),
        ("out_of_scope", None, "can't help with that"),
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


async def test_booking_echoes_and_stores_the_raw_time_phrase(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    fake_llm.queue(
        classification("booking", raw_datetime_text="Monday at 2pm")
    )

    reply = await orchestrator.handle(CHAT_ID, {"text": "can I book Monday at 2pm?"})

    assert "Monday at 2pm" in reply.text
    assert NOT_WIRED_YET in reply.text
    assert (await store.get(CHAT_ID)).raw_datetime_text == "Monday at 2pm"


async def test_booking_stub_leaves_the_conversation_idle(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """Phase 1 must not park the user in a flow that cannot advance."""
    fake_llm.queue(classification("booking", raw_datetime_text="tomorrow"))

    await orchestrator.handle(CHAT_ID, {"text": "book me tomorrow"})

    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------- state precedence over intent

@pytest.mark.parametrize(
    "stage",
    ["awaiting_slot_confirmation", "awaiting_email", "awaiting_final_confirmation"],
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

    failing = FakeGroqClient(error=LLMError("groq is down"))
    subject = Orchestrator(IntentRouter(failing), store)

    reply = await subject.handle(CHAT_ID, {"text": "hello"})

    assert reply.text == TROUBLE_MESSAGE


async def test_history_records_both_sides_of_the_exchange(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    fake_llm.queue(classification("greeting"))

    await orchestrator.handle(CHAT_ID, {"text": "hi"})

    history = (await store.get(CHAT_ID)).history
    assert [turn.role for turn in history] == ["user", "bot"]
