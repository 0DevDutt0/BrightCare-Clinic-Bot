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
from app.services.email_service import (
    DisabledEmailService,
    EmailError,
    SmtpEmailService,
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


def test_a_naive_ics_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_ics(datetime(2026, 9, 7, 14, 0))


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
