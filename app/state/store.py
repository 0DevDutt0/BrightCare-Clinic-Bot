"""State storage.

The interface is async even though the in-memory implementation needs no I/O. That is
deliberate: the intended Phase-5 swap is Redis, whose clients are async, and if the
interface were sync every handler would have to change to adopt it. Handlers await the
store today and will keep awaiting it unchanged.

Known limitation of the in-memory backend: state lives in one process, so running
uvicorn with multiple workers would give each worker its own view of a conversation.
That is fine for local development and is exactly what the Redis backend fixes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Callable

from app.state.models import ConversationState, utc_now

DEFAULT_TTL = timedelta(minutes=30)


class StateStore(ABC):
    """Conversation state keyed by Telegram chat id."""

    @abstractmethod
    async def get(self, chat_id: int) -> ConversationState:
        """Return the stored state, or a fresh idle one if absent or expired."""

    @abstractmethod
    async def save(self, state: ConversationState) -> None:
        """Persist the state."""

    @abstractmethod
    async def clear(self, chat_id: int) -> None:
        """Forget everything about this chat."""


class InMemoryStateStore(StateStore):
    """Dict-backed store with idle expiry.

    ``now`` is injectable so TTL behaviour can be tested without sleeping.
    """

    def __init__(
        self,
        ttl: timedelta = DEFAULT_TTL,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._states: dict[int, ConversationState] = {}
        self._ttl = ttl
        self._now = now

    def _is_expired(self, state: ConversationState) -> bool:
        return self._now() - state.updated_at > self._ttl

    async def get(self, chat_id: int) -> ConversationState:
        state = self._states.get(chat_id)
        if state is None:
            return ConversationState(chat_id=chat_id)
        if self._is_expired(state):
            # An abandoned booking should not resume half an hour later: drop it and
            # start clean, so the next message is classified as a fresh request.
            del self._states[chat_id]
            return ConversationState(chat_id=chat_id)
        return state

    async def save(self, state: ConversationState) -> None:
        self._states[state.chat_id] = state

    async def clear(self, chat_id: int) -> None:
        self._states.pop(chat_id, None)

    def purge_expired(self) -> int:
        """Drop expired entries; returns how many were removed."""
        stale = [cid for cid, st in self._states.items() if self._is_expired(st)]
        for chat_id in stale:
            del self._states[chat_id]
        return len(stale)

    def __len__(self) -> int:
        return len(self._states)
