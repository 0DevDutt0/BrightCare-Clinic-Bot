"""Rescheduling end to end, and the disambiguation that decides which flow runs.

Reference: Friday 2026-09-04 11:30 IST. 2026-09-07 is a Monday.

Rescheduling is a cancellation with a booking attached, so most of what protects it is
already proven in ``test_changes_flow.py`` -- the one-time code, the lookup, the escape
hatch. What is tested here is what only rescheduling has: the order of the two calendar
writes, what happens when one of them fails, and the fact that "I can't make Monday" is
a question rather than an instruction.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.handlers.changes import (
    ASK_FOR_NEW_TIME,
    CHANGE_INTENT_UNCLEAR,
    RESCHEDULE_CONFIRM_UNCLEAR,
    RESCHEDULE_DECLINED,
    RESCHEDULE_TROUBLE_AFTER_CODE,
    read_change_mode,
)
from app.agent.orchestrator import CHANGE_ABANDONED, Orchestrator
from app.services.calendar_service import CalendarError
from app.services.email_service import EmailError

from tests.conftest import (
    TZ,
    FakeCalendarService,
    FakeEmailService,
    FakeGroqClient,
    classification,
    resolution,
)

CHAT_ID = 555
EMAIL = "dev@example.com"
MON_2PM = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
WED_11AM = datetime(2026, 9, 9, 11, 0, tzinfo=TZ)


async def say(subject: Orchestrator, text: str) -> str:
    return (await subject.handle(CHAT_ID, {"text": text})).text


async def open_with(subject: Orchestrator, llm: FakeGroqClient, intent: str) -> str:
    llm.queue(classification(intent))
    reply = await subject.handle(
        CHAT_ID, {"text": "about my appointment", "from": {"first_name": "Dev"}}
    )
    return reply.text


async def reach_new_time_prompt(
    subject: Orchestrator, llm: FakeGroqClient, calendar: FakeCalendarService
) -> str:
    """Seed one appointment and get as far as "what day and time instead?"."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, llm, "reschedule")
    return await say(subject, EMAIL)


async def propose_wednesday(
    subject: Orchestrator, llm: FakeGroqClient, day: str = "2026-09-09", clock: str = "11:00"
) -> str:
    llm.queue(resolution(day, clock))
    return await say(subject, "Wednesday at 11am")


async def reach_the_code_prompt(
    subject: Orchestrator, llm: FakeGroqClient, calendar: FakeCalendarService
) -> str:
    await reach_new_time_prompt(subject, llm, calendar)
    await propose_wednesday(subject, llm)
    return await say(subject, "yes")


# ------------------------------------------------------------------- happy path

async def test_a_whole_reschedule_books_the_new_time_and_releases_the_old(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    found = await reach_new_time_prompt(subject, fake_llm, calendar)
    assert "Monday 7 September at 2:00 PM" in found
    assert "What day and time" in found
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"

    offered = await propose_wednesday(subject, fake_llm)
    assert "Wednesday 9 September at 11:00 AM is free" in offered
    assert "move your appointment from Monday 7 September at 2:00 PM" in offered

    sent = await say(subject, "yes")
    assert "6-digit code" in sent
    assert calendar.created == []          # nothing has happened yet

    done = await say(subject, email.last_code)

    assert "moved from Monday 7 September at 2:00 PM" in done
    assert "to Wednesday 9 September at 11:00 AM" in done
    assert [item["start"] for item in calendar.created] == [WED_11AM]
    assert calendar.cancelled == ["evt-1"]
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_the_new_time_is_booked_before_the_old_is_released(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The order is the safety property. Cancelling first and then failing to book
    leaves a patient with no appointment and no warning."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert calendar.calls.index("create_event") < calendar.calls.index("cancel_event")


async def test_the_new_slot_is_rechecked_immediately_before_booking(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert calendar.calls.index("is_free") < calendar.calls.index("create_event")


async def test_the_old_slot_is_free_and_the_new_one_taken(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert MON_2PM not in calendar.busy
    assert WED_11AM in calendar.busy


async def test_a_reschedule_costs_two_model_calls(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """One to classify, one to resolve the new time. The code, the address and the
    yes/no are all free."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    await say(subject, email.last_code)

    assert fake_llm.call_count == 2


async def test_resolving_the_new_time_is_reported_as_a_model_call(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    """The one continuation in the app that is not free, so it must not be logged or
    tested as though it were."""
    await reach_new_time_prompt(subject, fake_llm, calendar)
    fake_llm.queue(resolution("2026-09-09", "11:00"))

    reply = await subject.handle(CHAT_ID, {"text": "Wednesday at 11am"})

    assert reply.used_llm is True
    assert reply.intent == "continuation.awaiting_reschedule_time"


async def test_one_email_describes_the_move(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Not a cancellation followed by a confirmation: two mails for one action race in
    an inbox and, arriving out of order, read as "cancelled" last."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert len(email.reschedules) == 1
    assert email.cancellations == []
    assert email.sent == []
    assert email.reschedules[0]["previous_start"] == MON_2PM
    assert email.reschedules[0]["appointment_start"] == WED_11AM


# ----------------------------------------------------- the patient's own calendar

async def test_the_moved_appointment_keeps_its_calendar_uid(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """A new UID would file the new time as a second entry beside the old one, which is
    exactly the mess the patient asked us to avoid."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert calendar.created[0]["ics_uid"] == "evt-1"
    assert email.reschedules[0]["ics_uid"] == "evt-1"


async def test_each_move_outranks_the_last(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """A client ignores an update whose SEQUENCE has not increased, so a second
    reschedule that reused the first's number would silently not apply."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev", ics_uid="uid-1", ics_sequence=3)
    await open_with(subject, fake_llm, "reschedule")
    await say(subject, EMAIL)
    await propose_wednesday(subject, fake_llm)
    await say(subject, "yes")

    await say(subject, email.last_code)

    assert calendar.created[0]["ics_uid"] == "uid-1"
    assert calendar.created[0]["ics_sequence"] == 4
    assert email.reschedules[0]["ics_sequence"] == 4


async def test_an_appointment_can_be_moved_twice(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The end-to-end version: the UID survives and the sequence keeps climbing."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    await say(subject, email.last_code)

    await open_with(subject, fake_llm, "reschedule")
    await say(subject, EMAIL)
    fake_llm.queue(resolution("2026-09-10", "16:00"))
    await say(subject, "Thursday at 4pm")
    await say(subject, "yes")
    await say(subject, email.last_code)

    assert [item["ics_uid"] for item in calendar.created] == ["evt-1", "evt-1"]
    assert [item["ics_sequence"] for item in calendar.created] == [1, 2]


async def test_a_moved_appointment_can_then_be_cancelled(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The new event has to be tagged like any other, or the next lookup misses it."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    await say(subject, email.last_code)

    await open_with(subject, fake_llm, "cancel")
    found = await say(subject, EMAIL)
    assert "Wednesday 9 September at 11:00 AM" in found

    await say(subject, "yes")
    await say(subject, email.last_code)

    assert calendar.cancelled == ["evt-1", "evt-2"]
    assert WED_11AM not in calendar.busy


# ------------------------------------------------- picking between cancel and move

@pytest.mark.parametrize(
    "text, expected",
    [
        ("reschedule", "reschedule"),
        ("move it", "reschedule"),
        ("change it please", "reschedule"),
        ("a different time", "reschedule"),
        ("cancel", "cancel"),
        ("cancel it", "cancel"),
        ("just cancel please", "cancel"),
        # Both named, so neither is chosen: picking one from a sentence that named the
        # other is how a booking the user meant to keep gets destroyed.
        ("don't cancel it, just move it", None),
        ("cancel or reschedule?", None),
        # Neither named.
        ("yes", None),
        ("I'm not sure", None),
        ("", None),
    ],
)
def test_reading_which_one_they_want(text: str, expected: str | None) -> None:
    assert read_change_mode(text) == expected


async def test_an_unclear_request_asks_instead_of_guessing(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """"I can't make Monday" is a problem with an appointment, not an instruction about
    it. Guessing either destroys a booking they meant to keep or leaves one they meant
    to drop."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, fake_llm, "change_appointment")

    reply = await say(subject, EMAIL)

    assert "Monday 7 September at 2:00 PM" in reply
    assert "move it to another time, or cancel it" in reply
    assert (await store.get(CHAT_ID)).stage == "awaiting_change_intent"
    assert email.codes == []
    assert calendar.cancelled == []


async def test_answering_reschedule_asks_for_a_new_time(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, fake_llm, "change_appointment")
    await say(subject, EMAIL)

    reply = await say(subject, "reschedule")

    assert reply == ASK_FOR_NEW_TIME
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"


async def test_answering_cancel_is_an_answer_not_an_escape(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    """The bot just asked "move it, or cancel it?". Letting the escape hatch swallow
    "cancel" would abandon the flow and reply that nothing was cancelled -- to someone
    in the middle of cancelling."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, fake_llm, "change_appointment")
    await say(subject, EMAIL)

    reply = await say(subject, "cancel")

    assert reply != CHANGE_ABANDONED
    assert "Shall I go ahead?" in reply
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_confirmation"


@pytest.mark.parametrize("word", ["stop", "nevermind", "quit"])
async def test_the_other_escape_words_still_work_at_that_prompt(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
    word: str,
) -> None:
    """Only "cancel" is claimed by the question; the rest of the hatch is untouched."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, fake_llm, "change_appointment")
    await say(subject, EMAIL)

    assert await say(subject, word) == CHANGE_ABANDONED
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_an_unreadable_answer_re_asks(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await open_with(subject, fake_llm, "change_appointment")
    await say(subject, EMAIL)

    assert await say(subject, "hmm") == CHANGE_INTENT_UNCLEAR
    assert (await store.get(CHAT_ID)).stage == "awaiting_change_intent"


# ------------------------------------------------------------ choosing a new time

async def test_asking_for_the_time_you_already_have_says_so(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    """Their own appointment blocks its own slot, so the naive answer is the slot after
    it -- technically true, and baffling."""
    await reach_new_time_prompt(subject, fake_llm, calendar)
    fake_llm.queue(resolution("2026-09-07", "14:00"))

    reply = await say(subject, "Monday at 2pm")

    assert "already when your appointment is" in reply
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"
    assert calendar.created == []


async def test_a_refused_time_stays_at_the_prompt(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    """A Saturday is refused in booking's own words, and the flow waits for another."""
    await reach_new_time_prompt(subject, fake_llm, calendar)
    fake_llm.queue(resolution("2026-09-12", "10:00"))  # a Saturday

    reply = await say(subject, "Saturday at 10")

    assert "closed that day" in reply
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"


async def test_a_busy_day_offers_the_next_free_slot(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    calendar.busy.add(WED_11AM)
    await reach_new_time_prompt(subject, fake_llm, calendar)

    reply = await propose_wednesday(subject, fake_llm)

    assert "11:30 AM" in reply
    assert "at or after" in reply


async def test_declining_the_offered_slot_asks_again_rather_than_leaving(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    """They still want to move it; they just do not want *this* slot."""
    await reach_new_time_prompt(subject, fake_llm, calendar)
    await propose_wednesday(subject, fake_llm)

    reply = await say(subject, "no")

    assert reply == RESCHEDULE_DECLINED
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"
    assert (await store.get(CHAT_ID)).reschedule_start is None


async def test_an_unclear_answer_to_the_offer_re_asks(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    await reach_new_time_prompt(subject, fake_llm, calendar)
    await propose_wednesday(subject, fake_llm)

    assert await say(subject, "cancel it") == RESCHEDULE_CONFIRM_UNCLEAR
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_confirmation"


# ---------------------------------------------------------------- failure paths

async def test_a_failed_booking_leaves_the_original_untouched(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Nothing has changed, so the reply says so and the same code retries it."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    calendar.error = CalendarError("500 from Google")

    reply = await say(subject, email.last_code)

    assert reply == RESCHEDULE_TROUBLE_AFTER_CODE
    assert calendar.cancelled == []
    assert MON_2PM in calendar.busy
    assert (await store.get(CHAT_ID)).stage == "awaiting_change_code"


async def test_the_same_code_retries_after_a_calendar_failure(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code
    calendar.error = CalendarError("500 from Google")
    await say(subject, code)

    calendar.error = None
    reply = await say(subject, code)

    assert "moved from" in reply
    assert len(email.codes) == 1              # no second code was needed


async def test_a_new_slot_lost_to_a_race_does_not_lose_the_appointment(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Taken while they were fetching the code out of their inbox. The original must
    still be standing, and they must not have to prove themselves again."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    calendar.taken_on_next_check = {WED_11AM}

    reply = await say(subject, email.last_code)

    assert "was taken while we were talking" in reply
    assert "still Monday 7 September at 2:00 PM" in reply
    assert calendar.created == []
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "awaiting_reschedule_time"


async def test_a_second_attempt_after_that_race_needs_no_new_code(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """They already proved who they are. Making them do it again for our race is rude,
    and no safer -- same flow, same appointment."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    calendar.taken_on_next_check = {WED_11AM}
    await say(subject, email.last_code)

    fake_llm.queue(resolution("2026-09-10", "16:00"))
    await say(subject, "Thursday at 4pm")
    reply = await say(subject, "yes")

    assert "moved from" in reply                 # went straight through
    assert len(email.codes) == 1
    assert calendar.cancelled == ["evt-1"]


async def test_a_failed_release_reports_two_appointments(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """The half-done case this ordering deliberately accepts. Saying nothing would leave
    the patient turning up at the wrong time, or a slot blocked for someone else."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    async def refuse(event_id: str) -> None:
        raise CalendarError("500 from Google")

    calendar.cancel_event = refuse  # type: ignore[method-assign]
    reply = await say(subject, email.last_code)

    assert "booked for Wednesday 9 September at 11:00 AM" in reply
    assert "couldn't release the earlier one" in reply
    assert "call the clinic" in reply
    assert len(calendar.created) == 1
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_failed_email_does_not_undo_a_real_move(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    email.reschedule_error = EmailError("smtp down")

    reply = await say(subject, email.last_code)

    assert len(calendar.created) == 1
    assert calendar.cancelled == ["evt-1"]
    assert "couldn't send the confirmation email" in reply
    assert "change itself went through" in reply
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_backing_out_mid_reschedule_changes_nothing(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    reply = await say(subject, "stop")

    assert reply == CHANGE_ABANDONED
    assert calendar.created == []
    assert calendar.cancelled == []
    assert MON_2PM in calendar.busy
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_wrong_code_moves_nothing(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    wrong = "000000" if email.last_code != "000000" else "111111"

    reply = await say(subject, wrong)

    assert "isn't right" in reply
    assert calendar.created == []
    assert calendar.cancelled == []
