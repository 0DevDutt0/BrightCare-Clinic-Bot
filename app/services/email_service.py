"""Confirmation email -- interface only until Phase 4.

Gated on ``settings.email_configured`` so the app runs fully without SMTP credentials
during Phases 1-3, and Phase 4 needs no config change to switch on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


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


class SmtpEmailService(EmailService):
    """Phase 4 implementation over stdlib smtplib, using the SMTP_* settings."""

    async def send_confirmation(
        self,
        to_email: str,
        appointment_start: datetime,
        patient_name: str | None = None,
    ) -> None:
        raise NotImplementedError("Email confirmation lands in Phase 4")
