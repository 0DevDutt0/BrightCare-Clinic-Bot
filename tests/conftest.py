"""Shared fixtures.

The Groq client is faked at the ``complete_json`` seam -- the narrowest point that
still exercises the router's parsing, validation and error handling. Faking HTTP
instead would test the SDK; faking the router would skip the logic under test.

Every fake counts its calls, because several requirements are about work *not* done:
a sticker must cost zero model calls, and a mid-flow reply must skip the classifier.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agent.llm import LLMError
from app.agent.orchestrator import Orchestrator
from app.agent.resolver import DatetimeResolver
from app.agent.router import IntentRouter
from app.domain.business import SLOT_DURATION, slot_starts
from app.services.calendar_service import CalendarService
from app.state.store import InMemoryStateStore
from app.telegram.handler import UpdateHandler

TZ = ZoneInfo("Asia/Kolkata")

# Friday, mid-morning, mid-week: far enough from a weekend that "tomorrow" and
# "Monday" are distinct, and inside opening hours so "today at 3pm" is bookable.
# Tests read against this instead of the wall clock, so none of them rot.
FIXED_NOW = datetime(2026, 9, 4, 11, 30, tzinfo=TZ)


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


class FakeCalendarService(CalendarService):
    """In-memory calendar with real slot-grid behaviour.

    Busy times are given as the slot starts that are taken, so a test reads as
    "14:00 is booked" rather than as a list of RFC 3339 intervals. Availability is
    computed from the same domain helpers the live service uses, so a test that
    passes here is testing the rule, not a hand-written answer.
    """

    def __init__(
        self,
        busy: set[datetime] | None = None,
        error: Exception | None = None,
        tz: ZoneInfo = TZ,
    ) -> None:
        self.busy = set(busy or ())
        self.error = error
        self.tz = tz
        self.created: list[dict[str, Any]] = []
        # Slots that fall away between proposing and confirming, to exercise the race.
        self.taken_on_next_check: set[datetime] = set()
        self.calls: list[str] = []

    def _guard(self, call: str) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    async def list_busy(
        self, window_start: datetime, window_end: datetime
    ) -> list[tuple[datetime, datetime]]:
        self._guard("list_busy")
        return [
            (slot, slot + SLOT_DURATION)
            for slot in sorted(self.busy)
            if slot < window_end and slot + SLOT_DURATION > window_start
        ]

    async def find_nearest_available(self, requested_start: datetime) -> datetime | None:
        self._guard("find_nearest_available")
        day = requested_start.astimezone(self.tz).date()
        for slot in slot_starts(day, self.tz):
            if slot >= requested_start and slot not in self.busy:
                return slot
        return None

    async def is_free(self, start: datetime) -> bool:
        self._guard("is_free")
        if start in self.taken_on_next_check:
            self.busy.add(start)
            self.taken_on_next_check.discard(start)
            return False
        return start not in self.busy

    async def create_event(
        self,
        start: datetime,
        summary: str,
        description: str = "",
        attendee_email: str | None = None,
    ) -> str:
        self._guard("create_event")
        self.busy.add(start)
        self.created.append(
            {
                "start": start,
                "summary": summary,
                "description": description,
                "attendee_email": attendee_email,
            }
        )
        return f"evt-{len(self.created)}"


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


def resolution(
    day: str | None = None,
    clock: str | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Build a well-formed Layer 2 payload (the model emits date/time, not day/clock)."""
    return {"date": day, "time": clock, "confidence": confidence}


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
def calendar() -> FakeCalendarService:
    return FakeCalendarService()


@pytest.fixture
def orchestrator(
    fake_llm: FakeGroqClient,
    store: InMemoryStateStore,
    calendar: FakeCalendarService,
) -> Orchestrator:
    """Both layers share one fake client, so queued responses are consumed in order:
    the classification first, then the resolution if the booking path reaches it."""
    return Orchestrator(
        router=IntentRouter(fake_llm),
        store=store,
        resolver=DatetimeResolver(fake_llm),
        calendar=calendar,
        tz=TZ,
        now=lambda: FIXED_NOW,
    )


@pytest.fixture
def telegram() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def handler(
    telegram: FakeTelegramClient, orchestrator: Orchestrator
) -> UpdateHandler:
    return UpdateHandler(telegram, orchestrator)
