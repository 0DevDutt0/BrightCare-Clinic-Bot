"""Shared fixtures.

The Groq client is faked at the ``complete_json`` seam -- the narrowest point that
still exercises the router's parsing, validation and error handling. Faking HTTP
instead would test the SDK; faking the router would skip the logic under test.

Every fake counts its calls, because several requirements are about work *not* done:
a sticker must cost zero model calls, and a mid-flow reply must skip the classifier.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agent.llm import LLMError
from app.agent.orchestrator import Orchestrator
from app.agent.resolver import DatetimeResolver
from app.agent.router import IntentRouter
from app.domain.business import SLOT_DURATION, slot_starts
from app.services.calendar_service import (
    Appointment,
    CalendarService,
    EventNotFound,
)
from app.services.email_service import EmailService
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
        # Existing appointments the change flow can find, keyed by event id.
        self.appointments: dict[str, Appointment] = {}
        self.cancelled: list[str] = []
        self._next_id = 0

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
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> str:
        self._guard("create_event")
        self.busy.add(start)
        self.created.append(
            {
                "start": start,
                "summary": summary,
                "description": description,
                "attendee_email": attendee_email,
                "patient_name": patient_name,
                "ics_uid": ics_uid,
                "ics_sequence": ics_sequence,
            }
        )
        # Skips ids a test seeded with add_appointment. Handing a created event the
        # same id as an existing one made a reschedule cancel the event it had just
        # made, and the fake reported success -- a fake that lies is worse than none.
        self._next_id += 1
        while f"evt-{self._next_id}" in self.appointments:
            self._next_id += 1
        event_id = f"evt-{self._next_id}"
        if attendee_email:
            # So a booking made in one test is findable by the change flow in the next
            # line of the same test, exactly as it would be on a real calendar.
            self.appointments[event_id] = Appointment(
                event_id=event_id,
                start=start,
                summary=summary,
                patient_email=attendee_email,
                patient_name=patient_name,
                ics_uid=ics_uid or event_id,
                ics_sequence=ics_sequence,
            )
        return event_id

    def add_appointment(
        self,
        event_id: str,
        start: datetime,
        patient_email: str,
        patient_name: str | None = "Dev",
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> Appointment:
        """Seed an appointment the way an earlier booking would have left one."""
        appointment = Appointment(
            event_id=event_id,
            start=start,
            summary=f"Appointment - {patient_name or 'patient'}",
            patient_email=patient_email,
            patient_name=patient_name,
            ics_uid=ics_uid or event_id,
            ics_sequence=ics_sequence,
        )
        self.appointments[event_id] = appointment
        self.busy.add(start)
        return appointment

    async def find_upcoming_by_email(
        self, email: str, window_start: datetime, window_end: datetime
    ) -> list[Appointment]:
        self._guard("find_upcoming_by_email")
        wanted = email.strip().lower()
        found = [
            appointment
            for appointment in self.appointments.values()
            if appointment.patient_email.lower() == wanted
            and window_start <= appointment.start < window_end
        ]
        return sorted(found, key=lambda appointment: appointment.start)

    async def cancel_event(self, event_id: str) -> None:
        self._guard("cancel_event")
        appointment = self.appointments.pop(event_id, None)
        if appointment is None:
            raise EventNotFound(f"no such event: {event_id}")
        self.busy.discard(appointment.start)
        self.cancelled.append(event_id)


class FakeEmailService(EmailService):
    """Records mail instead of sending it.

    ``codes`` is the seam the cancellation tests need: the one-time code is deliberately
    unreachable everywhere else -- not in state, not in a log, not in a reply -- so a
    test can only learn it by reading what was "delivered", which is also the only way
    a real user learns it.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.codes: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []
        self.reschedules: list[dict[str, Any]] = []
        # Set independently so a test can break only the code mail, or only the receipt.
        self.code_error: Exception | None = None
        self.cancellation_error: Exception | None = None
        self.reschedule_error: Exception | None = None

    @property
    def last_code(self) -> str:
        assert self.codes, "no cancellation code was sent"
        return str(self.codes[-1]["code"])

    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(
            {
                "to_email": to_email,
                "appointment_start": appointment_start,
                "patient_name": patient_name,
                "ics_uid": ics_uid,
            }
        )

    async def send_cancellation_code(
        self,
        to_email: str,
        code: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        if self.code_error is not None:
            raise self.code_error
        self.codes.append(
            {
                "to_email": to_email,
                "code": code,
                "appointment_start": appointment_start,
                "patient_name": patient_name,
            }
        )

    async def send_cancellation_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> None:
        if self.cancellation_error is not None:
            raise self.cancellation_error
        self.cancellations.append(
            {
                "to_email": to_email,
                "appointment_start": appointment_start,
                "patient_name": patient_name,
                "ics_uid": ics_uid,
                "ics_sequence": ics_sequence,
            }
        )

    async def send_reschedule_confirmation(
        self,
        to_email: str,
        previous_start: datetime,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 1,
    ) -> None:
        if self.reschedule_error is not None:
            raise self.reschedule_error
        self.reschedules.append(
            {
                "to_email": to_email,
                "previous_start": previous_start,
                "appointment_start": appointment_start,
                "patient_name": patient_name,
                "ics_uid": ics_uid,
                "ics_sequence": ics_sequence,
            }
        )


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
def email() -> FakeEmailService:
    return FakeEmailService()


@pytest.fixture
def orchestrator(
    fake_llm: FakeGroqClient,
    store: InMemoryStateStore,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> Orchestrator:
    """Both layers share one fake client, so queued responses are consumed in order:
    the classification first, then the resolution if the booking path reaches it."""
    return Orchestrator(
        router=IntentRouter(fake_llm),
        store=store,
        resolver=DatetimeResolver(fake_llm),
        calendar=calendar,
        email=email,
        tz=TZ,
        now=lambda: FIXED_NOW,
    )


class Clock:
    """A hand-wound clock, so expiry can be tested without sleeping for ten minutes."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, delta: timedelta) -> None:
        self.at += delta


@pytest.fixture
def clock() -> Clock:
    return Clock(FIXED_NOW)


@pytest.fixture
def subject(
    fake_llm: FakeGroqClient,
    store,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    clock: Clock,
) -> Orchestrator:
    return Orchestrator(
        router=IntentRouter(fake_llm),
        store=store,
        resolver=DatetimeResolver(fake_llm),
        calendar=calendar,
        email=email,
        tz=TZ,
        now=clock,
    )


@pytest.fixture
def telegram() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def handler(
    telegram: FakeTelegramClient, orchestrator: Orchestrator
) -> UpdateHandler:
    return UpdateHandler(telegram, orchestrator)
