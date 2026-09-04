"""Appointment confirmation email.

Built on stdlib ``smtplib`` and ``email``. No dependency is needed and none is added.

Two structural choices:

*Message construction is a pure function.* :func:`build_confirmation_message` takes
values and returns an ``EmailMessage``, so the part most worth testing -- headers,
both body alternatives, the calendar attachment -- is tested without mocking a mail
server. Only delivery needs a patch.

*Delivery runs in a thread.* ``smtplib`` is synchronous and would otherwise block the
event loop for the second or two a Gmail handshake takes, stalling every other chat.

The message carries an ``.ics`` attachment. That is not decoration: a service account
cannot add the patient as a calendar attendee (HTTP 403
``forbiddenForServiceAccounts``), so this file is the only way the appointment reaches
the patient's own calendar. ``METHOD:PUBLISH``, not ``REQUEST`` -- it is a copy to
save, not an invitation to answer.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from app.domain.business import CLINIC_ADDRESS, CLINIC_NAME, SLOT_DURATION, SLOT_MINUTES
from app.domain.scheduling import format_slot

logger = logging.getLogger(__name__)

# Implicit TLS rather than STARTTLS. 587 is the usual submission port.
SMTPS_PORT = 465

DEFAULT_TIMEOUT_SECONDS = 15.0


class EmailError(RuntimeError):
    """The confirmation could not be sent."""


class EmailService(ABC):
    """Sends appointment confirmations."""

    @abstractmethod
    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        """Email a confirmation for an appointment at ``appointment_start`` (aware)."""


# ------------------------------------------------------------------ construction

def build_ics(start: datetime, uid: str | None = None) -> str:
    """A minimal VEVENT the patient's mail client can add to their own calendar.

    Times are emitted in UTC with a trailing Z, which every client understands and
    which sidesteps having to ship a VTIMEZONE block. Lines are CRLF-terminated as
    RFC 5545 requires, and kept short so none needs folding.
    """
    if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
        raise ValueError("build_ics requires a timezone-aware datetime")

    def stamp(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{CLINIC_NAME}//Booking Assistant//EN",
        "CALSCALE:GREGORIAN",
        # PUBLISH, not REQUEST: this is a copy to save, not an invitation to answer.
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4()}@brightcare.invalid",
        f"DTSTAMP:{stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{stamp(start)}",
        f"DTEND:{stamp(start + SLOT_DURATION)}",
        f"SUMMARY:Appointment at {CLINIC_NAME}",
        f"LOCATION:{CLINIC_ADDRESS}",
        f"DESCRIPTION:Your {SLOT_MINUTES}-minute appointment at {CLINIC_NAME}.",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_confirmation_message(
    to_email: str,
    appointment_start: datetime,
    from_email: str,
    from_name: str,
    patient_name: str | None = None,
) -> EmailMessage:
    """Assemble the confirmation. Pure: no I/O, no configuration lookup."""
    if appointment_start.tzinfo is None or appointment_start.tzinfo.utcoffset(
        appointment_start
    ) is None:
        raise ValueError("appointment_start must be timezone-aware")

    when = format_slot(appointment_start)
    greeting = f"Hello {patient_name}," if patient_name else "Hello,"

    message = EmailMessage()
    message["Subject"] = f"Your appointment at {CLINIC_NAME} - {when}"
    message["From"] = formataddr((from_name, from_email))
    message["To"] = to_email
    message["Date"] = formatdate(localtime=True)

    message.set_content(
        f"{greeting}\n\n"
        f"Your appointment at {CLINIC_NAME} is confirmed.\n\n"
        f"  When:  {when}\n"
        f"  Where: {CLINIC_ADDRESS}\n"
        f"  Length: {SLOT_MINUTES} minutes\n\n"
        "To cancel or change it, just message us on Telegram.\n\n"
        f"{CLINIC_NAME}\n"
    )

    message.add_alternative(
        f"""\
<html><body style="font-family:system-ui,sans-serif;color:#1a1a1a">
  <p>{greeting}</p>
  <p>Your appointment at <strong>{CLINIC_NAME}</strong> is confirmed.</p>
  <table cellpadding="6" style="border-collapse:collapse">
    <tr><td><strong>When</strong></td><td>{when}</td></tr>
    <tr><td><strong>Where</strong></td><td>{CLINIC_ADDRESS}</td></tr>
    <tr><td><strong>Length</strong></td><td>{SLOT_MINUTES} minutes</td></tr>
  </table>
  <p>To cancel or change it, just message us on Telegram.</p>
  <p>{CLINIC_NAME}</p>
</body></html>
""",
        subtype="html",
    )

    message.add_attachment(
        build_ics(appointment_start).encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename="appointment.ics",
    )
    return message


# ------------------------------------------------------------------- delivery

class SmtpEmailService(EmailService):
    """Sends over SMTP using the SMTP_* settings."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
        from_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_email = from_email
        self._from_name = from_name
        self._timeout = timeout

    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        message = build_confirmation_message(
            to_email=to_email,
            appointment_start=appointment_start,
            from_email=self._from_email,
            from_name=self._from_name,
            patient_name=patient_name,
        )
        try:
            await asyncio.to_thread(self._deliver, message)
        except smtplib.SMTPAuthenticationError as exc:
            # By far the most likely misconfiguration: Gmail rejects an account
            # password here and wants a 16-character App Password.
            raise EmailError(
                "SMTP rejected the credentials -- Gmail requires an App Password, "
                "not the account password"
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise EmailError(f"could not send the email: {type(exc).__name__}") from exc

        # The recipient is never logged: it is the one piece of patient data here.
        logger.info("email.sent", extra={"recipient_domain": to_email.rsplit("@", 1)[-1]})

    def _deliver(self, message: EmailMessage) -> None:
        """Blocking SMTP send. Runs in a thread; never call from the event loop."""
        context = ssl.create_default_context()
        if self._port == SMTPS_PORT:
            with smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            ) as server:
                server.login(self._username, self._password)
                server.send_message(message)
            return

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
            server.starttls(context=context)
            server.login(self._username, self._password)
            server.send_message(message)


class DisabledEmailService(EmailService):
    """Used when SMTP is not configured, so the app runs without credentials.

    Raises rather than silently succeeding: a caller that believes it sent a
    confirmation would tell the patient so, and nothing would arrive.
    """

    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        raise EmailError("SMTP is not configured; set the SMTP_* environment variables")
