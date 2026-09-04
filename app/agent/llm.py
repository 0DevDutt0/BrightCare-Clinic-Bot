"""Groq access through the OpenAI-compatible SDK.

Groq speaks the OpenAI wire protocol, so the official ``openai`` client works against
it with only ``base_url`` changed. The model name always comes from settings.

Retry policy: the SDK's own retries are switched off (``max_retries=0``) so this class
is the single place attempts are counted. Left on, the SDK would retry underneath us,
turning the required "one retry" into several and multiplying the wall-clock timeout
the user is waiting on. Transient failures get exactly one retry after a short backoff;
anything the server rejected outright is not retried, because it would be rejected
identically the second time.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Final

import openai
from openai import AsyncOpenAI
from pydantic import SecretStr

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS: Final = 0.5
MAX_ATTEMPTS: Final = 2  # one initial attempt plus one retry

# Must be generous, and the reason is not obvious. Reasoning models (gpt-oss among
# them) spend hidden reasoning tokens *inside* this budget before writing a single
# character of the answer. Measured on this task, reasoning alone ran to 346-376
# tokens, so a 300-token cap left nothing for the JSON: the API then rejected the
# call with HTTP 400 json_validate_failed and an empty failed_generation, on exactly
# the harder inputs a classifier most needs to get right.
#
# A cap only bills what is actually generated, so headroom is close to free. The hard
# timeout, not this number, is what bounds how long a user waits.
#
# Groq also accepts reasoning_effort="low", which is roughly four times cheaper but
# measurably less accurate here: it read "2:15 on Monday" as 02:15 rather than 14:15.
# For appointment times that trade is not worth making.
DEFAULT_MAX_TOKENS: Final = 1024


class LLMError(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


def _describe_api_error(exc: openai.APIStatusError) -> str:
    """Summarise an API rejection using the provider's own code and message.

    Worth the few lines: a bare "HTTP 400" hides which of a dozen causes fired. The
    code that matters here is json_validate_failed, which means the model wrote no
    parseable answer -- usually because reasoning tokens exhausted max_tokens.

    Deliberately omits ``failed_generation``: it echoes model output, which can carry
    whatever the user typed. Callers that want it should log it under a redacted key.
    """
    body = exc.body if isinstance(exc.body, dict) else {}
    # Groq nests under "error"; some gateways return the fields at the top level.
    nested = body.get("error")
    error = nested if isinstance(nested, dict) else body
    code = error.get("code")
    detail = str(error.get("message", ""))[:200] or "no detail"
    return f"{code}: {detail}" if code else detail


def strip_code_fences(raw: str) -> str:
    """Unwrap ```json ... ``` if the model added it despite JSON mode.

    Shared by both LLM layers: JSON mode should make this unnecessary, but models
    still wrap output often enough that six defensive lines are cheaper than the
    intermittent parse failures they prevent.
    """
    text = raw.strip()
    if not text.startswith("```"):
        return text
    body = text[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    return body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()


class GroqClient:
    """JSON-mode chat completions with a hard timeout and one retry."""

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        base_url: str,
        timeout: float = 12.0,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # see module docstring
        )
        self.call_count = 0  # lets tests assert a path made zero LLM calls

    @property
    def model(self) -> str:
        return self._model

    async def complete_json(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Return the raw JSON string the model produced.

        Parsing and validation are the caller's job: on a schema failure the caller
        still needs the raw text to log alongside the error.
        """
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.call_count += 1
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0,  # classification must be reproducible
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens,
                    timeout=self._timeout,
                )
            except (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.RateLimitError,
            ) as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    await self._sleep_backoff(attempt, exc)
                    continue
                raise LLMError(f"transient failure after {attempt} attempts") from exc
            except openai.APIStatusError as exc:
                # 5xx is worth one retry; a 4xx would be refused identically.
                if exc.status_code >= 500 and attempt < MAX_ATTEMPTS:
                    last_error = exc
                    await self._sleep_backoff(attempt, exc)
                    continue
                raise LLMError(
                    f"API rejected the request (HTTP {exc.status_code}): "
                    f"{_describe_api_error(exc)}"
                ) from exc

            content = (response.choices[0].message.content or "").strip() if response.choices else ""
            if not content:
                raise LLMError("model returned an empty response")
            return content

        raise LLMError("exhausted attempts") from last_error

    async def _sleep_backoff(self, attempt: int, exc: Exception) -> None:
        # Jitter so concurrent chats hitting a rate limit do not retry in lockstep.
        delay = RETRY_BACKOFF_SECONDS * attempt * (1 + random.random() * 0.25)
        logger.warning(
            "llm.retrying",
            extra={
                "attempt": attempt,
                "error": type(exc).__name__,
                "retry_in_s": round(delay, 2),
            },
        )
        await asyncio.sleep(delay)

    async def aclose(self) -> None:
        await self._client.close()
