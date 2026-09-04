"""FastAPI application: wiring, lifespan, health check, webhook route.

Both transports converge on one ``UpdateHandler``. Polling drives it from a background
task; the webhook route drives it from a request. Neither knows anything the other
does, so switching RUN_MODE changes how updates arrive and nothing about how they are
answered.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response

from app.agent.llm import GroqClient
from app.agent.orchestrator import Orchestrator
from app.agent.resolver import DatetimeResolver
from app.agent.router import IntentRouter
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.state.store import InMemoryStateStore
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.handler import UpdateHandler
from app.telegram.poller import Poller

logger = logging.getLogger(__name__)

# Telegram only needs to know the request was accepted; it ignores the body.
_ACK = Response(status_code=200)


def build_components(settings: Settings) -> dict[str, Any]:
    """Construct the object graph. Separated from lifespan so tests can reuse it."""
    client = TelegramClient(settings.telegram_bot_token)
    llm = GroqClient(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        base_url=settings.groq_base_url,
        timeout=settings.groq_timeout_seconds,
    )
    store = InMemoryStateStore()
    # Both layers share one client: same model, same retry policy, one connection pool.
    orchestrator = Orchestrator(
        router=IntentRouter(llm),
        store=store,
        resolver=DatetimeResolver(llm),
        tz=settings.tz,
    )
    handler = UpdateHandler(client, orchestrator)
    return {
        "client": client,
        "llm": llm,
        "store": store,
        "orchestrator": orchestrator,
        "handler": handler,
        "poller": Poller(client, handler),
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("app.starting", extra=settings.safe_summary())

    components = build_components(settings)
    app.state.settings = settings
    for name, value in components.items():
        setattr(app.state, name, value)

    client: TelegramClient = components["client"]
    try:
        me = await client.get_me()
        logger.info("app.bot_identified", extra={"bot_username": me.get("username")})
    except TelegramError:
        # Not fatal: the health endpoint should still come up so the failure is
        # visible as a running app with a broken token, not a crash loop.
        logger.warning("app.get_me_failed", exc_info=True)

    if settings.run_mode == "polling":
        components["poller"].start()
    else:
        webhook_url = settings.public_base_url.rstrip("/") + settings.webhook_path
        try:
            await client.set_webhook(webhook_url)
        except TelegramError:
            logger.exception("app.set_webhook_failed")

    try:
        yield
    finally:
        logger.info("app.stopping")
        if settings.run_mode == "polling":
            await components["poller"].stop()
        await components["llm"].aclose()
        await client.aclose()


app = FastAPI(title="BrightCare Clinic Bot", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness plus the resolved configuration, with no secrets in the response."""
    settings: Settings = request.app.state.settings
    poller: Poller = request.app.state.poller
    return {
        "status": "ok",
        "run_mode": settings.run_mode,
        "polling_active": settings.run_mode == "polling" and poller.is_running,
        "model": settings.groq_model,
        "timezone": settings.timezone,
        "email_configured": settings.email_configured,
    }


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    """Receive an update. Always answers 200.

    A non-200 makes Telegram redeliver with backoff, and a bug that reliably 500s
    turns into a retry storm that outlives the deploy. Failures are logged and, where
    possible, answered to the user instead.
    """
    settings: Settings = request.app.state.settings
    expected = settings.telegram_webhook_secret.get_secret_value()

    # compare_digest keeps the check constant-time; an empty expected secret (polling
    # mode) must never match, so reject before comparing.
    if not expected or not secrets.compare_digest(secret, expected):
        logger.warning("webhook.bad_secret")
        return _ACK

    try:
        update = await request.json()
    except Exception:
        logger.warning("webhook.invalid_json", exc_info=True)
        return _ACK

    if not isinstance(update, dict):
        logger.warning("webhook.unexpected_payload")
        return _ACK

    try:
        await request.app.state.handler.handle_update(update)
    except Exception:
        logger.exception("webhook.handler_failed")
    return _ACK
