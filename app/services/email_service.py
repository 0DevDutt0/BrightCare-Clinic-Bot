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

Three messages leave here, and they carry different weight:

*The booking confirmation* is a receipt. It can fail without undoing anything.

*The cancellation code* is a credential. If it does not arrive the cancellation cannot
proceed at all, so its failure aborts the flow rather than being reported and shrugged off.

*The cancellation confirmation* is a receipt again, and its ``.ics`` reuses the UID of
the booking's -- derived from the Google event id, so the two files are the same event
to a mail client and the second can retract the first.
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
from app.domain.otp import MAX_ATTEMPTS, OTP_TTL
from app.domain.scheduling import format_slot

logger = logging.getLogger(__name__)

# Implicit TLS rather than STARTTLS. 587 is the usual submission port.
SMTPS_PORT = 465

DEFAULT_TIMEOUT_SECONDS = 15.0

_OTP_MINUTES = int(OTP_TTL.total_seconds() // 60)


class EmailError(RuntimeError):
    """The mail could not be sent."""


class EmailService(ABC):
    """Sends appointment confirmations, cancellation codes and cancellation receipts."""

    @abstractmethod
    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
    ) -> None:
        """Email a confirmation for an appointment at ``appointment_start`` (aware)."""

    @abstractmethod
    async def send_cancellation_code(
        self,
        to_email: str,
        code: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        """Email the one-time code that authorises cancelling that appointment."""

    @abstractmethod
    async def send_cancellation_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> None:
        """Email a receipt for an appointment that has just been cancelled."""

    @abstractmethod
    async def send_reschedule_confirmation(
        self,
        to_email: str,
        previous_start: datetime,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 1,
    ) -> None:
        """Email a receipt for an appointment that has just moved."""


# ------------------------------------------------------------------ construction

def build_ics(
    start: datetime,
    uid: str | None = None,
    *,
    cancelled: bool = False,
    sequence: int | None = None,
) -> str:
    """A minimal VEVENT the patient's mail client can add to their own calendar.

    Times are emitted in UTC with a trailing Z, which every client understands and
    which sidesteps having to ship a VTIMEZONE block. Lines are CRLF-terminated as
    RFC 5545 requires, and kept short so none needs folding.

    ``uid`` should be the Google event id. Deriving the UID from it rather than from a
    fresh uuid4 is what lets the cancellation file refer to the same event as the
    booking file, so a client that honours ``METHOD:CANCEL`` removes the appointment
    instead of adding a second copy of it. Support for that pairing is uneven across
    mail clients -- the retraction is worth sending, not worth relying on, which is why
    the message says in words that the appointment is cancelled.
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
        "METHOD:CANCEL" if cancelled else "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4()}@brightcare.invalid",
        # A revision must outrank the copy the client already holds, or it is ignored.
        # Defaults to 1 for a retraction and 0 for an original; a reschedule passes the
        # next number in the appointment's own run, since it may not be the first.
        f"SEQUENCE:{(1 if cancelled else 0) if sequence is None else max(sequence, 0)}",
        f"DTSTAMP:{stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{stamp(start)}",
        f"DTEND:{stamp(start + SLOT_DURATION)}",
        f"SUMMARY:Appointment at {CLINIC_NAME}",
        f"LOCATION:{CLINIC_ADDRESS}",
        f"DESCRIPTION:Your {SLOT_MINUTES}-minute appointment at {CLINIC_NAME}.",
        "STATUS:CANCELLED" if cancelled else "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def _envelope(
    subject: str, to_email: str, from_email: str, from_name: str
) -> EmailMessage:
    """The headers every message here shares."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_email))
    message["To"] = to_email
    message["Date"] = formatdate(localtime=True)
    return message


def _require_aware_start(appointment_start: datetime) -> None:
    if appointment_start.tzinfo is None or appointment_start.tzinfo.utcoffset(
        appointment_start
    ) is None:
        raise ValueError("appointment_start must be timezone-aware")


def build_confirmation_message(
    to_email: str,
    appointment_start: datetime,
    from_email: str,
    from_name: str,
    patient_name: str | None = None,
    ics_uid: str | None = None,
) -> EmailMessage:
    """Assemble the confirmation. Pure: no I/O, no configuration lookup."""
    _require_aware_start(appointment_start)

    when = format_slot(appointment_start)
    greeting = f"Hello {patient_name}," if patient_name else "Hello,"

    message = _envelope(
        f"Your appointment at {CLINIC_NAME} - {when}", to_email, from_email, from_name
    )

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
        build_ics(appointment_start, ics_uid).encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename="appointment.ics",
    )
    return message


def build_cancellation_code_message(
    to_email: str,
    code: str,
    appointment_start: datetime,
    from_email: str,
    from_name: str,
    patient_name: str | None = None,
) -> EmailMessage:
    """Assemble the one-time code mail. Pure: no I/O, no configuration lookup.

    The code is in the subject line as well as the body, which is where a phone's
    notification will show it. That is a deliberate exposure and a small one: the code
    is single-use, dies in ten minutes, and cancels exactly one named appointment.

    The last line matters more than it looks. Anyone can start this flow by typing
    somebody else's address, so the real owner has to be told, in the mail itself, that
    ignoring it leaves their appointment untouched.
    """
    _require_aware_start(appointment_start)

    when = format_slot(appointment_start)
    greeting = f"Hello {patient_name}," if patient_name else "Hello,"

    message = _envelope(
        f"{code} is your code to cancel your {CLINIC_NAME} appointment",
        to_email,
        from_email,
        from_name,
    )

    message.set_content(
        f"{greeting}\n\n"
        f"Someone asked to cancel this appointment on Telegram:\n\n"
        f"  When:  {when}\n"
        f"  Where: {CLINIC_ADDRESS}\n\n"
        f"Your confirmation code is:\n\n"
        f"    {code}\n\n"
        f"Enter it in the chat within {_OTP_MINUTES} minutes to cancel. "
        f"You get {MAX_ATTEMPTS} attempts.\n\n"
        "If this wasn't you, ignore this email - nothing has been cancelled and your "
        "appointment stands.\n\n"
        f"{CLINIC_NAME}\n"
    )

    message.add_alternative(
        f"""\
<html><body style="font-family:system-ui,sans-serif;color:#1a1a1a">
  <p>{greeting}</p>
  <p>Someone asked to cancel this appointment on Telegram:</p>
  <table cellpadding="6" style="border-collapse:collapse">
    <tr><td><strong>When</strong></td><td>{when}</td></tr>
    <tr><td><strong>Where</strong></td><td>{CLINIC_ADDRESS}</td></tr>
  </table>
  <p>Your confirmation code is:</p>
  <p style="font-size:28px;letter-spacing:6px;font-weight:700">{code}</p>
  <p>Enter it in the chat within {_OTP_MINUTES} minutes to cancel.
     You get {MAX_ATTEMPTS} attempts.</p>
  <p><strong>If this wasn't you, ignore this email</strong> - nothing has been
     cancelled and your appointment stands.</p>
  <p>{CLINIC_NAME}</p>
</body></html>
""",
        subtype="html",
    )
    # No .ics here. This mail is a credential, not a record of an appointment.
    return message


def build_cancellation_message(
    to_email: str,
    appointment_start: datetime,
    from_email: str,
    from_name: str,
    patient_name: str | None = None,
    ics_uid: str | None = None,
    ics_sequence: int = 0,
) -> EmailMessage:
    """Assemble the cancellation receipt. Pure: no I/O, no configuration lookup."""
    _require_aware_start(appointment_start)

    when = format_slot(appointment_start)
    greeting = f"Hello {patient_name}," if patient_name else "Hello,"

    message = _envelope(
        f"Cancelled - your appointment at {CLINIC_NAME} on {when}",
        to_email,
        from_email,
        from_name,
    )

    message.set_content(
        f"{greeting}\n\n"
        f"Your appointment at {CLINIC_NAME} has been cancelled.\n\n"
        f"  Was:   {when}\n"
        f"  Where: {CLINIC_ADDRESS}\n\n"
        "Nothing further is needed. To book another time, just message us on "
        "Telegram.\n\n"
        f"{CLINIC_NAME}\n"
    )

    message.add_alternative(
        f"""\
<html><body style="font-family:system-ui,sans-serif;color:#1a1a1a">
  <p>{greeting}</p>
  <p>Your appointment at <strong>{CLINIC_NAME}</strong> has been cancelled.</p>
  <table cellpadding="6" style="border-collapse:collapse">
    <tr><td><strong>Was</strong></td><td><s>{when}</s></td></tr>
    <tr><td><strong>Where</strong></td><td>{CLINIC_ADDRESS}</td></tr>
  </table>
  <p>Nothing further is needed. To book another time, just message us on Telegram.</p>
  <p>{CLINIC_NAME}</p>
</body></html>
""",
        subtype="html",
    )

    message.add_attachment(
        build_ics(appointment_start, ics_uid, cancelled=True, sequence=ics_sequence).encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename="cancelled.ics",
    )
    return message


def build_reschedule_message(
    to_email: str,
    previous_start: datetime,
    appointment_start: datetime,
    from_email: str,
    from_name: str,
    patient_name: str | None = None,
    ics_uid: str | None = None,
    ics_sequence: int = 1,
) -> EmailMessage:
    """Assemble the "your appointment has moved" receipt. Pure: no I/O.

    One message, not a cancellation followed by a confirmation. Two mails for one action
    is noise, and worse, they race: arriving out of order in an inbox they read as
    "cancelled" last, which is the opposite of what happened.

    The attachment carries the appointment's *existing* UID with a higher SEQUENCE and
    the new time, which is what iCalendar rescheduling actually is -- a client that
    honours it moves the entry. A fresh UID would leave the old time sitting beside the
    new one, which is the failure this mechanism exists to avoid.
    """
    _require_aware_start(previous_start)
    _require_aware_start(appointment_start)

    was, now = format_slot(previous_start), format_slot(appointment_start)
    greeting = f"Hello {patient_name}," if patient_name else "Hello,"

    message = _envelope(
        f"Moved - your appointment at {CLINIC_NAME} is now {now}",
        to_email,
        from_email,
        from_name,
    )

    message.set_content(
        f"{greeting}\n\n"
        f"Your appointment at {CLINIC_NAME} has been moved.\n\n"
        f"  Was:   {was}\n"
        f"  Now:   {now}\n"
        f"  Where: {CLINIC_ADDRESS}\n"
        f"  Length: {SLOT_MINUTES} minutes\n\n"
        "The earlier time has been released. Nothing further is needed.\n\n"
        f"{CLINIC_NAME}\n"
    )

    message.add_alternative(
        f"""\
<html><body style="font-family:system-ui,sans-serif;color:#1a1a1a">
  <p>{greeting}</p>
  <p>Your appointment at <strong>{CLINIC_NAME}</strong> has been moved.</p>
  <table cellpadding="6" style="border-collapse:collapse">
    <tr><td><strong>Was</strong></td><td><s>{was}</s></td></tr>
    <tr><td><strong>Now</strong></td><td><strong>{now}</strong></td></tr>
    <tr><td><strong>Where</strong></td><td>{CLINIC_ADDRESS}</td></tr>
    <tr><td><strong>Length</strong></td><td>{SLOT_MINUTES} minutes</td></tr>
  </table>
  <p>The earlier time has been released. Nothing further is needed.</p>
  <p>{CLINIC_NAME}</p>
</body></html>
""",
        subtype="html",
    )

    message.add_attachment(
        build_ics(appointment_start, ics_uid, sequence=ics_sequence).encode("utf-8"),
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
        ics_uid: str | None = None,
    ) -> None:
        await self._send(
            build_confirmation_message(
                to_email=to_email,
                appointment_start=appointment_start,
                from_email=self._from_email,
                from_name=self._from_name,
                patient_name=patient_name,
                ics_uid=ics_uid,
            ),
            to_email,
            kind="confirmation",
        )

    async def send_cancellation_code(
        self,
        to_email: str,
        code: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        await self._send(
            build_cancellation_code_message(
                to_email=to_email,
                code=code,
                appointment_start=appointment_start,
                from_email=self._from_email,
                from_name=self._from_name,
                patient_name=patient_name,
            ),
            to_email,
            kind="cancellation_code",
        )

    async def send_cancellation_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> None:
        await self._send(
            build_cancellation_message(
                to_email=to_email,
                appointment_start=appointment_start,
                from_email=self._from_email,
                from_name=self._from_name,
                patient_name=patient_name,
                ics_uid=ics_uid,
                ics_sequence=ics_sequence,
            ),
            to_email,
            kind="cancellation",
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
        await self._send(
            build_reschedule_message(
                to_email=to_email,
                previous_start=previous_start,
                appointment_start=appointment_start,
                from_email=self._from_email,
                from_name=self._from_name,
                patient_name=patient_name,
                ics_uid=ics_uid,
                ics_sequence=ics_sequence,
            ),
            to_email,
            kind="reschedule",
        )

    async def _send(self, message: EmailMessage, to_email: str, *, kind: str) -> None:
        """Deliver one built message, mapping SMTP failures onto :class:`EmailError`."""
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

        # Neither the recipient nor, for a code mail, anything of its contents: only the
        # domain, which is what actually helps when SMTP starts refusing one provider.
        logger.info(
            "email.sent",
            extra={"kind": kind, "recipient_domain": to_email.rsplit("@", 1)[-1]},
        )

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

    For booking that is a degraded but working service -- the appointment is real, and
    the reply says no email is coming. For cancelling and rescheduling it is a closed
    door, since the one-time code has nowhere to go; those flows stop and say so rather
    than pretending to verify.
    """

    _MESSAGE = "SMTP is not configured; set the SMTP_* environment variables"

    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
    ) -> None:
        raise EmailError(self._MESSAGE)

    async def send_cancellation_code(
        self,
        to_email: str,
        code: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        raise EmailError(self._MESSAGE)

    async def send_cancellation_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 0,
    ) -> None:
        raise EmailError(self._MESSAGE)

    async def send_reschedule_confirmation(
        self,
        to_email: str,
        previous_start: datetime,
        appointment_start: datetime,
        patient_name: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int = 1,
    ) -> None:
        raise EmailError(self._MESSAGE)
