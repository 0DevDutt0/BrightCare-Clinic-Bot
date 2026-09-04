"""Long-polling runner for local development.

Polling needs no public URL, so the bot runs from a laptop with no tunnel. It calls
the same :meth:`UpdateHandler.handle_update` the webhook route does.

Telegram permits exactly one delivery mode per bot, so the poller deletes any
registered webhook before its first poll. Without that, getUpdates fails with
"terminated by other getUpdates request" or a 409 for as long as the webhook stands.
"""

from __future__ import annotations

import asyncio
import logging

from app.telegram.client import TelegramClient, TelegramError
from app.telegram.handler import UpdateHandler

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


class Poller:
    """Owns the getUpdates loop as a cancellable asyncio task."""

    def __init__(
        self,
        client: TelegramClient,
        handler: UpdateHandler,
        poll_timeout: int = 25,
    ) -> None:
        self._client = client
        self._handler = handler
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def _poll_once(self) -> int:
        """Fetch and process one batch; returns how many updates were handled."""
        updates = await self._client.get_updates(
            offset=self._offset, poll_timeout=self._poll_timeout
        )
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Acknowledge by advancing past this id, even if handling fails --
                # handle_update never raises, and a poison update must not wedge
                # the loop into redelivering it forever.
                self._offset = max(self._offset or 0, update_id + 1)
            await self._handler.handle_update(update)
        return len(updates)

    async def run(self) -> None:
        """Poll until cancelled, backing off on transport errors."""
        try:
            await self._client.delete_webhook()
        except TelegramError:
            logger.warning("poller.webhook_delete_failed", exc_info=True)

        backoff = INITIAL_BACKOFF_SECONDS
        logger.info("poller.started", extra={"poll_timeout": self._poll_timeout})
        while True:
            try:
                count = await self._poll_once()
                backoff = INITIAL_BACKOFF_SECONDS
                if count:
                    logger.info("poller.batch", extra={"updates": count})
            except asyncio.CancelledError:
                logger.info("poller.stopped")
                raise
            except TelegramError:
                logger.warning("poller.error", extra={"retry_in_s": backoff}, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except Exception:
                # Never let an unexpected error kill the loop; the bot would go
                # silent with the process still apparently healthy.
                logger.exception("poller.unexpected", extra={"retry_in_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    @property
    def is_running(self) -> bool:
        """True while the poll loop task is alive. Reported by /health."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="telegram-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
