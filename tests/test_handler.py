"""Transport-level behaviour: deduplication, update filtering, failure isolation."""

from __future__ import annotations

import pytest

from app.agent.orchestrator import NON_TEXT_MESSAGE, Orchestrator, Reply
from app.telegram.handler import FAILURE_MESSAGE, UpdateDeduplicator, UpdateHandler

from tests.conftest import (
    FakeGroqClient,
    FakeTelegramClient,
    RecordingOrchestrator,
    classification,
    non_text_update,
    text_update,
)

CHAT_ID = 555


# ------------------------------------------------------------- deduplication

async def test_duplicate_update_id_is_dropped(
    telegram: FakeTelegramClient,
) -> None:
    agent = RecordingOrchestrator(Reply("ok", "greeting", used_llm=True))
    subject = UpdateHandler(telegram, agent)

    await subject.handle_update(text_update("hi", update_id=42))
    await subject.handle_update(text_update("hi", update_id=42))

    assert len(agent.calls) == 1
    assert len(telegram.sent) == 1


async def test_distinct_update_ids_are_both_processed(
    telegram: FakeTelegramClient,
) -> None:
    agent = RecordingOrchestrator(Reply("ok", "greeting"))
    subject = UpdateHandler(telegram, agent)

    await subject.handle_update(text_update("hi", update_id=1))
    await subject.handle_update(text_update("hi", update_id=2))

    assert len(agent.calls) == 2


def test_deduplicator_is_bounded() -> None:
    """Memory must stay flat on a long-running process."""
    dedup = UpdateDeduplicator(capacity=3)

    assert all(dedup.check_and_record(i) for i in range(5))
    assert len(dedup) == 3
    # The oldest id aged out, so it is no longer recognised as seen.
    assert dedup.check_and_record(0) is True
    assert dedup.check_and_record(4) is False


# ------------------------------------------------------- update kind filtering

@pytest.mark.parametrize(
    "key", ["edited_message", "channel_post", "edited_channel_post", "callback_query"]
)
async def test_ignored_update_kinds_never_reach_the_agent(
    telegram: FakeTelegramClient, key: str
) -> None:
    agent = RecordingOrchestrator(Reply("ok", "greeting"))
    subject = UpdateHandler(telegram, agent)

    await subject.handle_update(
        {"update_id": 7, key: {"chat": {"id": CHAT_ID}, "text": "hi"}}
    )

    assert agent.calls == []
    assert telegram.sent == []


async def test_update_without_a_message_is_ignored(
    telegram: FakeTelegramClient,
) -> None:
    agent = RecordingOrchestrator(Reply("ok", "greeting"))
    subject = UpdateHandler(telegram, agent)

    await subject.handle_update({"update_id": 8})

    assert agent.calls == []


# ---------------------------------------------------------- end-to-end wiring

async def test_sticker_produces_a_text_only_reply_and_zero_llm_calls(
    handler: UpdateHandler, telegram: FakeTelegramClient, fake_llm: FakeGroqClient
) -> None:
    """The acceptance criterion, asserted through the real orchestrator."""
    await handler.handle_update(non_text_update("sticker"))

    assert telegram.last_text == NON_TEXT_MESSAGE
    assert fake_llm.call_count == 0


async def test_start_command_produces_a_reply_and_zero_llm_calls(
    handler: UpdateHandler, telegram: FakeTelegramClient, fake_llm: FakeGroqClient
) -> None:
    await handler.handle_update(text_update("/start"))

    assert telegram.sent
    assert fake_llm.call_count == 0


async def test_text_message_flows_through_to_a_sent_reply(
    handler: UpdateHandler,
    telegram: FakeTelegramClient,
    fake_llm: FakeGroqClient,
) -> None:
    fake_llm.queue(classification("faq", faq_topic="location"))

    await handler.handle_update(text_update("where are you?"))

    assert "12 Orchard Rd" in telegram.last_text


# ------------------------------------------------------------ failure handling

async def test_agent_failure_still_answers_the_user(
    telegram: FakeTelegramClient,
) -> None:
    """Silence looks like a dead bot, so an internal error still gets a reply."""
    agent = RecordingOrchestrator(error=RuntimeError("boom"))
    subject = UpdateHandler(telegram, agent)

    await subject.handle_update(text_update("hi"))

    assert telegram.last_text == FAILURE_MESSAGE


async def test_send_failure_does_not_propagate(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    from app.telegram.client import TelegramError

    class BrokenTelegram:
        async def send_message(self, chat_id: int, text: str) -> None:
            raise TelegramError("network down")

    fake_llm.queue(classification("greeting"))
    subject = UpdateHandler(BrokenTelegram(), orchestrator)

    await subject.handle_update(text_update("hi"))  # must not raise
