"""Google Calendar integration.

Talks to the REST API over httpx rather than through google-api-python-client. That
client is synchronous and builds its own HTTP stack, which in an async app means either
blocking the event loop or wrapping every call in a thread. Only a handful of endpoints
are needed here -- freeBusy, and listing, creating and deleting events -- so the REST
calls are written directly and the app keeps one HTTP library.

``google-auth`` is still used for the part that genuinely needs it -- signing the
service-account JWT -- but its default transport wants ``requests``, so an httpx
transport is supplied instead. Token refresh is synchronous inside that library, so it
runs in a thread; it happens about once an hour, not per request.

Every datetime crossing this boundary is timezone-aware. Google returns RFC 3339
timestamps with offsets, and the clinic's slot grid is defined in its own zone, so a
naive value here would silently compare against the wrong wall clock.

**Attendees are deliberately never sent.** A service account without Domain-Wide
Delegation cannot invite them -- the API rejects the whole request with HTTP 403
``forbiddenForServiceAccounts``, so one attendee field fails the entire booking rather
than degrading. Delegation needs a Workspace domain, which a personal calendar cannot
grant. The patient's address goes in the event description instead, and the patient's
own confirmation is the SMTP mail in Phase 4.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from google.auth.transport import Request as AuthRequest
from google.auth.transport import Response as AuthResponse
from google.oauth2 import service_account

from app.domain.business import SLOT_DURATION, slot_starts

logger = logging.getLogger(__name__)

CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Refresh slightly before expiry so a request never races the token going stale.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

# Where the patient's address is stored for machine lookup. Private extended properties
# are invisible to anyone reading the calendar and, unlike the description, can be
# filtered on server-side with an exact match.
PATIENT_EMAIL_PROPERTY = "patient_email"
PATIENT_NAME_PROPERTY = "patient_name"

# One page is plenty: this is one patient's upcoming appointments, not a calendar dump.
MAX_SEARCH_RESULTS = 50

# The description shape written before extended properties existed. Events already on a
# real calendar carry the address only here, so cancellation still has to read it.
_DESCRIPTION_EMAIL = re.compile(r"Patient email:\s*(\S+)", re.IGNORECASE)
_DESCRIPTION_NAME = re.compile(r"^Patient:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class CalendarError(RuntimeError):
    """The calendar could not be read or written."""


class SlotTaken(CalendarError):
    """The requested slot was free when proposed and is not any more."""


class EventNotFound(CalendarError):
    """The event is not on the calendar: already cancelled, or never there.

    A subclass so that every existing ``except CalendarError`` still catches it, while a
    caller that cares -- cancellation, which should say "already gone" rather than
    "something broke" -- can single it out.
    """


@dataclass(frozen=True)
class Appointment:
    """One upcoming appointment, as the calendar knows it."""

    event_id: str
    start: datetime
    summary: str
    patient_email: str
    patient_name: str | None = None


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
    async def is_free(self, start: datetime) -> bool:
        """Whether one specific slot is still unbooked."""

    @abstractmethod
    async def create_event(
        self,
        start: datetime,
        summary: str,
        description: str = "",
        attendee_email: str | None = None,
        patient_name: str | None = None,
    ) -> str:
        """Create the appointment; returns the created event id."""

    @abstractmethod
    async def find_upcoming_by_email(
        self, email: str, window_start: datetime, window_end: datetime
    ) -> list[Appointment]:
        """Appointments booked with ``email`` that start inside the window, soonest first.

        The window is passed in rather than read from a clock, matching
        :meth:`list_busy` -- this class owns no notion of "now".
        """

    @abstractmethod
    async def cancel_event(self, event_id: str) -> None:
        """Remove one appointment. Raises :class:`EventNotFound` if it is already gone."""


# --------------------------------------------------------------------- transport

class _HttpxAuthResponse(AuthResponse):
    """Adapts an httpx response to the shape google-auth expects."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    @property
    def data(self) -> bytes:
        return self._response.content


class HttpxAuthTransport(AuthRequest):
    """google-auth transport over httpx, so ``requests`` stays out of the tree."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _HttpxAuthResponse:
        with httpx.Client(timeout=timeout or self._timeout) as client:
            return _HttpxAuthResponse(
                client.request(method, url, content=body, headers=headers)
            )


# ----------------------------------------------------------------- implementation

class GoogleCalendarService(CalendarService):
    """Live implementation against the Google Calendar REST API."""

    def __init__(
        self,
        service_account_info: dict[str, Any] | None,
        calendar_id: str,
        tz: ZoneInfo,
        timeout: float = 15.0,
        credentials: Any = None,
    ) -> None:
        """``credentials`` is injectable so tests can exercise the HTTP behaviour
        without signing a real JWT; production always passes the key instead."""
        if credentials is None:
            if service_account_info is None:
                raise ValueError("one of service_account_info or credentials is required")
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=SCOPES
            )
        self._calendar_id = calendar_id
        self._tz = tz
        self._credentials = credentials
        self._transport = HttpxAuthTransport()
        self._client = httpx.AsyncClient(timeout=timeout)
        # Serialises refreshes so concurrent chats do not each mint a token.
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth

    async def _auth_header(self) -> dict[str, str]:
        async with self._token_lock:
            if not self._is_token_fresh():
                try:
                    # google-auth's refresh is synchronous; keep it off the loop.
                    await asyncio.to_thread(self._credentials.refresh, self._transport)
                except Exception as exc:  # google.auth raises several unrelated types
                    raise CalendarError(
                        f"could not obtain a Google access token: {type(exc).__name__}"
                    ) from exc
                logger.info("calendar.token_refreshed")
        return {"Authorization": f"Bearer {self._credentials.token}"}

    def _is_token_fresh(self) -> bool:
        if not self._credentials.token or self._credentials.expiry is None:
            return False
        expiry = self._credentials.expiry
        if expiry.tzinfo is None:
            # google-auth's convention is naive UTC; make that explicit rather than
            # comparing against a deprecated utcnow().
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) + TOKEN_REFRESH_MARGIN < expiry

    # ------------------------------------------------------------------ http

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        headers = await self._auth_header()
        try:
            response = await self._client.request(
                method,
                f"{CALENDAR_API_ROOT}{path}",
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"calendar request failed: {type(exc).__name__}") from exc

        if response.status_code in (200, 201, 204):
            return response.json() if response.content else None
        if response.status_code in (404, 410):
            # 410 Gone is what Google returns for an event deleted from under us.
            raise EventNotFound(
                f"calendar has no such resource ({method} {path}): "
                f"{_describe_error(response)}"
            )
        raise CalendarError(
            f"calendar API rejected {method} {path} "
            f"(HTTP {response.status_code}): {_describe_error(response)}"
        )

    # -------------------------------------------------------------- read side

    async def list_busy(
        self, window_start: datetime, window_end: datetime
    ) -> list[tuple[datetime, datetime]]:
        _require_aware(window_start, "window_start")
        _require_aware(window_end, "window_end")

        payload = await self._call(
            "POST",
            "/freeBusy",
            json_body={
                "timeMin": window_start.isoformat(),
                "timeMax": window_end.isoformat(),
                "items": [{"id": self._calendar_id}],
            },
        )
        entry = (payload or {}).get("calendars", {}).get(self._calendar_id, {})
        if entry.get("errors"):
            # A per-calendar error here means the query "succeeded" with no data,
            # which would otherwise read as a completely free day.
            raise CalendarError(f"freeBusy returned errors: {entry['errors']}")

        busy: list[tuple[datetime, datetime]] = []
        for block in entry.get("busy", []):
            busy.append(
                (
                    _parse_rfc3339(block["start"]).astimezone(self._tz),
                    _parse_rfc3339(block["end"]).astimezone(self._tz),
                )
            )
        return busy

    async def find_nearest_available(self, requested_start: datetime) -> datetime | None:
        """Soonest free slot at or after ``requested_start``, on that same day."""
        _require_aware(requested_start, "requested_start")
        day = requested_start.astimezone(self._tz).date()

        candidates = [
            slot for slot in slot_starts(day, self._tz) if slot >= requested_start
        ]
        if not candidates:
            return None

        # One freeBusy call covers the whole remaining day: N slots, one request.
        busy = await self.list_busy(candidates[0], candidates[-1] + SLOT_DURATION)
        for slot in candidates:
            if not _overlaps_any(slot, busy):
                return slot
        return None

    async def is_free(self, start: datetime) -> bool:
        _require_aware(start, "start")
        busy = await self.list_busy(start, start + SLOT_DURATION)
        return not _overlaps_any(start, busy)

    async def find_upcoming_by_email(
        self, email: str, window_start: datetime, window_end: datetime
    ) -> list[Appointment]:
        """Find this patient's upcoming appointments.

        Two queries, tried in order, because the calendar holds two generations of event:

        1. ``privateExtendedProperty`` -- an exact server-side match on the address
           written by :meth:`create_event`. No false positives, one request.
        2. ``q`` free text -- the fallback for events booked before that property
           existed, which carry the address only in their prose description.

        Whatever either query returns is then matched **exactly** on the address in
        code. Google's free-text search tokenises, so ``q`` alone could hand back a
        near-miss, and offering someone else's appointment for cancellation is the one
        mistake this function must not make.
        """
        _require_aware(window_start, "window_start")
        _require_aware(window_end, "window_end")
        wanted = email.strip().lower()
        if not wanted:
            return []

        items = await self._search_events(
            window_start,
            window_end,
            {"privateExtendedProperty": f"{PATIENT_EMAIL_PROPERTY}={wanted}"},
        )
        if not items:
            items = await self._search_events(window_start, window_end, {"q": wanted})

        found = [
            appointment
            for appointment in (self._to_appointment(item) for item in items)
            if appointment is not None and appointment.patient_email.lower() == wanted
        ]
        found.sort(key=lambda appointment: appointment.start)
        logger.info("calendar.lookup_by_email", extra={"match_count": len(found)})
        return found

    async def _search_events(
        self, window_start: datetime, window_end: datetime, criteria: dict[str, str]
    ) -> list[dict[str, Any]]:
        payload = await self._call(
            "GET",
            f"/calendars/{self._calendar_id}/events",
            params={
                "timeMin": window_start.isoformat(),
                "timeMax": window_end.isoformat(),
                # Expand recurrence, so an event id names one occurrence and cancelling
                # it cannot take a whole series with it.
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": MAX_SEARCH_RESULTS,
                **criteria,
            },
        )
        items = (payload or {}).get("items", [])
        return [item for item in items if isinstance(item, dict)]

    def _to_appointment(self, event: dict[str, Any]) -> Appointment | None:
        """Read one API event, or None when it is not a bookable appointment."""
        event_id = event.get("id")
        start_value = (event.get("start") or {}).get("dateTime")
        if not event_id or not start_value:
            # An all-day entry has "date" instead of "dateTime": a clinic closure or a
            # note, never a 30-minute appointment.
            return None
        if event.get("status") == "cancelled":
            return None

        private = (event.get("extendedProperties") or {}).get("private") or {}
        description = str(event.get("description") or "")
        email = private.get(PATIENT_EMAIL_PROPERTY) or _first_group(
            _DESCRIPTION_EMAIL, description
        )
        if not email:
            return None

        return Appointment(
            event_id=str(event_id),
            start=_parse_rfc3339(start_value).astimezone(self._tz),
            summary=str(event.get("summary") or ""),
            patient_email=str(email).strip(),
            patient_name=private.get(PATIENT_NAME_PROPERTY)
            or _first_group(_DESCRIPTION_NAME, description),
        )

    # ------------------------------------------------------------- write side

    async def create_event(
        self,
        start: datetime,
        summary: str,
        description: str = "",
        attendee_email: str | None = None,
        patient_name: str | None = None,
    ) -> str:
        _require_aware(start, "start")
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": str(self._tz)},
            "end": {
                "dateTime": (start + SLOT_DURATION).isoformat(),
                "timeZone": str(self._tz),
            },
        }

        private: dict[str, str] = {}
        if attendee_email:
            # Never sent as an attendee: see the module docstring. Recorded so the
            # clinic can still see who booked from the event alone -- and, lowercased,
            # as a private property, so cancellation can find it by exact match instead
            # of by searching prose.
            body["description"] = (
                f"{description}\n\nPatient email: {attendee_email}".strip()
            )
            private[PATIENT_EMAIL_PROPERTY] = attendee_email.strip().lower()
        if patient_name:
            private[PATIENT_NAME_PROPERTY] = patient_name
        if private:
            body["extendedProperties"] = {"private": private}

        payload = await self._call(
            "POST",
            f"/calendars/{self._calendar_id}/events",
            params={"sendUpdates": "none"},
            json_body=body,
        )
        event_id = (payload or {}).get("id")
        if not event_id:
            raise CalendarError("calendar accepted the event but returned no id")
        logger.info("calendar.event_created", extra={"event_id": event_id})
        return str(event_id)

    async def cancel_event(self, event_id: str) -> None:
        """Delete the appointment, freeing the slot for someone else.

        A delete rather than a ``status: cancelled`` patch. Google hides cancelled
        events from ``freeBusy`` either way, so the slot returns in both cases; delete
        is the operation that says what happened, and a clinic that needs an audit trail
        needs a record of its own, not a tombstone on a calendar Google eventually purges.
        """
        if not event_id:
            raise ValueError("cancel_event requires an event id")
        await self._call(
            "DELETE",
            # Quoted: this id comes back out of conversation state, and a path segment
            # is the one place an unexpected character would change which URL is called.
            f"/calendars/{self._calendar_id}/events/{quote(event_id, safe='')}",
            params={"sendUpdates": "none"},
        )
        logger.info("calendar.event_cancelled", extra={"event_id": event_id})

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------- helpers

def _require_aware(moment: datetime, label: str) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{label} must be timezone-aware")


def _parse_rfc3339(value: str) -> datetime:
    """Parse a Google timestamp. Handles the trailing 'Z' fromisoformat once refused."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    """First capture of ``pattern`` in ``text``, stripped, or None."""
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _overlaps_any(slot: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    """True when a slot beginning at ``slot`` collides with any busy interval.

    Half-open on both sides: an appointment ending exactly when a busy block starts
    does not collide, and neither does one starting exactly when a block ends.
    """
    slot_end = slot + SLOT_DURATION
    return any(slot < busy_end and slot_end > busy_start for busy_start, busy_end in busy)


def _describe_error(response: httpx.Response) -> str:
    """Surface Google's own reason, which names the fix far better than the status."""
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:200]
    reasons = [d.get("reason") for d in error.get("errors", []) if d.get("reason")]
    message = str(error.get("message", ""))[:200]
    return f"{reasons or error.get('status', '')}: {message}"
