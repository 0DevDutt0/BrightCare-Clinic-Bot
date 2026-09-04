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


class LLMError(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


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
        max_tokens: int = 300,
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
                raise LLMError(f"API rejected the request (HTTP {exc.status_code})") from exc

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
