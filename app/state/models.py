"""Conversation state.

Every datetime here is timezone-aware, enforced by a validator rather than by
convention -- a naive value raises at assignment, so it cannot silently propagate
into Phase 3's slot arithmetic where it would compare wrongly against clinic hours.

Bookkeeping timestamps (``updated_at``, ``Turn.at``) are UTC; appointment times
(``requested_start``, ``proposed_start``) are in the clinic's zone. Both are aware,
so comparisons across the two are well defined.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.otp import OtpChallenge

Stage = Literal[
    "idle",
    # Booking
    "awaiting_slot_confirmation",
    "awaiting_email",
    "awaiting_final_confirmation",
    # Changing an appointment that already exists. The first four are shared by
    # cancelling and rescheduling: both have to find the appointment and prove who is
    # asking, and only what happens at the end differs.
    "awaiting_change_email",
    "awaiting_change_choice",
    "awaiting_change_intent",
    "awaiting_change_code",
    # Cancelling
    "awaiting_cancel_confirmation",
    # Rescheduling
    "awaiting_reschedule_time",
    "awaiting_reschedule_confirmation",
]

# Which of the two the user is doing. None means they have not said yet -- "I can't make
# Monday" is a problem with an appointment, not an instruction about it -- and the flow
# asks once it has the appointment in view.
ChangeMode = Literal["cancel", "reschedule"]

Role = Literal["user", "bot"]

# Keep the tail of the conversation only: enough for context, bounded so a long-running
# chat cannot grow without limit.
HISTORY_LIMIT = 10


def utc_now() -> datetime:
    """Aware UTC timestamp. The only clock this module reads."""
    return datetime.now(timezone.utc)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "naive datetime rejected: every datetime in this app must be "
            "timezone-aware (attach settings.tz or use utc_now())"
        )
    return value


class Turn(BaseModel):
    """One message in the conversation tail."""

    role: Role
    text: str
    at: datetime = Field(default_factory=utc_now)

    _check_at = field_validator("at")(_require_aware)


class ChangeCandidate(BaseModel):
    """One appointment on offer, while the user picks between several.

    A flattened copy rather than the calendar's own
    :class:`~app.services.calendar_service.Appointment`: state sits below services and
    must not import from them, and this has to survive the turn in which the user
    answers "2".
    """

    event_id: str
    start: datetime
    patient_name: str | None = None
    ics_uid: str | None = None

    _check_start = field_validator("start")(_require_aware)


class ConversationState(BaseModel):
    """What the bot remembers about one chat.

    ``stage`` drives routing: anything other than ``idle`` means the next message is a
    continuation of an active flow, not a fresh request to classify.

    The ``change_*`` fields are the working set for cancelling or rescheduling. They are
    kept apart from ``patient_email`` deliberately: that one is the address a *booking*
    was made with and outlives the flow, while ``change_email`` is an unproven claim that
    the one-time code is about to test, and it must not leak into a later booking.
    """

    chat_id: int
    stage: Stage = "idle"
    requested_start: datetime | None = None
    proposed_start: datetime | None = None
    raw_datetime_text: str | None = None
    patient_email: str | None = None
    patient_name: str | None = None

    # --- changing an existing appointment: cancel or reschedule ---
    change_mode: ChangeMode | None = None
    change_email: str | None = None
    change_event_id: str | None = None
    change_start: datetime | None = None
    change_name: str | None = None
    # Carried from the appointment being changed so the new one inherits it: it is the
    # UID in the patient's own calendar, and a rescheduled appointment that arrives under
    # a new UID is a second entry rather than a moved one.
    change_ics_uid: str | None = None
    change_ics_sequence: int = 0
    # Set once the one-time code has been accepted. It survives a retry inside the same
    # flow -- if the new slot is taken between verifying and booking, the user picks
    # another time without proving themselves twice. It never survives reset_flow, so it
    # only ever authorises the one appointment this flow already identified.
    change_verified: bool = False
    change_candidates: list[ChangeCandidate] = Field(default_factory=list)
    change_otp: OtpChallenge | None = None
    change_code_sends: int = 0
    change_lookups: int = 0
    # Where a reschedule is moving to, once a free slot has been found and agreed.
    reschedule_start: datetime | None = None

    history: list[Turn] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    _check_datetimes = field_validator(
        "requested_start",
        "proposed_start",
        "change_start",
        "reschedule_start",
        "updated_at",
    )(_require_aware)

    @property
    def is_active(self) -> bool:
        """True when a flow is mid-conversation and owns the next message."""
        return self.stage != "idle"

    def touch(self) -> None:
        self.updated_at = utc_now()

    def add_turn(self, role: Role, text: str) -> None:
        """Append a turn and trim to the most recent ``HISTORY_LIMIT``."""
        self.history.append(Turn(role=role, text=text))
        if len(self.history) > HISTORY_LIMIT:
            del self.history[:-HISTORY_LIMIT]
        self.touch()

    def reset_flow(self) -> None:
        """Return to idle, discarding in-flight flow data but keeping history.

        Used by the mid-flow escape hatch when a user changes the subject, and by the
        flows themselves on completion.

        Every ``change_*`` field goes with it, the live one-time code included. A code
        that outlived its flow would still verify on the next one, against whatever
        appointment that flow had found -- which is exactly the thing the code exists to
        prevent.
        """
        self.stage = "idle"
        self.requested_start = None
        self.proposed_start = None
        self.raw_datetime_text = None
        self.change_mode = None
        self.change_email = None
        self.change_event_id = None
        self.change_start = None
        self.change_name = None
        self.change_ics_uid = None
        self.change_ics_sequence = 0
        self.change_verified = False
        self.change_candidates = []
        self.change_otp = None
        self.change_code_sends = 0
        self.change_lookups = 0
        self.reschedule_start = None
        self.touch()
