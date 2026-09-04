"""The single entrypoint every transport funnels into.

Long polling and the webhook route both call :meth:`UpdateHandler.handle_update` with
the same raw update dict. Nothing below this line knows which transport delivered the
message, which is what lets the agent be tested without either one and lets deployment
switch modes with an env var.

This module owns the *update envelope*: deduplication, discarding update kinds the bot
does not act on, and turning a reply into a sent message. Everything about the
*content* of a message belongs to the orchestrator.
"""

from __future__ import annotations

import logging
from collections import deque
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol

from app.logging_config import text_fingerprint
from app.telegram.client import TelegramClient, TelegramError

if TYPE_CHECKING:  # imported for typing only, so transport does not depend on the agent
    from app.agent.orchestrator import Reply

logger = logging.getLogger(__name__)

FAILURE_MESSAGE = (
    "Sorry, something went wrong on my end. Could you try that again in a moment?"
)

# Update keys that carry a message the bot does not act on. Edits are ignored because
# re-answering an amended question mid-flow confuses the conversation more than it
# helps; channel and inline traffic is out of scope entirely.
IGNORED_UPDATE_KEYS = (
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
)


class SupportsHandle(Protocol):
    """What the transport needs from the agent -- deliberately one method wide."""

    async def handle(self, chat_id: int, message: dict[str, Any]) -> "Reply": ...


class UpdateDeduplicator:
    """Bounded record of update ids already processed.

    Telegram redelivers an update until it is acknowledged, so the same update_id can
    arrive twice: after a webhook timeout, or when polling resumes from a stale offset.
    The bound keeps memory flat on a long-running process; ids age out in arrival
    order, which is safe because Telegram never redelivers an old update after
    thousands of newer ones.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._seen: set[int] = set()
        self._order: deque[int] = deque()

    def check_and_record(self, update_id: int) -> bool:
        """True if this update is new (and now recorded); False if it is a duplicate."""
        if update_id in self._seen:
            return False
        self._seen.add(update_id)
        self._order.append(update_id)
        if len(self._order) > self._capacity:
            self._seen.discard(self._order.popleft())
        return True

    def __len__(self) -> int:
        return len(self._seen)


class UpdateHandler:
    """Transport-agnostic update processing."""

    def __init__(
        self,
        client: TelegramClient,
        orchestrator: SupportsHandle,
        deduplicator: UpdateDeduplicator | None = None,
    ) -> None:
        self._client = client
        self._orchestrator = orchestrator
        self._dedup = deduplicator or UpdateDeduplicator()

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Process one raw Telegram update. Never raises."""
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not self._dedup.check_and_record(update_id):
            logger.info("update.duplicate", extra={"update_id": update_id})
            return

        ignored = next((key for key in IGNORED_UPDATE_KEYS if key in update), None)
        if ignored is not None:
            logger.info("update.ignored", extra={"update_id": update_id, "kind": ignored})
            return

        message = update.get("message")
        if not isinstance(message, dict):
            logger.info("update.no_message", extra={"update_id": update_id})
            return

        chat_id = (message.get("chat") or {}).get("id")
        if not isinstance(chat_id, int):
            logger.warning("update.no_chat_id", extra={"update_id": update_id})
            return

        started = perf_counter()
        logger.info(
            "update.received",
            extra={
                "update_id": update_id,
                "chat_id": chat_id,
                **text_fingerprint(message.get("text") or ""),
            },
        )

        try:
            reply = await self._orchestrator.handle(chat_id, message)
            reply_text, intent, used_llm = reply.text, reply.intent, reply.used_llm
        except Exception:
            # A failure here must still produce a reply: silence looks like a dead bot.
            logger.exception(
                "update.failed", extra={"update_id": update_id, "chat_id": chat_id}
            )
            reply_text, intent, used_llm = FAILURE_MESSAGE, "error", False

        try:
            await self._client.send_message(chat_id, reply_text)
        except TelegramError:
            logger.exception(
                "update.send_failed", extra={"update_id": update_id, "chat_id": chat_id}
            )

        logger.info(
            "update.handled",
            extra={
                "update_id": update_id,
                "chat_id": chat_id,
                "intent": intent,
                "used_llm": used_llm,
                "latency_ms": round((perf_counter() - started) * 1000, 1),
            },
        )
