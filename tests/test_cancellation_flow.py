"""The cancellation conversation end to end, through the real orchestrator.

Reference: Friday 2026-09-04 11:30 IST. 2026-09-07 is a Monday.

These drive the orchestrator rather than the handler, for the same reason the booking
tests do: the thing worth proving is that stage precedence, the one-time code and the
calendar all agree. A handler-level test would pass even if the orchestrator never
routed a message to it.

The code is only ever read back off the fake mail service. That is not convenience --
it is the only place it exists, which is the property under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent.handlers.cancellation import (
    ALREADY_GONE,
    CANCEL_EMAIL_NOT_VALID,
    CANCEL_TROUBLE_AFTER_CODE,
    CANCELLATION_DECLINED,
    CHOICE_UNCLEAR,
    CODE_EMAIL_FAILED,
    CODE_EXPIRED,
    CODE_NOT_UNDERSTOOD,
    CONFIRM_UNCLEAR,
    LOOKUP_TROUBLE,
    TOO_MANY_ATTEMPTS,
    TOO_MANY_CODES,
    start_cancellation,
)
from app.agent.orchestrator import CANCELLATION_ABANDONED, Orchestrator
from app.agent.resolver import DatetimeResolver
from app.agent.router import IntentRouter
from app.domain.otp import MAX_ATTEMPTS, MAX_SENDS, OTP_TTL
from app.services.calendar_service import CalendarError
from app.services.email_service import EmailError
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
EMAIL = "dev@example.com"
MON_2PM = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
TUE_10AM = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)


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


async def say(subject: Orchestrator, text: str) -> str:
    return (await subject.handle(CHAT_ID, {"text": text})).text


async def ask_to_cancel(subject: Orchestrator, llm: FakeGroqClient) -> str:
    llm.queue(classification("cancel"))
    reply = await subject.handle(
        CHAT_ID,
        {"text": "I need to cancel my appointment", "from": {"first_name": "Dev"}},
    )
    return reply.text


async def reach_the_code_prompt(
    subject: Orchestrator, llm: FakeGroqClient, calendar: FakeCalendarService
) -> str:
    """Seed one appointment and drive as far as "enter the code"."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, llm)
    await say(subject, EMAIL)
    return await say(subject, "yes")


# ------------------------------------------------------------------ happy path

async def test_a_whole_cancellation_from_request_to_deleted_event(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")

    assert "email address" in await ask_to_cancel(subject, fake_llm)
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_email"

    found = await say(subject, EMAIL)
    assert "Monday 7 September at 2:00 PM" in found
    assert "Shall I cancel it?" in found
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_confirmation"

    sent = await say(subject, "yes")
    assert "6-digit code" in sent
    assert EMAIL in sent
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_code"
    assert len(email.codes) == 1
    assert calendar.cancelled == []          # nothing has happened yet

    done = await say(subject, email.last_code)
    assert "cancelled" in done
    assert "Monday 7 September at 2:00 PM" in done

    assert calendar.cancelled == ["evt-1"]
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_the_slot_is_free_again_afterwards(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """A cancellation that does not return the slot has not cancelled anything useful."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    assert MON_2PM in calendar.busy

    await say(subject, email.last_code)

    assert MON_2PM not in calendar.busy


async def test_the_whole_flow_costs_one_model_call(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Only the first message is classified. No stage after it is a judgement call."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    await say(subject, email.last_code)

    assert fake_llm.call_count == 1


async def test_a_receipt_is_emailed_with_the_event_id(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The id becomes the .ics UID, which is what lets the receipt retract the booking."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, email.last_code)

    assert len(email.cancellations) == 1
    assert email.cancellations[0]["to_email"] == EMAIL
    assert email.cancellations[0]["appointment_start"] == MON_2PM
    assert email.cancellations[0]["event_id"] == "evt-1"


async def test_the_code_email_names_the_appointment_it_authorises(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """So the real owner can tell, from the mail alone, what is about to be cancelled."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    assert email.codes[0]["appointment_start"] == MON_2PM
    assert email.codes[0]["to_email"] == EMAIL
    assert email.codes[0]["patient_name"] == "Dev"


# ------------------------------------------------------------- the code itself

async def test_the_code_never_appears_in_a_reply_or_in_state(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Telegram history is not a secure channel, and neither is a state dump."""
    prompt = await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code

    state = await store.get(CHAT_ID)

    assert code not in prompt
    assert code not in state.model_dump_json()
    assert all(code not in turn.text for turn in state.history)


async def test_a_wrong_code_cancels_nothing_and_keeps_the_flow(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    wrong = "000000" if email.last_code != "000000" else "111111"

    reply = await say(subject, wrong)

    assert "isn't right" in reply
    assert "2 attempts left" in reply
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_code"


async def test_the_right_code_still_works_after_a_wrong_one(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code
    await say(subject, "000000" if code != "000000" else "111111")

    reply = await say(subject, code)

    assert "cancelled" in reply
    assert calendar.cancelled == ["evt-1"]


async def test_three_wrong_codes_end_the_flow_with_nothing_cancelled(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Three guesses against 10^6 is what makes the six digits enough."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(MAX_ATTEMPTS - 1):
        await say(subject, wrong)
    reply = await say(subject, wrong)

    assert reply == TOO_MANY_ATTEMPTS
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_dead_code_cannot_be_revived_by_starting_over(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """reset_flow drops the challenge, so the burned code is not waiting in the next one."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_ATTEMPTS):
        await say(subject, wrong)

    # Start again; the old code belongs to a challenge that no longer exists.
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)
    await say(subject, "yes")
    reply = await say(subject, code)

    assert "isn't right" in reply
    assert calendar.cancelled == []


async def test_a_reply_that_is_not_a_code_does_not_spend_an_attempt(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Punishing a typo or a question with one of three attempts is punishing noise."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    for noise in ("what?", "12345", "hang on"):
        assert await say(subject, noise) == CODE_NOT_UNDERSTOOD

    reply = await say(subject, email.last_code)
    assert "cancelled" in reply


async def test_an_expired_code_ends_the_flow_with_nothing_cancelled(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    clock: Clock,
    store,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code

    clock.advance(OTP_TTL + timedelta(seconds=1))
    reply = await say(subject, code)

    assert reply == CODE_EXPIRED
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------------------------- resending

async def test_resend_issues_a_new_code(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)
    first = email.last_code

    await say(subject, "resend")

    assert len(email.codes) == 2
    assert await say(subject, email.last_code) is not None
    assert calendar.cancelled == ["evt-1"]
    assert first != email.last_code or len(email.codes) == 2


async def test_the_old_code_dies_when_a_new_one_is_sent(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Two live codes would double the guessing surface for no benefit."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    first = email.last_code
    await say(subject, "resend")
    if email.last_code == first:  # a 1-in-10^6 collision, not a behaviour
        pytest.skip("the resent code happened to match the first")

    reply = await say(subject, first)

    assert "isn't right" in reply
    assert calendar.cancelled == []


@pytest.mark.parametrize("phrase", ["resend", "Resend", "send again", "I didn't get it"])
async def test_asking_again_never_spends_an_attempt(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    phrase: str,
) -> None:
    """Without this, the word "resend" reads as a wrong code and burns a third of the
    budget for asking a reasonable question."""
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, phrase)
    reply = await say(subject, "000000" if email.last_code != "000000" else "111111")

    assert "2 attempts left" in reply


async def test_the_send_budget_is_capped(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    for _ in range(MAX_SENDS - 1):
        await say(subject, "resend")
    reply = await say(subject, "resend")

    assert reply == TOO_MANY_CODES
    assert len(email.codes) == MAX_SENDS
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "idle"


# --------------------------------------------------------------- finding it

async def test_an_address_with_no_appointment_keeps_the_prompt_open(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """The reply offers to look again, so the flow has to still be there to do it.
    Someone with a work address and a personal one should not have to start over."""
    calendar.add_appointment("evt-1", MON_2PM, "someone@else.com", "Someone")
    await ask_to_cancel(subject, fake_llm)

    reply = await say(subject, EMAIL)

    assert "couldn't find" in reply
    assert email.codes == []                 # no mail to an address with no booking
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_email"


async def test_a_second_address_is_looked_up_without_starting_over(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, "dev@work.example", "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    reply = await say(subject, "dev@work.example")

    assert "Monday 7 September at 2:00 PM" in reply
    assert fake_llm.call_count == 1


async def test_the_lookup_prompt_does_not_stay_open_forever(
    subject: Orchestrator, fake_llm: FakeGroqClient, store
) -> None:
    """An open prompt is a free calendar query per message, and free is what makes
    address probing worth attempting. Restarting costs a classification."""
    from app.agent.handlers.cancellation import MAX_LOOKUPS, TOO_MANY_LOOKUPS

    await ask_to_cancel(subject, fake_llm)

    for index in range(MAX_LOOKUPS - 1):
        await say(subject, f"nobody{index}@example.org")
    reply = await say(subject, "nobody-last@example.org")

    assert reply == TOO_MANY_LOOKUPS
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_no_code_is_sent_to_an_address_that_booked_nothing(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The bot must not become a way to make mail arrive at an arbitrary address."""
    await ask_to_cancel(subject, fake_llm)

    await say(subject, "a.stranger@example.org")

    assert email.codes == []


@pytest.mark.parametrize("bad", ["not an email", "dev@", "@example.com", "dev example"])
async def test_an_invalid_address_is_re_asked_not_rerouted(
    subject: Orchestrator, fake_llm: FakeGroqClient, store, bad: str
) -> None:
    await ask_to_cancel(subject, fake_llm)

    reply = await say(subject, bad)

    assert reply == CANCEL_EMAIL_NOT_VALID
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_email"


async def test_the_lookup_matches_the_address_exactly(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """Offering a near-match for cancellation is the one mistake this must not make."""
    calendar.add_appointment("evt-1", MON_2PM, "dev@example.com", "Dev")
    await ask_to_cancel(subject, fake_llm)

    reply = await say(subject, "dev@example.com.au")

    assert "couldn't find" in reply
    assert email.codes == []


async def test_the_address_is_matched_case_insensitively(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, "dev@example.com", "Dev")
    await ask_to_cancel(subject, fake_llm)

    reply = await say(subject, "Dev@Example.com")

    assert "Monday 7 September at 2:00 PM" in reply


async def test_an_unreachable_calendar_ends_the_flow_cleanly(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
) -> None:
    await ask_to_cancel(subject, fake_llm)
    calendar.error = CalendarError("network down")

    reply = await say(subject, EMAIL)

    assert reply == LOOKUP_TROUBLE
    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------------- choosing between several

async def test_several_appointments_are_listed_and_numbered(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    calendar.add_appointment("evt-2", TUE_10AM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)

    reply = await say(subject, EMAIL)

    assert "1. Monday 7 September at 2:00 PM" in reply
    assert "2. Tuesday 8 September at 10:00 AM" in reply
    assert email.codes == []                 # nothing is sent until one is chosen
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_choice"


async def test_picking_a_number_cancels_that_one_and_only_that_one(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    calendar.add_appointment("evt-2", TUE_10AM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    offered = await say(subject, "2")
    assert "Tuesday 8 September at 10:00 AM" in offered

    await say(subject, "yes")
    await say(subject, email.last_code)

    assert calendar.cancelled == ["evt-2"]
    assert "evt-1" in calendar.appointments


@pytest.mark.parametrize("choice", ["1", "1st", "number 1", "the 1"])
async def test_a_number_is_read_however_it_is_phrased(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    choice: str,
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    calendar.add_appointment("evt-2", TUE_10AM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    assert "Monday 7 September at 2:00 PM" in await say(subject, choice)


@pytest.mark.parametrize(
    "bad",
    [
        "3",                                    # out of range
        "0",                                    # there is no zeroth
        "either",                               # not a number at all
        "dev123@example.com",                   # an address, retyped
        "I have 2 appointments which is which",  # a question that happens to contain one
    ],
)
async def test_an_unusable_choice_re_asks(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    store,
    bad: str,
) -> None:
    """None of these may be mined for a stray number and read as a pick -- doing so
    would offer the wrong appointment, and the user is one "yes" from cancelling it."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    calendar.add_appointment("evt-2", TUE_10AM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    assert await say(subject, bad) == CHOICE_UNCLEAR
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_choice"


# ---------------------------------------------------------------- backing out

async def test_declining_at_the_confirmation_sends_no_code(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    reply = await say(subject, "no")

    assert reply == CANCELLATION_DECLINED
    assert email.codes == []
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "idle"


@pytest.mark.parametrize("ambiguous", ["cancel it", "cancel that"])
async def test_cancel_at_the_confirmation_prompt_is_neither_yes_nor_no(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
    ambiguous: str,
) -> None:
    """The trap this flow is built around. Booking reads "cancel it" as a refusal; here
    it could mean either "cancel the appointment" or "cancel this conversation", so it
    must decide nothing and ask again."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    reply = await say(subject, ambiguous)

    assert reply == CONFIRM_UNCLEAR
    assert email.codes == []
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_confirmation"


async def test_yes_cancel_it_is_a_yes(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The counterpart: neutralising the word must not swallow a plain answer."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    reply = await say(subject, "yes cancel it")

    assert "6-digit code" in reply
    assert len(email.codes) == 1


@pytest.mark.parametrize(
    "stage",
    [
        "awaiting_cancel_email",
        "awaiting_cancel_choice",
        "awaiting_cancel_confirmation",
        "awaiting_cancel_code",
    ],
)
async def test_backing_out_says_the_appointment_is_still_booked(
    subject: Orchestrator, fake_llm: FakeGroqClient, store, stage: str
) -> None:
    """"No problem, I've cleared that" is fine after a booking and reads as "cleared your
    appointment" after this -- the one sentence a patient must not misread."""
    await store.save(ConversationState(chat_id=CHAT_ID, stage=stage))

    reply = await say(subject, "stop")

    assert reply == CANCELLATION_ABANDONED
    assert "still booked" in reply
    assert fake_llm.call_count == 0
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_backing_out_mid_flow_cancels_nothing(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, "cancel")

    assert calendar.cancelled == []
    assert "evt-1" in calendar.appointments


async def test_start_clears_a_cancellation_in_flight(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    await reach_the_code_prompt(subject, fake_llm, calendar)

    await say(subject, "/start")

    state = await store.get(CHAT_ID)
    assert state.stage == "idle"
    assert state.cancel_otp is None
    assert calendar.cancelled == []


# ---------------------------------------------------------------- failure paths

async def test_a_code_that_cannot_be_emailed_ends_the_flow(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Parking someone at a prompt for a code that was never sent is worse than saying so."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)
    email.code_error = EmailError("smtp down")

    reply = await say(subject, "yes")

    assert reply == CODE_EMAIL_FAILED
    assert calendar.cancelled == []
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_an_appointment_already_gone_is_reported_as_such(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """The patient's goal is met either way, so this is news rather than a failure."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    calendar.appointments.pop("evt-1")       # the clinic removed it by hand

    reply = await say(subject, email.last_code)

    assert reply == ALREADY_GONE
    assert (await store.get(CHAT_ID)).stage == "idle"


async def test_a_calendar_failure_after_a_good_code_keeps_the_code_usable(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """They proved who they are. Making them start over -- new code, new mail -- for a
    transient 500 would spend their patience on our problem."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    code = email.last_code
    calendar.error = CalendarError("500 from Google")

    reply = await say(subject, code)
    assert reply == CANCEL_TROUBLE_AFTER_CODE
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_code"

    calendar.error = None
    assert "cancelled" in await say(subject, code)
    assert calendar.cancelled == ["evt-1"]
    assert len(email.codes) == 1             # no second code was needed


async def test_a_failed_receipt_does_not_undo_a_real_cancellation(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Same rule as booking: the calendar is the record, the mail is the courtesy."""
    await reach_the_code_prompt(subject, fake_llm, calendar)
    email.cancellation_error = EmailError("smtp down")

    reply = await say(subject, email.last_code)

    assert calendar.cancelled == ["evt-1"]
    assert "couldn't send the confirmation email" in reply
    assert "cancellation itself went through" in reply
    assert (await store.get(CHAT_ID)).stage == "idle"


# ------------------------------------------------------------------ integration

async def test_an_appointment_booked_in_this_conversation_can_be_cancelled(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
) -> None:
    """The two flows meet at the calendar, which is the only place they should."""
    fake_llm.queue(classification("booking", raw_datetime_text="Monday at 2pm"))
    fake_llm.queue(resolution("2026-09-07", "14:00"))
    await subject.handle(
        CHAT_ID, {"text": "can I book Monday at 2pm?", "from": {"first_name": "Dev"}}
    )
    for message in ("yes", EMAIL, "yes"):
        await say(subject, message)
    assert len(calendar.created) == 1

    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)
    await say(subject, "yes")
    reply = await say(subject, email.last_code)

    assert "cancelled" in reply
    assert calendar.cancelled == ["evt-1"]
    assert MON_2PM not in calendar.busy


async def test_a_cancellation_does_not_inherit_the_booking_address(
    subject: Orchestrator,
    fake_llm: FakeGroqClient,
    calendar: FakeCalendarService,
    email: FakeEmailService,
    store,
) -> None:
    """Reusing patient_email would skip the one step that establishes who is asking."""
    state = ConversationState(chat_id=CHAT_ID, patient_email=EMAIL, patient_name="Dev")
    await store.save(state)
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")

    reply = await ask_to_cancel(subject, fake_llm)

    assert "email address" in reply
    assert (await store.get(CHAT_ID)).stage == "awaiting_cancel_email"
    assert email.codes == []


def test_starting_a_cancellation_clears_whatever_was_in_flight() -> None:
    """Called directly, because today every route into it happens to arrive clean. That
    is a property of the current dispatch, not of this function, and the invariant it
    relies on -- a cancellation never inherits another flow's leftovers -- should hold
    however dispatch is rearranged later."""
    state = ConversationState(
        chat_id=CHAT_ID,
        stage="awaiting_final_confirmation",
        proposed_start=MON_2PM,
        raw_datetime_text="Monday at 2pm",
    )

    start_cancellation(state)

    assert state.stage == "awaiting_cancel_email"
    assert state.proposed_start is None
    assert state.raw_datetime_text is None


async def test_the_cancel_address_never_leaks_into_a_later_booking(
    subject: Orchestrator, fake_llm: FakeGroqClient, calendar: FakeCalendarService, store
) -> None:
    """An unproven claim must not become the address a booking is confirmed to."""
    calendar.add_appointment("evt-1", MON_2PM, EMAIL, "Dev")
    await ask_to_cancel(subject, fake_llm)
    await say(subject, EMAIL)

    await say(subject, "stop")

    state = await store.get(CHAT_ID)
    assert state.cancel_email is None
    assert state.patient_email is None
