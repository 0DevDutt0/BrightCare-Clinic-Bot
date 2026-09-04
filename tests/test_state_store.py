"""StateStore contract and in-memory TTL behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.state.models import ConversationState
from app.state.store import DEFAULT_TTL, InMemoryStateStore, StateStore


class Clock:
    """Injectable clock, so TTL is tested without sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def test_in_memory_store_implements_the_interface() -> None:
    assert issubclass(InMemoryStateStore, StateStore)


async def test_unknown_chat_gets_a_fresh_idle_state() -> None:
    state = await InMemoryStateStore().get(999)

    assert state.chat_id == 999
    assert state.stage == "idle"
    assert state.history == []


async def test_saved_state_round_trips() -> None:
    store = InMemoryStateStore()
    state = await store.get(1)
    state.stage = "awaiting_email"
    state.patient_email = "someone@example.com"
    await store.save(state)

    restored = await store.get(1)

    assert restored.stage == "awaiting_email"
    assert restored.patient_email == "someone@example.com"


async def test_state_survives_up_to_the_ttl() -> None:
    clock = Clock()
    store = InMemoryStateStore(ttl=timedelta(minutes=30), now=clock)
    state = ConversationState(chat_id=1, stage="awaiting_email", updated_at=clock.now)
    await store.save(state)

    clock.advance(timedelta(minutes=29))

    assert (await store.get(1)).stage == "awaiting_email"


async def test_state_expires_after_the_ttl() -> None:
    """An abandoned booking must not resume half an hour later."""
    clock = Clock()
    store = InMemoryStateStore(ttl=timedelta(minutes=30), now=clock)
    await store.save(
        ConversationState(chat_id=1, stage="awaiting_email", updated_at=clock.now)
    )

    clock.advance(timedelta(minutes=31))

    assert (await store.get(1)).stage == "idle"
    assert len(store) == 0  # the stale entry is dropped, not just hidden


async def test_clear_forgets_the_conversation() -> None:
    store = InMemoryStateStore()
    await store.save(ConversationState(chat_id=1, stage="awaiting_email"))

    await store.clear(1)

    assert (await store.get(1)).stage == "idle"
    assert len(store) == 0


async def test_purge_expired_reports_what_it_removed() -> None:
    clock = Clock()
    store = InMemoryStateStore(ttl=timedelta(minutes=30), now=clock)
    for chat_id in (1, 2, 3):
        await store.save(ConversationState(chat_id=chat_id, updated_at=clock.now))

    clock.advance(timedelta(minutes=31))
    fresh = ConversationState(chat_id=4, updated_at=clock.now)
    await store.save(fresh)

    assert store.purge_expired() == 3
    assert len(store) == 1


def test_default_ttl_is_thirty_minutes() -> None:
    assert DEFAULT_TTL == timedelta(minutes=30)
