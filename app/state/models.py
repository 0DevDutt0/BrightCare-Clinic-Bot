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

Stage = Literal[
    "idle",
    "awaiting_slot_confirmation",
    "awaiting_email",
    "awaiting_final_confirmation",
]

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


class ConversationState(BaseModel):
    """What the bot remembers about one chat.

    ``stage`` drives routing: anything other than ``idle`` means the next message is a
    continuation of an active flow, not a fresh request to classify.
    """

    chat_id: int
    stage: Stage = "idle"
    requested_start: datetime | None = None
    proposed_start: datetime | None = None
    raw_datetime_text: str | None = None
    patient_email: str | None = None
    patient_name: str | None = None
    history: list[Turn] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    _check_datetimes = field_validator(
        "requested_start", "proposed_start", "updated_at"
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
        """Return to idle, discarding in-flight booking data but keeping history.

        Used by the mid-flow escape hatch when a user changes the subject, and by the
        flows themselves on completion.
        """
        self.stage = "idle"
        self.requested_start = None
        self.proposed_start = None
        self.raw_datetime_text = None
        self.touch()
