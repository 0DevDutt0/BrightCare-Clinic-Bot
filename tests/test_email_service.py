"""Confirmation email: message construction, the calendar attachment, delivery errors.

Construction is a pure function, so the part most worth checking -- headers, both body
alternatives, the .ics -- needs no mail server. Only delivery is patched.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest

from app.domain.business import CLINIC_ADDRESS, CLINIC_NAME
from app.domain.otp import MAX_ATTEMPTS
from app.services.email_service import (
    DisabledEmailService,
    EmailError,
    SmtpEmailService,
    build_cancellation_code_message,
    build_cancellation_message,
    build_confirmation_message,
    build_ics,
)

TZ = ZoneInfo("Asia/Kolkata")
MON_2PM = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)


def message(**overrides) -> EmailMessage:
    kwargs = {
        "to_email": "patient@example.com",
        "appointment_start": MON_2PM,
        "from_email": "clinic@example.com",
        "from_name": CLINIC_NAME,
        "patient_name": "Dev",
        **overrides,
    }
    return build_confirmation_message(**kwargs)


def body_of(msg: EmailMessage, subtype: str) -> str:
    for part in msg.walk():
        if part.get_content_type() == f"text/{subtype}":
            return part.get_content()
    raise AssertionError(f"no text/{subtype} part")


# ------------------------------------------------------------------- headers

def test_headers_are_addressed_and_named() -> None:
    msg = message()

    assert msg["To"] == "patient@example.com"
    assert msg["From"] == f"{CLINIC_NAME} <clinic@example.com>"
    assert "Monday 7 September at 2:00 PM" in msg["Subject"]
    assert msg["Date"]


def test_the_subject_names_the_clinic_and_the_time() -> None:
    """A confirmation that does not say when is a confirmation of nothing."""
    assert CLINIC_NAME in message()["Subject"]


# --------------------------------------------------------------------- bodies

def test_the_plain_text_body_carries_every_detail() -> None:
    text = body_of(message(), "plain")

    assert "Dev" in text
    assert "Monday 7 September at 2:00 PM" in text
    assert CLINIC_ADDRESS in text
    assert "30 minutes" in text


def test_an_html_alternative_is_offered() -> None:
    html = body_of(message(), "html")

    assert "Monday 7 September at 2:00 PM" in html
    assert CLINIC_ADDRESS in html


def test_a_missing_name_degrades_to_a_plain_greeting() -> None:
    text = body_of(message(patient_name=None), "plain")

    assert text.startswith("Hello,")


def test_a_naive_appointment_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        message(appointment_start=datetime(2026, 9, 7, 14, 0))


# ------------------------------------------------------------ ics attachment

def test_a_calendar_file_is_attached() -> None:
    """The only route the appointment has into the patient's own calendar, since a
    service account cannot add them as an attendee."""
    attachments = [p for p in message().walk()
                   if p.get_filename() == "appointment.ics"]

    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "text/calendar"


def test_the_ics_times_are_utc_and_one_slot_apart() -> None:
    ics = build_ics(MON_2PM)

    # 14:00 IST is 08:30 UTC; the slot runs 30 minutes.
    assert "DTSTART:20260907T083000Z" in ics
    assert "DTEND:20260907T090000Z" in ics


def test_the_ics_publishes_rather_than_invites() -> None:
    """REQUEST would ask the patient to accept an invitation nobody can send."""
    ics = build_ics(MON_2PM)

    assert "METHOD:PUBLISH" in ics
    assert "METHOD:REQUEST" not in ics


def test_the_ics_is_crlf_terminated_as_rfc5545_requires() -> None:
    ics = build_ics(MON_2PM)

    assert ics.endswith("\r\n")
    assert "\r\n" in ics
    assert ics.replace("\r\n", "").find("\n") == -1  # no bare newlines


def test_the_ics_carries_the_clinic_details() -> None:
    ics = build_ics(MON_2PM)

    assert f"SUMMARY:Appointment at {CLINIC_NAME}" in ics
    assert f"LOCATION:{CLINIC_ADDRESS}" in ics
    assert "BEGIN:VEVENT" in ics and "END:VEVENT" in ics


def test_each_ics_gets_a_unique_uid() -> None:
    assert build_ics(MON_2PM) != build_ics(MON_2PM)


def test_the_uid_is_derived_from_the_event_id_when_one_is_given() -> None:
    """Stable across the booking and the cancellation, which is what lets the second
    file retract the first rather than adding a duplicate."""
    assert "UID:evt-1@" in build_ics(MON_2PM, "evt-1")
    assert "UID:evt-1@" in build_ics(MON_2PM, "evt-1", cancelled=True)


def test_a_naive_ics_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_ics(datetime(2026, 9, 7, 14, 0))


def test_a_cancelled_ics_retracts_rather_than_adds() -> None:
    ics = build_ics(MON_2PM, "evt-1", cancelled=True)

    assert "METHOD:CANCEL" in ics
    assert "STATUS:CANCELLED" in ics
    # Without a higher SEQUENCE a client keeps the copy it already has.
    assert "SEQUENCE:1" in ics
    assert "SEQUENCE:0" in build_ics(MON_2PM, "evt-1")


# -------------------------------------------------------------- code email

def code_message(**overrides) -> EmailMessage:
    kwargs = {
        "to_email": "patient@example.com",
        "code": "123456",
        "appointment_start": MON_2PM,
        "from_email": "clinic@example.com",
        "from_name": CLINIC_NAME,
        "patient_name": "Dev",
        **overrides,
    }
    return build_cancellation_code_message(**kwargs)


def test_the_code_is_in_the_subject_where_a_notification_shows_it() -> None:
    assert "123456" in code_message()["Subject"]


def test_the_code_email_names_the_appointment_at_stake() -> None:
    """Anyone can start this flow by typing somebody else's address, so the mail has to
    say what is about to be cancelled."""
    text = body_of(code_message(), "plain")

    assert "123456" in text
    assert "Monday 7 September at 2:00 PM" in text
    assert CLINIC_ADDRESS in text


def test_the_code_email_tells_the_owner_how_to_do_nothing() -> None:
    """The recourse for a real owner who did not ask for this: ignore it."""
    for subtype in ("plain", "html"):
        body = body_of(code_message(), subtype)
        assert "ignore this email" in body
        assert "stands" in body


def test_the_code_email_states_its_limits() -> None:
    text = body_of(code_message(), "plain")

    assert "10 minutes" in text
    assert str(MAX_ATTEMPTS) in text


def test_the_code_email_carries_no_calendar_file() -> None:
    """It is a credential, not a record of an appointment."""
    assert not [part for part in code_message().walk() if part.get_filename()]


def test_a_naive_time_is_refused_by_the_code_email() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        code_message(appointment_start=datetime(2026, 9, 7, 14, 0))


# ------------------------------------------------------- cancellation receipt

def cancelled_message(**overrides) -> EmailMessage:
    kwargs = {
        "to_email": "patient@example.com",
        "appointment_start": MON_2PM,
        "from_email": "clinic@example.com",
        "from_name": CLINIC_NAME,
        "patient_name": "Dev",
        "event_id": "evt-1",
        **overrides,
    }
    return build_cancellation_message(**kwargs)


def test_the_receipt_says_cancelled_in_words() -> None:
    """Mail-client support for METHOD:CANCEL is uneven, so the prose has to carry it."""
    message = cancelled_message()

    assert "Cancelled" in message["Subject"]
    assert "cancelled" in body_of(message, "plain")
    assert "Monday 7 September at 2:00 PM" in body_of(message, "plain")


def test_the_receipt_attaches_a_retraction() -> None:
    attachments = [
        part for part in cancelled_message().walk()
        if part.get_filename() == "cancelled.ics"
    ]

    assert len(attachments) == 1
    assert "METHOD:CANCEL" in attachments[0].get_content()
    assert "UID:evt-1@" in attachments[0].get_content()


def test_the_retraction_matches_the_uid_of_the_booking() -> None:
    """Different UIDs would leave the patient with two calendar entries, one a ghost."""
    booked = [p for p in message(event_id="evt-1").walk()
              if p.get_filename() == "appointment.ics"][0].get_content()
    cancelled = [p for p in cancelled_message().walk()
                 if p.get_filename() == "cancelled.ics"][0].get_content()

    def uid(ics: str) -> str:
        return next(line for line in ics.splitlines() if line.startswith("UID:"))

    assert uid(booked) == uid(cancelled)


def test_a_naive_time_is_refused_by_the_receipt() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        cancelled_message(appointment_start=datetime(2026, 9, 7, 14, 0))


# ------------------------------------------------------------------ delivery

class FakeSMTP:
    """Records what a real smtplib client would have been told to do."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.raise_on_login: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def starttls(self, context=None): self.started_tls = True

    def login(self, user, password):
        if self.raise_on_login:
            raise self.raise_on_login
        self.logged_in = (user, password)

    def send_message(self, msg): self.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


def service(port: int = 587) -> SmtpEmailService:
    return SmtpEmailService(
        host="smtp.example.com", port=port, username="clinic@example.com",
        password="apppassword1234", from_email="clinic@example.com",
        from_name=CLINIC_NAME,
    )


async def test_port_587_negotiates_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    await service(587).send_confirmation("patient@example.com", MON_2PM, "Dev")

    sent = FakeSMTP.instances[0]
    assert sent.started_tls is True
    assert sent.logged_in == ("clinic@example.com", "apppassword1234")
    assert len(sent.sent) == 1


async def test_port_465_uses_implicit_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMTPS is already encrypted; calling starttls on it is an error."""
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    await service(465).send_confirmation("patient@example.com", MON_2PM, "Dev")

    assert FakeSMTP.instances[0].started_tls is False


async def test_a_timeout_is_always_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without one, a hung server holds a worker thread indefinitely."""
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    await service().send_confirmation("patient@example.com", MON_2PM)

    assert FakeSMTP.instances[0].timeout is not None


async def test_bad_credentials_name_the_app_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By far the most common misconfiguration deserves the most specific message."""
    def failing(*args, **kwargs):
        instance = FakeSMTP(*args, **kwargs)
        instance.raise_on_login = smtplib.SMTPAuthenticationError(535, b"denied")
        return instance

    monkeypatch.setattr(smtplib, "SMTP", failing)

    with pytest.raises(EmailError, match="App Password"):
        await service().send_confirmation("patient@example.com", MON_2PM)


@pytest.mark.parametrize(
    "failure",
    [smtplib.SMTPServerDisconnected("gone"), OSError("connection refused")],
)
async def test_transport_failures_become_email_errors(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def failing(*args, **kwargs):
        raise failure

    monkeypatch.setattr(smtplib, "SMTP", failing)

    with pytest.raises(EmailError):
        await service().send_confirmation("patient@example.com", MON_2PM)


async def test_the_password_never_reaches_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    await service().send_confirmation("patient@example.com", MON_2PM, "Dev")

    assert "apppassword1234" not in FakeSMTP.instances[0].sent[0].as_string()


async def test_only_the_recipient_domain_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The address itself is the one piece of patient data here."""
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    with caplog.at_level("INFO"):
        await service().send_confirmation("patient@example.com", MON_2PM)

    logged = " ".join(
        f"{record.getMessage()} {getattr(record, 'recipient_domain', '')}"
        for record in caplog.records
    )
    assert "example.com" in logged
    assert "patient@example.com" not in logged


# ------------------------------------------------------------------- disabled

async def test_the_disabled_service_refuses_rather_than_pretending() -> None:
    """Silent success would have the bot promise a mail that never existed."""
    with pytest.raises(EmailError, match="not configured"):
        await DisabledEmailService().send_confirmation("p@example.com", MON_2PM)


async def test_the_disabled_service_refuses_a_code_too() -> None:
    """Which stops the cancellation flow dead, as it should: a code that cannot be sent
    cannot prove anything, and a prompt for it could never be satisfied."""
    with pytest.raises(EmailError, match="not configured"):
        await DisabledEmailService().send_cancellation_code(
            "p@example.com", "123456", MON_2PM
        )
    with pytest.raises(EmailError, match="not configured"):
        await DisabledEmailService().send_cancellation_confirmation(
            "p@example.com", MON_2PM
        )


# --------------------------------------------------- delivery of the new messages

async def test_a_code_is_delivered_over_the_same_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    await service().send_cancellation_code("patient@example.com", "123456", MON_2PM, "Dev")

    assert "123456" in FakeSMTP.instances[0].sent[0]["Subject"]


async def test_a_code_is_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A code in a log line is a code in whatever ships the logs."""
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    with caplog.at_level("DEBUG"):
        await service().send_cancellation_code(
            "patient@example.com", "123456", MON_2PM, "Dev"
        )

    for record in caplog.records:
        assert "123456" not in record.getMessage()
        assert "123456" not in str(record.__dict__)


async def test_a_cancellation_receipt_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    await service().send_cancellation_confirmation(
        "patient@example.com", MON_2PM, "Dev", "evt-1"
    )

    assert "Cancelled" in FakeSMTP.instances[0].sent[0]["Subject"]
