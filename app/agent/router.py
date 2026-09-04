"""LAYER 1 -- intent classification.

One model call decides which handler owns the message. It classifies only: it never
answers, and it never resolves a date. The time phrase is captured verbatim so that
Phase 2 can resolve it against the clinic's timezone and business hours, where the
"today"/"tomorrow" reference point is actually known.

Validation is strict about the intent and lenient about the trimmings. A model that
invents an unknown ``faq_topic`` still classified the intent correctly, so the topic
is dropped rather than failing the whole message into an error reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.llm import GroqClient, LLMError, strip_code_fences
from app.agent.prompts import INTENT_CLASSIFIER_PROMPT
from app.domain.business import FAQ_FACTS

logger = logging.getLogger(__name__)

Intent = Literal["greeting", "faq", "booking", "out_of_scope"]

# Below this the classifier is guessing, so the bot asks instead of acting.
MIN_CONFIDENCE = 0.6

# Longer than any real message; guards against a pathological payload reaching the API.
MAX_INPUT_CHARS = 2000


class RoutingError(RuntimeError):
    """Classification failed: the model errored, or returned unusable output."""


class Classification(BaseModel):
    """Validated Layer 1 output."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    faq_topic: str | None = None
    raw_datetime_text: str | None = None

    @field_validator("faq_topic", "raw_datetime_text", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        """Models emit "null", "none" or "" instead of JSON null often enough to matter."""
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
            return None
        return value

    @field_validator("faq_topic")
    @classmethod
    def _known_topic_only(cls, value: str | None) -> str | None:
        """Drop an unrecognised topic rather than rejecting a good classification."""
        if value is None:
            return None
        if value not in FAQ_FACTS:
            logger.warning("router.unknown_faq_topic", extra={"faq_topic": value})
            return None
        return value

    @property
    def is_confident(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE


class IntentRouter:
    """Turns a user message into a :class:`Classification`."""

    def __init__(self, llm: GroqClient) -> None:
        self._llm = llm

    async def classify(self, text: str) -> Classification:
        """Classify one message.

        Raises :class:`RoutingError` on any failure -- transport, timeout, malformed
        JSON or schema violation. The caller turns that into a graceful reply.
        """
        message = text.strip()[:MAX_INPUT_CHARS]
        raw = ""
        try:
            raw = await self._llm.complete_json(INTENT_CLASSIFIER_PROMPT, message)
            payload = json.loads(strip_code_fences(raw))
            classification = Classification.model_validate(payload)
        except LLMError as exc:
            logger.warning("router.llm_failed", extra={"error": str(exc)}, exc_info=True)
            raise RoutingError("model call failed") from exc
        except json.JSONDecodeError as exc:
            # Log the raw output alongside the error: without it a schema drift is
            # invisible and unfixable after the fact.
            logger.warning(
                "router.invalid_json",
                extra={"error": exc.msg, "raw_output": raw[:500]},
                exc_info=True,
            )
            raise RoutingError("model returned malformed JSON") from exc
        except ValidationError as exc:
            logger.warning(
                "router.schema_mismatch",
                extra={"error_count": exc.error_count(), "raw_output": raw[:500]},
                exc_info=True,
            )
            raise RoutingError("model output did not match the schema") from exc

        logger.info(
            "router.classified",
            extra={
                "intent": classification.intent,
                "confidence": round(classification.confidence, 2),
                "faq_topic": classification.faq_topic,
                "has_datetime_text": classification.raw_datetime_text is not None,
            },
        )
        return classification
