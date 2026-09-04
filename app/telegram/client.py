"""Thin async wrapper over the Telegram Bot API.

The bot token sits in the request URL, so no URL is ever logged -- log lines name the
API method instead. ``httpx`` and ``httpcore`` are held at WARNING in
:mod:`app.logging_config` for the same reason.

Messages are sent as plain text. Telegram's MarkdownV2 requires escaping a dozen
characters, and an unescaped one makes the whole send fail with a 400; clinic replies
contain addresses and punctuation, so plain text trades formatting for reliability.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Updates we act on. Narrowing this server-side means Telegram never delivers the
# channel posts and edits the orchestrator would only discard.
ALLOWED_UPDATES = ["message"]


class TelegramError(RuntimeError):
    """The Bot API returned ok=false, or the request could not be completed."""


class TelegramClient:
    """One client per process; call :meth:`aclose` on shutdown."""

    def __init__(self, token: SecretStr, timeout: float = 10.0) -> None:
        self._token = token
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token.get_secret_value()}/{method}"

    async def _call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            response = await self._client.post(
                self._url(method),
                json=payload or {},
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.HTTPError as exc:
            # str(exc) can embed the request URL, and therefore the token.
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"{method} returned non-JSON (HTTP {response.status_code})"
            ) from exc

        if not body.get("ok"):
            raise TelegramError(
                f"{method} rejected (HTTP {response.status_code}): "
                f"{body.get('description', 'no description')}"
            )
        return body.get("result")

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """Send a plain-text reply. Never logs the body."""
        result = await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        logger.info("telegram.sent", extra={"chat_id": chat_id, "reply_len": len(text)})
        return result

    async def get_updates(
        self,
        offset: int | None = None,
        poll_timeout: int = 25,
    ) -> list[dict[str, Any]]:
        """Long-poll for updates.

        The HTTP read timeout must outlast the server-side hold, or every poll would
        abort client-side just before Telegram was ready to answer.
        """
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ALLOWED_UPDATES,
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._call(
            "getUpdates", payload, timeout=poll_timeout + self._timeout
        )

    async def set_webhook(self, url: str, secret_token: str | None = None) -> Any:
        payload: dict[str, Any] = {"url": url, "allowed_updates": ALLOWED_UPDATES}
        if secret_token:
            payload["secret_token"] = secret_token
        result = await self._call("setWebhook", payload)
        logger.info("telegram.webhook_set")
        return result

    async def delete_webhook(self, drop_pending_updates: bool = False) -> Any:
        """Remove the webhook. Required before polling: Telegram allows only one mode."""
        result = await self._call(
            "deleteWebhook", {"drop_pending_updates": drop_pending_updates}
        )
        logger.info("telegram.webhook_deleted")
        return result

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def aclose(self) -> None:
        await self._client.aclose()
