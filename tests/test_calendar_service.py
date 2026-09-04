"""GoogleCalendarService against a mocked Google.

HTTP is faked with respx rather than the service being faked wholesale, so the request
bodies are actually asserted: that attendees are never sent, that the slot end is
derived from the domain's slot length, and that a per-calendar freeBusy error is not
mistaken for an empty day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.services.calendar_service import (
    CALENDAR_API_ROOT,
    CalendarError,
    GoogleCalendarService,
    _describe_error,
    _overlaps_any,
    _parse_rfc3339,
)

TZ = ZoneInfo("Asia/Kolkata")
CAL = "clinic@example.com"
MONDAY = datetime(2026, 9, 7, tzinfo=TZ)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, tzinfo=TZ)


class StubCredentials:
    """Stands in for google-auth credentials; never signs anything."""

    def __init__(self, expired: bool = False) -> None:
        self.token = "stub-token"
        # Naive UTC, matching google-auth's own convention for `expiry`.
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + (
            timedelta(hours=-1) if expired else timedelta(hours=1)
        )
        self.refreshes = 0

    def refresh(self, _transport: object) -> None:
        self.refreshes += 1
        self.token = "refreshed-token"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


@pytest.fixture
def service() -> GoogleCalendarService:
    return GoogleCalendarService(
        service_account_info=None, calendar_id=CAL, tz=TZ,
        credentials=StubCredentials(),
    )


def busy_response(*intervals: tuple[str, str]) -> dict:
    return {
        "calendars": {
            CAL: {"busy": [{"start": s, "end": e} for s, e in intervals]}
        }
    }


# --------------------------------------------------------------- pure helpers

@pytest.mark.parametrize(
    "slot, expected",
    [
        (at(13, 0), False),   # 13:00-13:30, clear of the block
        (at(13, 30), False),  # ends exactly when busy starts -- back to back, no clash
        (at(14, 0), True),    # starts exactly when busy starts
        (at(14, 30), True),   # sits inside the block
        (at(15, 0), False),   # starts exactly when busy ends -- back to back
    ],
)
def test_overlap_is_half_open_at_both_ends(slot: datetime, expected: bool) -> None:
    """Back-to-back appointments must not be treated as a clash.

    Treating a shared boundary as a collision would lose the slot either side of
    every existing event -- a third of a working day, for no reason.
    """
    busy = [(at(14, 0), at(15, 0))]

    assert _overlaps_any(slot, busy) is expected


@pytest.mark.parametrize(
    "slot, expected",
    [
        (at(14, 0), True),    # 14:00-14:30 straddles the start of 14:15-14:45
        (at(14, 30), True),   # 14:30-15:00 straddles the end of it
        (at(13, 30), False),
        (at(15, 0), False),
    ],
)
def test_an_off_grid_busy_block_still_blocks_the_slots_it_touches(
    slot: datetime, expected: bool
) -> None:
    """Events on the calendar are not obliged to sit on the clinic's 30-minute grid."""
    busy = [(at(14, 15), at(14, 45))]

    assert _overlaps_any(slot, busy) is expected


def test_overlap_against_no_busy_blocks() -> None:
    assert _overlaps_any(at(14, 0), []) is False


@pytest.mark.parametrize(
    "value",
    ["2026-09-07T14:00:00Z", "2026-09-07T14:00:00+00:00", "2026-09-07T19:30:00+05:30"],
)
def test_google_timestamps_parse_to_aware_datetimes(value: str) -> None:
    parsed = _parse_rfc3339(value)

    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 14


def test_error_description_surfaces_googles_reason() -> None:
    """The reason names the fix far better than the status code does."""
    response = httpx.Response(
        403,
        json={"error": {"errors": [{"reason": "forbiddenForServiceAccounts"}],
                        "message": "Service accounts cannot invite attendees"}},
    )

    described = _describe_error(response)

    assert "forbiddenForServiceAccounts" in described
    assert "cannot invite attendees" in described


# ------------------------------------------------------------------- read side

@respx.mock
async def test_list_busy_converts_into_the_clinic_zone(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            200, json=busy_response(("2026-09-07T08:30:00Z", "2026-09-07T09:00:00Z"))
        )
    )

    busy = await service.list_busy(MONDAY, MONDAY + timedelta(days=1))

    assert busy == [(at(14, 0), at(14, 30))]  # 08:30Z is 14:00 IST
    assert busy[0][0].tzinfo is not None


@respx.mock
async def test_a_per_calendar_error_is_not_read_as_a_free_day(
    service: GoogleCalendarService,
) -> None:
    """The HTTP call succeeds, so a silent empty list would book over real events."""
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            200, json={"calendars": {CAL: {"errors": [{"reason": "notFound"}]}}}
        )
    )

    with pytest.raises(CalendarError, match="freeBusy returned errors"):
        await service.list_busy(MONDAY, MONDAY + timedelta(days=1))


@respx.mock
async def test_nearest_available_skips_booked_slots(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            200,
            json=busy_response(
                ("2026-09-07T08:30:00Z", "2026-09-07T09:30:00Z"),  # 14:00-15:00 IST
            ),
        )
    )

    assert await service.find_nearest_available(at(14, 0)) == at(15, 0)


@respx.mock
async def test_nearest_available_returns_the_requested_slot_when_free(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(200, json=busy_response())
    )

    assert await service.find_nearest_available(at(14, 0)) == at(14, 0)


@respx.mock
async def test_a_full_day_yields_none_rather_than_tomorrow(
    service: GoogleCalendarService,
) -> None:
    """NEAREST_AVAILABLE_RULE: never roll over to the next day."""
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            200,
            json=busy_response(("2026-09-07T03:30:00Z", "2026-09-07T12:30:00Z")),
        )
    )

    assert await service.find_nearest_available(at(9, 0)) is None


async def test_a_request_after_the_last_slot_needs_no_api_call(
    service: GoogleCalendarService,
) -> None:
    """No candidates means nothing to ask Google about."""
    with respx.mock:
        route = respx.post(f"{CALENDAR_API_ROOT}/freeBusy")
        assert await service.find_nearest_available(at(23, 0)) is None
        assert not route.called


@respx.mock
async def test_availability_for_a_whole_day_takes_one_request(
    service: GoogleCalendarService,
) -> None:
    """Eighteen slots must not become eighteen round trips."""
    route = respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(200, json=busy_response())
    )

    await service.find_nearest_available(at(9, 0))

    assert route.call_count == 1


# ------------------------------------------------------------------ write side

@respx.mock
async def test_create_event_never_sends_attendees(
    service: GoogleCalendarService,
) -> None:
    """One attendee field returns 403 and fails the entire booking."""
    route = respx.post(f"{CALENDAR_API_ROOT}/calendars/{CAL}/events").mock(
        return_value=httpx.Response(200, json={"id": "evt-1"})
    )

    await service.create_event(
        at(14, 0), "Appointment - Dev", "notes", attendee_email="dev@example.com"
    )

    body = route.calls[0].request.read().decode()
    assert "attendees" not in body
    assert "dev@example.com" in body  # recorded in the description instead


@respx.mock
async def test_create_event_sets_the_end_from_the_slot_length(
    service: GoogleCalendarService,
) -> None:
    import json

    route = respx.post(f"{CALENDAR_API_ROOT}/calendars/{CAL}/events").mock(
        return_value=httpx.Response(200, json={"id": "evt-1"})
    )

    await service.create_event(at(14, 0), "Appointment")

    body = json.loads(route.calls[0].request.read())
    assert body["start"]["dateTime"].startswith("2026-09-07T14:00:00")
    assert body["end"]["dateTime"].startswith("2026-09-07T14:30:00")
    assert body["start"]["timeZone"] == "Asia/Kolkata"


@respx.mock
async def test_create_event_returns_the_id(service: GoogleCalendarService) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/calendars/{CAL}/events").mock(
        return_value=httpx.Response(200, json={"id": "abc123"})
    )

    assert await service.create_event(at(14, 0), "Appointment") == "abc123"


@respx.mock
async def test_an_accepted_event_without_an_id_is_an_error(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/calendars/{CAL}/events").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(CalendarError, match="returned no id"):
        await service.create_event(at(14, 0), "Appointment")


@respx.mock
async def test_is_free_reflects_the_calendar(service: GoogleCalendarService) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            200, json=busy_response(("2026-09-07T08:30:00Z", "2026-09-07T09:00:00Z"))
        )
    )

    assert await service.is_free(at(14, 0)) is False
    assert await service.is_free(at(15, 0)) is True


# ------------------------------------------------------------------- failures

@respx.mock
async def test_an_http_error_becomes_a_calendar_error(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(
            403, json={"error": {"errors": [{"reason": "forbidden"}], "message": "nope"}}
        )
    )

    with pytest.raises(CalendarError, match="forbidden"):
        await service.list_busy(MONDAY, MONDAY + timedelta(days=1))


@respx.mock
async def test_a_transport_failure_becomes_a_calendar_error(
    service: GoogleCalendarService,
) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        side_effect=httpx.ConnectError("network down")
    )

    with pytest.raises(CalendarError, match="calendar request failed"):
        await service.list_busy(MONDAY, MONDAY + timedelta(days=1))


# ------------------------------------------------------------- aware datetimes

@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.list_busy(datetime(2026, 9, 7), datetime(2026, 9, 8)),
        lambda s: s.find_nearest_available(datetime(2026, 9, 7, 14, 0)),
        lambda s: s.is_free(datetime(2026, 9, 7, 14, 0)),
        lambda s: s.create_event(datetime(2026, 9, 7, 14, 0), "x"),
    ],
)
async def test_naive_datetimes_are_refused_at_the_boundary(
    service: GoogleCalendarService, call
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await call(service)


# ------------------------------------------------------------------ token use

@respx.mock
async def test_a_fresh_token_is_not_refreshed(service: GoogleCalendarService) -> None:
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(200, json=busy_response())
    )

    await service.list_busy(MONDAY, MONDAY + timedelta(days=1))

    assert service._credentials.refreshes == 0


@respx.mock
async def test_an_expired_token_is_refreshed_once_for_concurrent_callers() -> None:
    import asyncio

    creds = StubCredentials(expired=True)
    service = GoogleCalendarService(
        service_account_info=None, calendar_id=CAL, tz=TZ, credentials=creds
    )
    respx.post(f"{CALENDAR_API_ROOT}/freeBusy").mock(
        return_value=httpx.Response(200, json=busy_response())
    )

    await asyncio.gather(
        *(service.list_busy(MONDAY, MONDAY + timedelta(days=1)) for _ in range(5))
    )

    assert creds.refreshes == 1  # the lock serialises them
