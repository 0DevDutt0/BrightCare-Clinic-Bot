"""Routing: deterministic pre-checks, then state, then intent, then dispatch.

The order is the design. Each step is a chance to answer *without* calling the model,
and only a message that survives all of them costs a classification:

  Step 0  message shape      -- non-text, commands, empty. No model call, ever.
  Step 1  conversation state -- a non-idle stage means the message belongs to an
                                active flow and must not be re-classified.
  Step 2  intent             -- one model call, temperature 0, JSON mode.
  Step 3  dispatch           -- one handler, chosen from the validated intent.

Step 1 before Step 2 is the part that is easy to get wrong. If a user has been asked
"is 2pm alright?" and replies "yes", classifying that message in isolation yields
"greeting" -- confidently, and wrongly. The state, not the words, decides who owns it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.handlers.booking import booking_reply, continue_booking_flow
from app.agent.handlers.faq import faq_reply
from app.agent.handlers.greeting import greeting_reply
from app.agent.handlers.out_of_scope import out_of_scope_reply
from app.agent.router import IntentRouter, RoutingError
from app.domain.business import CAPABILITIES, WELCOME
from app.state.store import StateStore

logger = logging.getLogger(__name__)

NON_TEXT_MESSAGE = "I can only read text messages right now."
EMPTY_MESSAGE = "I didn't catch that. Could you type your question?"
UNKNOWN_COMMAND = f"I don't recognise that command. {CAPABILITIES}"
TROUBLE_MESSAGE = (
    "I'm having trouble right now. Could you try again in a moment?"
)
CLARIFY_MESSAGE = (
    "I'm not sure I understood. You can ask about the clinic - location, hours, "
    "parking, or walk-ins - or tell me a day and time to book an appointment."
)
FLOW_CLEARED = f"No problem, I've cleared that. {CAPABILITIES}"

COMMANDS = frozenset({"/start", "/help"})

# Deterministic mid-flow escape hatch. Keyword matching keeps it free: recognising a
# topic change with the model would cost a classification on every in-flow message,
# and these are the phrases users actually type to back out.
ESCAPE_PHRASES = frozenset(
    {
        "cancel", "stop", "nevermind", "never mind", "start over", "restart",
        "forget it", "quit", "reset", "exit",
    }
)


@dataclass(frozen=True)
class Reply:
    """What the orchestrator decided, and how it got there.

    ``used_llm`` exists so tests can assert a path made no model call -- a stronger
    check than asserting on the reply text, which could match by coincidence.
    """

    text: str
    intent: str
    used_llm: bool = False


class Orchestrator:
    """Routes one message to one reply."""

    def __init__(self, router: IntentRouter, store: StateStore) -> None:
        self._router = router
        self._store = store

    async def handle(self, chat_id: int, message: dict[str, Any]) -> Reply:
        # --- Step 0: message shape, no model call ------------------------------
        text = message.get("text")
        if not isinstance(text, str):
            kind = _describe_non_text(message)
            logger.info("orchestrator.non_text", extra={"chat_id": chat_id, "kind": kind})
            return Reply(NON_TEXT_MESSAGE, "non_text")

        stripped = text.strip()

        if stripped.startswith("/"):
            # "/start@BotName" is what Telegram delivers in groups.
            command = stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command in COMMANDS:
                state = await self._store.get(chat_id)
                state.reset_flow()
                await self._store.save(state)
                logger.info(
                    "orchestrator.command", extra={"chat_id": chat_id, "command": command}
                )
                return Reply(WELCOME, "command")
            logger.info(
                "orchestrator.unknown_command",
                extra={"chat_id": chat_id, "command": command},
            )
            return Reply(UNKNOWN_COMMAND, "unknown_command")

        if not stripped:
            logger.info("orchestrator.empty", extra={"chat_id": chat_id})
            return Reply(EMPTY_MESSAGE, "empty")

        state = await self._store.get(chat_id)
        state.add_turn("user", stripped)

        # --- Step 1: active flow owns the message ------------------------------
        if state.is_active:
            stage = state.stage
            if stripped.lower() in ESCAPE_PHRASES:
                state.reset_flow()
                reply = Reply(FLOW_CLEARED, "flow_cleared")
                logger.info(
                    "orchestrator.flow_cleared", extra={"chat_id": chat_id, "stage": stage}
                )
            else:
                reply = Reply(continue_booking_flow(state), f"continuation.{stage}")
                logger.info(
                    "orchestrator.continuation",
                    extra={"chat_id": chat_id, "stage": stage},
                )
            return await self._finish(state, reply)

        # --- Step 2: classify --------------------------------------------------
        try:
            classification = await self._router.classify(stripped)
        except RoutingError:
            # Already logged with the raw model output by the router.
            return await self._finish(state, Reply(TROUBLE_MESSAGE, "error", used_llm=True))

        if not classification.is_confident:
            logger.info(
                "orchestrator.low_confidence",
                extra={
                    "chat_id": chat_id,
                    "guess": classification.intent,
                    "confidence": round(classification.confidence, 2),
                },
            )
            return await self._finish(state, Reply(CLARIFY_MESSAGE, "unclear", used_llm=True))

        # --- Step 3: dispatch --------------------------------------------------
        intent = classification.intent
        if intent == "greeting":
            text_out = greeting_reply()
        elif intent == "faq":
            text_out = faq_reply(classification.faq_topic)
        elif intent == "booking":
            text_out = booking_reply(state, classification.raw_datetime_text)
        else:
            text_out = out_of_scope_reply()

        return await self._finish(state, Reply(text_out, intent, used_llm=True))

    async def _finish(self, state: Any, reply: Reply) -> Reply:
        """Record the bot's turn and persist before returning."""
        state.add_turn("bot", reply.text)
        await self._store.save(state)
        return reply


def _describe_non_text(message: dict[str, Any]) -> str:
    """Name the attachment kind for logs. Never logs its content or file id."""
    for key in (
        "photo", "voice", "sticker", "video", "document", "audio", "animation",
        "video_note", "location", "contact", "poll", "dice",
    ):
        if key in message:
            return key
    return "unknown"
