"""LAYER 2 -- datetime resolution.

Turns the verbatim phrase Layer 1 captured ("Monday at 2pm", "tomorrow morning") into
a concrete date and clock time, anchored to the current moment in the clinic's zone.

This layer resolves *words*, nothing else. It is never told the opening hours and never
asked whether a time is bookable: that is decided by :mod:`app.domain.scheduling`, from
constants, after this returns. Splitting it that way means a model that misreads
"Monday" produces a wrong date the user can see and correct, rather than a wrong
*policy* the user cannot.

Separating it from Layer 1 is what makes the reference point available. "Tomorrow" is
meaningless at classification time, when the message is being sorted rather than
scheduled; here the current date is injected into the prompt per call.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from datetime import datetime
from datetime import time as time_cls
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.llm import GroqClient, LLMError, strip_code_fences
from app.agent.prompts import build_resolver_prompt

logger = logging.getLogger(__name__)

# Below this the phrase was genuinely ambiguous ("next week", "the 5th"), so the bot
# asks rather than booking a date the user did not mean.
MIN_RESOLUTION_CONFIDENCE = 0.6

MAX_INPUT_CHARS = 200


class ResolutionError(RuntimeError):
    """Resolution failed: the model errored, or returned unusable output."""


class Resolution(BaseModel):
    """Validated Layer 2 output.

    ``day`` of None means no date could be resolved. ``clock`` of None means a day was
    named with no time -- a real answer, read downstream as "any time that day",
    not a failure.
    """

    model_config = ConfigDict(extra="ignore")

    day: date_cls | None = Field(default=None, alias="date")
    clock: time_cls | None = Field(default=None, alias="time")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("day", "clock", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {
            "", "null", "none", "n/a", "unknown",
        }:
            return None
        return value

    @property
    def is_confident(self) -> bool:
        return self.confidence >= MIN_RESOLUTION_CONFIDENCE

    @property
    def has_day(self) -> bool:
        return self.day is not None


class DatetimeResolver:
    """Resolves a time phrase against a reference moment."""

    def __init__(self, llm: GroqClient) -> None:
        self._llm = llm

    async def resolve(self, phrase: str, now: datetime) -> Resolution:
        """Resolve ``phrase`` relative to ``now`` (aware, clinic zone).

        Raises :class:`ResolutionError` on transport failure, malformed JSON or a
        schema violation, mirroring Layer 1 so the caller has one thing to catch.
        """
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("resolve() requires a timezone-aware reference time")

        message = phrase.strip()[:MAX_INPUT_CHARS]
        raw = ""
        try:
            raw = await self._llm.complete_json(build_resolver_prompt(now), message)
            payload = json.loads(strip_code_fences(raw))
            resolution = Resolution.model_validate(payload)
        except LLMError as exc:
            logger.warning("resolver.llm_failed", extra={"error": str(exc)}, exc_info=True)
            raise ResolutionError("model call failed") from exc
        except json.JSONDecodeError as exc:
            logger.warning(
                "resolver.invalid_json",
                extra={"error": exc.msg, "raw_output": raw[:500]},
                exc_info=True,
            )
            raise ResolutionError("model returned malformed JSON") from exc
        except ValidationError as exc:
            # A model that emits "2026-13-45" or "25:00" lands here rather than
            # producing a nonsense datetime downstream.
            logger.warning(
                "resolver.schema_mismatch",
                extra={"error_count": exc.error_count(), "raw_output": raw[:500]},
                exc_info=True,
            )
            raise ResolutionError("model output did not match the schema") from exc

        logger.info(
            "resolver.resolved",
            extra={
                "has_day": resolution.has_day,
                "has_clock": resolution.clock is not None,
                "confidence": round(resolution.confidence, 2),
            },
        )
        return resolution
