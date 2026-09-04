"""The booking conversation end to end, through the real orchestrator.

Reference: Friday 2026-09-04 11:30 IST. 2026-09-07 is a Monday.

These drive the orchestrator rather than the handler directly, because the thing worth
testing is that state precedence, stage transitions and the calendar all agree. A test
that called the handler would pass even if the orchestrator never routed to it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.handlers.booking import (
    ASK_FOR_EMAIL,
    BOOKING_CANCELLED,
    CALENDAR_TROUBLE,
    EMAIL_NOT_VALID,
)
from app.agent.orchestrator import Orchestrator
from app.domain.business import CLINIC_ADDRESS
from app.services.calendar_service import CalendarError

from tests.conftest import (
    TZ,
    FakeCalendarService,
    FakeEmailService,
    FakeGroqClient,
    classification,
    resolution,
)

CHAT_ID = 555
MON_2PM = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, tzinfo=TZ)


async def request_monday_2pm(orchestrator: Orchestrator, llm: FakeGroqClient) -> str:
    """Drive the first turn: classify, resolve, propose."""
    llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    llm.queue(resolution("2026-09-07", "14:00"))
    reply = await orchestrator.handle(
        CHAT_ID, {"text": "can I book Monday at 2pm?", "from": {"first_name": "Dev"}}
    )
    return reply.text


# ------------------------------------------------------------------ happy path

async def test_a_whole_booking_from_request_to_created_event(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    assert "Shall I book it?" in await request_monday_2pm(orchestrator, fake_llm)
    assert (await store.get(CHAT_ID)).stage == "awaiting_slot_confirmation"

    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})
    assert reply.text == ASK_FOR_EMAIL
    assert (await store.get(CHAT_ID)).stage == "awaiting_email"

    reply = await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})
    assert "Shall I go ahead" in reply.text
    assert (await store.get(CHAT_ID)).stage == "awaiting_final_confirmation"

    reply = await orchestrator.handle(CHAT_ID, {"text": "yes please"})
    assert "Booked." in reply.text
    assert "Monday 7 September at 2:00 PM" in reply.text
    assert CLINIC_ADDRESS in reply.text

    assert len(calendar.created) == 1
    assert calendar.created[0]["start"] == MON_2PM
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_the_confirmation_flow_costs_no_model_calls(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    """Only the first turn classifies and resolves. "yes" is never sent to a model."""
    await request_monday_2pm(orchestrator, fake_llm)
    calls_after_request = fake_llm.call_count

    for message in ("yes", "dev@example.com", "yes"):
        await orchestrator.handle(CHAT_ID, {"text": message})

    assert calls_after_request == 2
    assert fake_llm.call_count == 2


async def test_the_patient_name_comes_from_telegram_not_a_question(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    for message in ("yes", "dev@example.com", "yes"):
        await orchestrator.handle(CHAT_ID, {"text": message})

    assert "Dev" in calendar.created[0]["summary"]


async def test_the_patient_email_is_recorded_but_never_sent_as_an_attendee(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    """A service account without Domain-Wide Delegation gets HTTP 403 for attendees,
    which fails the whole booking rather than degrading."""
    await request_monday_2pm(orchestrator, fake_llm)
    for message in ("yes", "dev@example.com", "yes"):
        await orchestrator.handle(CHAT_ID, {"text": message})

    assert calendar.created[0]["attendee_email"] == "dev@example.com"


# --------------------------------------------------------------- declining

async def test_declining_the_slot_books_nothing(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)

    reply = await orchestrator.handle(CHAT_ID, {"text": "no"})

    assert reply.text == BOOKING_CANCELLED
    assert calendar.created == []
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_declining_at_the_final_step_books_nothing(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    reply = await orchestrator.handle(CHAT_ID, {"text": "no"})

    assert reply.text == BOOKING_CANCELLED
    assert calendar.created == []
    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------------------------- email

@pytest.mark.parametrize(
    "bad", ["not an email", "dev@", "@example.com", "dev example.com", "dev@@x.com"]
)
async def test_an_invalid_email_is_re_asked_not_accepted(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store, bad: str
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})

    reply = await orchestrator.handle(CHAT_ID, {"text": bad})

    assert reply.text == EMAIL_NOT_VALID
    assert (await store.get(CHAT_ID)).stage == "awaiting_email"
    assert (await store.get(CHAT_ID)).patient_email is None


async def test_an_email_inside_a_sentence_is_found(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})

    await orchestrator.handle(CHAT_ID, {"text": "sure, it's Dev.Shoji+bot@Example.COM"})

    # email-validator normalises the domain; the local part is left alone.
    assert (await store.get(CHAT_ID)).patient_email == "Dev.Shoji+bot@example.com"


# --------------------------------------------------- availability and races

async def test_a_booked_slot_is_skipped_for_the_next_free_one(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    calendar.busy = {at(14, 0), at(14, 30)}

    reply = await request_monday_2pm(orchestrator, fake_llm)

    assert "3:00 PM" in reply.text if hasattr(reply, "text") else "3:00 PM" in reply
    assert "at or after" in reply


async def test_a_fully_booked_day_is_refused_without_rolling_over(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    from app.domain.business import slot_starts
    from datetime import date

    orchestrator._calendar.busy = set(slot_starts(date(2026, 9, 7), TZ))  # type: ignore[attr-defined]

    reply = await request_monday_2pm(orchestrator, fake_llm)

    assert "fully booked" in reply
    assert "Tuesday 8 September" in reply  # suggested, not booked
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_slot_taken_while_the_user_types_is_caught_before_booking(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    """Booking on the earlier answer is how two patients land in one slot."""
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    calendar.taken_on_next_check = {MON_2PM}

    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert "was taken while we were talking" in reply.text
    assert calendar.created == []
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_availability_is_rechecked_at_confirmation(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})
    await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert calendar.calls.count("is_free") == 1
    assert calendar.calls.index("is_free") < calendar.calls.index("create_event")


# ------------------------------------------------------- unrecognised replies

async def test_a_new_time_mid_flow_is_reclassified_not_rejected(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """"actually can we do 4pm?" is a changed request, not a failed yes/no."""
    await request_monday_2pm(orchestrator, fake_llm)

    fake_llm.queue(classification("booking", raw_datetime_text="4pm Monday"))
    fake_llm.queue(resolution("2026-09-07", "16:00"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "actually can we do 4pm?"})

    assert "4:00 PM" in reply.text
    assert reply.intent == "booking"
    assert (await store.get(CHAT_ID)).proposed_start == at(16, 0)


async def test_an_unrelated_question_mid_flow_is_answered(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    fake_llm.queue(classification("faq", faq_topic="parking"))

    reply = await orchestrator.handle(CHAT_ID, {"text": "wait, is there parking?"})

    assert "parking" in reply.text.lower()
    assert reply.intent == "faq"


# ----------------------------------------------------------- calendar failures

async def test_an_unreachable_calendar_degrades_gracefully_when_proposing(
    store, fake_llm: FakeGroqClient
) -> None:
    from app.agent.resolver import DatetimeResolver
    from app.agent.router import IntentRouter
    from tests.conftest import FIXED_NOW

    broken = FakeCalendarService(error=CalendarError("network down"))
    subject = Orchestrator(
        router=IntentRouter(fake_llm),
        store=store,
        resolver=DatetimeResolver(fake_llm),
        calendar=broken,
        email=FakeEmailService(),
        tz=TZ,
        now=lambda: FIXED_NOW,
    )

    reply = await request_monday_2pm(subject, fake_llm)

    assert reply == CALENDAR_TROUBLE
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_create_failure_keeps_the_user_in_the_flow(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    """The slot is still theirs to confirm: dropping to idle would lose their place."""
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    calendar.error = CalendarError("500 from Google")
    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert reply.text == CALENDAR_TROUBLE
    assert (await store.get(CHAT_ID)).stage == "awaiting_final_confirmation"


# ------------------------------------------------------------ confirmation email

async def test_a_confirmation_email_is_sent_after_the_event_is_created(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert len(email.sent) == 1
    assert email.sent[0]["to_email"] == "dev@example.com"
    assert email.sent[0]["appointment_start"] == MON_2PM
    assert email.sent[0]["patient_name"] == "Dev"
    assert "sent a confirmation" in reply.text


async def test_a_failed_email_does_not_undo_a_real_appointment(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """The event exists. Rolling it back over a mail server would lose a real booking."""
    from app.services.email_service import EmailError

    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    email.error = EmailError("smtp down")
    reply = await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert len(calendar.created) == 1              # the appointment stands
    assert "Booked." in reply.text
    assert "couldn't send the confirmation email" in reply.text
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_the_email_is_sent_only_after_the_event_exists(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Confirming an appointment that then fails to save is worse than no email."""
    from app.services.calendar_service import CalendarError

    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    calendar.error = CalendarError("500 from Google")
    await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert email.sent == []


async def test_a_cancelled_booking_sends_no_email(
    orchestrator: Orchestrator, fake_llm: FakeGroqClient, email: FakeEmailService
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    await orchestrator.handle(CHAT_ID, {"text": "no"})

    assert email.sent == []


async def test_a_slot_lost_to_a_race_sends_no_email(
    orchestrator: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await request_monday_2pm(orchestrator, fake_llm)
    await orchestrator.handle(CHAT_ID, {"text": "yes"})
    await orchestrator.handle(CHAT_ID, {"text": "dev@example.com"})

    calendar.taken_on_next_check = {MON_2PM}
    await orchestrator.handle(CHAT_ID, {"text": "yes"})

    assert email.sent == []
