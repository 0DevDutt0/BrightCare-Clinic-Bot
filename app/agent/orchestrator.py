"""Routing: deterministic pre-checks, then state, then intent, then dispatch.

The order is the design. Each step is a chance to answer *without* calling the model,
and only a message that survives all of them costs a classification:

  Step 0  message shape      -- non-text, commands, empty. No model call, ever.
  Step 1  conversation state -- a non-idle stage means the message belongs to an
                                active flow and must not be re-classified.
  Step 2  intent (Layer 1)   -- one model call, temperature 0, JSON mode.
  Step 3  dispatch           -- one handler, chosen from the validated intent.

Only the booking handler goes further, spending a second call on Layer 2 to resolve
the time phrase. Greetings, FAQs, refusals and cancellations cost exactly one call; a
message that never reaches Step 2 costs none.

Step 1 dispatches on which flow owns the stage. Booking may hand a puzzling reply back
to the classifier; cancellation never does -- see
:mod:`app.agent.handlers.cancellation`.

Step 1 before Step 2 is the part that is easy to get wrong. If a user has been asked
"is 2pm alright?" and replies "yes", classifying that message in isolation yields
"greeting" -- confidently, and wrongly. The state, not the words, decides who owns it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.agent.handlers.booking import continue_booking, start_booking
from app.agent.handlers.changes import continue_change, start_change
from app.agent.handlers.faq import faq_reply
from app.agent.handlers.greeting import greeting_reply
from app.agent.handlers.out_of_scope import out_of_scope_reply
from app.agent.resolver import DatetimeResolver
from app.agent.router import IntentRouter, RoutingError
from app.domain.business import CAPABILITIES, WELCOME
from app.services.calendar_service import CalendarService
from app.services.email_service import EmailService
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
    "parking, or walk-ins - tell me a day and time to book an appointment, or say "
    "you'd like to move or cancel one."
)
FLOW_CLEARED = f"No problem, I've cleared that. {CAPABILITIES}"
# Backing out of a *change* needs its own wording. "I've cleared that" is fine when the
# abandoned flow was a booking, and dangerously readable as "cleared your appointment"
# when it was not -- the one sentence a patient must not misread.
CHANGE_ABANDONED = (
    "Alright, I've stopped there - nothing has changed and your appointment is still "
    f"booked as it was. {CAPABILITIES}"
)

COMMANDS = frozenset({"/start", "/help"})

# Stages owned by the change flow -- cancelling or rescheduling. Everything else
# non-idle belongs to booking.
CHANGE_STAGES = frozenset(
    {
        "awaiting_change_email",
        "awaiting_change_choice",
        "awaiting_change_intent",
        "awaiting_change_code",
        "awaiting_cancel_confirmation",
        "awaiting_reschedule_time",
        "awaiting_reschedule_confirmation",
    }
)

# The one stage where "cancel" is an answer rather than a way out. The bot has just
# asked "move it, or cancel it?", so treating the reply as an escape would abandon the
# flow and tell the user nothing was cancelled -- while they were trying to cancel.
_ESCAPE_EXEMPT = {"awaiting_change_intent": frozenset({"cancel"})}

# Intents that open the change flow, and what each one already tells us. None means the
# user has said something is up with an appointment without saying what they want done:
# "I can't make Monday" is a problem, not an instruction. Guessing there either destroys
# a booking they meant to keep or leaves one they meant to drop, so the flow asks.
CHANGE_INTENTS: dict[str, str | None] = {
    "cancel": "cancel",
    "reschedule": "reschedule",
    "change_appointment": None,
}

# Deterministic mid-flow escape hatch. Keyword matching keeps it free: recognising a
# topic change with the model would cost a classification on every in-flow message,
# and these are the phrases users actually type to back out.
#
# "cancel" is here, and it is the one entry that means two things. The match is on the
# *whole* message, so a bare "cancel" backs out of whatever flow is running while
# "cancel my appointment" falls through to be classified -- which is the reading a user
# who is mid-booking wants, and the safe reading for a user who is mid-cancellation,
# since backing out cancels nothing.
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

    def __init__(
        self,
        router: IntentRouter,
        store: StateStore,
        resolver: DatetimeResolver,
        calendar: CalendarService,
        email: EmailService,
        tz: ZoneInfo,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._router = router
        self._store = store
        self._resolver = resolver
        self._calendar = calendar
        self._email = email
        self._tz = tz
        # Injectable so "tomorrow at 3pm" can be tested against a fixed reference
        # instead of whatever day the suite happens to run on.
        self._now = now or (lambda: datetime.now(tz))

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
        # Telegram already knows who this is; asking would be a wasted turn.
        # Never logged: it is the one piece of personal data in the flow.
        sender_name = (message.get("from") or {}).get("first_name")
        if isinstance(sender_name, str) and sender_name.strip():
            state.patient_name = sender_name.strip()

        # --- Step 1: active flow owns the message ------------------------------
        if state.is_active:
            stage = state.stage
            changing = stage in CHANGE_STAGES

            # A stage may claim a word the escape hatch would otherwise swallow.
            escapes = ESCAPE_PHRASES - _ESCAPE_EXEMPT.get(stage, frozenset())
            if stripped.lower() in escapes:
                state.reset_flow()
                logger.info(
                    "orchestrator.flow_cleared", extra={"chat_id": chat_id, "stage": stage}
                )
                cleared = CHANGE_ABANDONED if changing else FLOW_CLEARED
                return await self._finish(state, Reply(cleared, "flow_cleared"))

            if changing:
                # Changing always answers: it has no reclassify path, because nothing at
                # one of its prompts reinterprets as a different request, and dropping
                # the flow silently while confirming a destructive action is the wrong
                # instinct. It is also the one continuation that can cost a model call,
                # so it reports that itself rather than being assumed free.
                result = await continue_change(
                    state,
                    stripped,
                    self._resolver,
                    self._calendar,
                    self._email,
                    self._now(),
                    self._tz,
                )
                logger.info(
                    "orchestrator.continuation",
                    extra={
                        "chat_id": chat_id,
                        "stage": stage,
                        "used_llm": result.used_llm,
                    },
                )
                return await self._finish(
                    state,
                    Reply(result.text, f"continuation.{stage}", used_llm=result.used_llm),
                )

            flow_reply = await continue_booking(
                state, stripped, self._calendar, self._email, self._now(), self._tz
            )
            if flow_reply is not None:
                logger.info(
                    "orchestrator.continuation",
                    extra={"chat_id": chat_id, "stage": stage},
                )
                return await self._finish(
                    state, Reply(flow_reply, f"continuation.{stage}")
                )

            # The reply did not answer the question that was asked -- most likely a
            # different time. continue_booking has already cleared the flow, so fall
            # through and let the router read it as a fresh request.
            logger.info(
                "orchestrator.flow_reclassify",
                extra={"chat_id": chat_id, "stage": stage},
            )

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
            text_out = await start_booking(
                state,
                classification.raw_datetime_text,
                self._resolver,
                self._calendar,
                self._now(),
                self._tz,
            )
        elif intent in CHANGE_INTENTS:
            # No Layer 2 call here: which appointment is settled by the calendar lookup
            # and, where several match, by the user picking a number -- both cheaper and
            # more certain than resolving "Monday" and hoping it names only one of them.
            # A reschedule spends one later, on the new time, once there is an
            # appointment to move.
            text_out = start_change(state, CHANGE_INTENTS[intent])
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
