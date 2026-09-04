"""Shared fixtures.

The Groq client is faked at the ``complete_json`` seam -- the narrowest point that
still exercises the router's parsing, validation and error handling. Faking HTTP
instead would test the SDK; faking the router would skip the logic under test.

Every fake counts its calls, because several requirements are about work *not* done:
a sticker must cost zero model calls, and a mid-flow reply must skip the classifier.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent.llm import LLMError
from app.agent.orchestrator import Orchestrator
from app.agent.router import IntentRouter
from app.state.store import InMemoryStateStore
from app.telegram.handler import UpdateHandler


class FakeGroqClient:
    """Stands in for GroqClient. Returns queued payloads, or raises a queued error."""

    def __init__(
        self,
        responses: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.call_count = 0
        self.prompts: list[tuple[str, str]] = []

    def queue(self, payload: dict[str, Any] | str) -> None:
        self._responses.append(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    async def complete_json(
        self, system_prompt: str, user_message: str, max_tokens: int = 300
    ) -> str:
        self.call_count += 1
        self.prompts.append((system_prompt, user_message))
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise LLMError("FakeGroqClient has no queued response")
        return self._responses.pop(0)


class FakeTelegramClient:
    """Captures outbound messages instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    @property
    def last_text(self) -> str:
        assert self.sent, "no message was sent"
        return self.sent[-1][1]


class RecordingOrchestrator:
    """Minimal orchestrator double for transport-level tests."""

    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[tuple[int, dict[str, Any]]] = []

    async def handle(self, chat_id: int, message: dict[str, Any]) -> Any:
        self.calls.append((chat_id, message))
        if self._error is not None:
            raise self._error
        return self._reply


def classification(
    intent: str,
    confidence: float = 0.95,
    faq_topic: str | None = None,
    raw_datetime_text: str | None = None,
) -> dict[str, Any]:
    """Build a well-formed Layer 1 payload."""
    return {
        "intent": intent,
        "confidence": confidence,
        "faq_topic": faq_topic,
        "raw_datetime_text": raw_datetime_text,
    }


def text_update(text: str, update_id: int = 1, chat_id: int = 555) -> dict[str, Any]:
    """A Telegram update carrying a text message."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def non_text_update(
    kind: str = "sticker", update_id: int = 1, chat_id: int = 555
) -> dict[str, Any]:
    """A Telegram update carrying an attachment and no text."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            kind: {"file_id": "irrelevant"},
        },
    }


@pytest.fixture
def fake_llm() -> FakeGroqClient:
    return FakeGroqClient()


@pytest.fixture
def store() -> InMemoryStateStore:
    return InMemoryStateStore()


@pytest.fixture
def orchestrator(fake_llm: FakeGroqClient, store: InMemoryStateStore) -> Orchestrator:
    return Orchestrator(IntentRouter(fake_llm), store)


@pytest.fixture
def telegram() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def handler(
    telegram: FakeTelegramClient, orchestrator: Orchestrator
) -> UpdateHandler:
    return UpdateHandler(telegram, orchestrator)
