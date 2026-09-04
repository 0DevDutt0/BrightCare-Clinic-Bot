"""Google Calendar integration -- interface only until Phase 3.

Defined now so the booking flow can be written against a type rather than against
Google's client, and so tests can substitute a fake without patching the network.

Every datetime crossing this boundary is timezone-aware. Google returns RFC 3339
timestamps with offsets, and the clinic's slot grid is defined in its own zone, so a
naive value here would silently compare against the wrong wall clock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class CalendarError(RuntimeError):
    """The calendar could not be read or written."""


class CalendarService(ABC):
    """Read availability and create appointments on the clinic's calendar."""

    @abstractmethod
    async def list_busy(
        self, window_start: datetime, window_end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Busy intervals overlapping the window, as aware (start, end) pairs."""

    @abstractmethod
    async def find_nearest_available(self, requested_start: datetime) -> datetime | None:
        """The soonest free slot at or after ``requested_start``, same business day.

        Returns None when the day has no slot left. See
        :data:`app.domain.business.NEAREST_AVAILABLE_RULE` -- this must never roll
        over to the following day.
        """

    @abstractmethod
    async def create_event(
        self,
        start: datetime,
        summary: str,
        description: str = "",
        attendee_email: str | None = None,
    ) -> str:
        """Create the appointment; returns the created event id."""


class GoogleCalendarService(CalendarService):
    """Phase 3 implementation. Constructed from ``settings.service_account_info``."""

    async def list_busy(
        self, window_start: datetime, window_end: datetime
    ) -> list[tuple[datetime, datetime]]:
        raise NotImplementedError("Google Calendar integration lands in Phase 3")

    async def find_nearest_available(self, requested_start: datetime) -> datetime | None:
        raise NotImplementedError("Slot search lands in Phase 3")

    async def create_event(
        self,
        start: datetime,
        summary: str,
        description: str = "",
        attendee_email: str | None = None,
    ) -> str:
        raise NotImplementedError("Event creation lands in Phase 3")
