"""GroqClient: retry policy, token budget, and error reporting.

The token budget has a test because it is the one constant whose wrongness is
invisible locally -- every unit test fakes the model, so a cap too small for a
reasoning model's hidden tokens only fails against the real API, intermittently, on
the hardest inputs.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from app.agent.llm import (
    DEFAULT_MAX_TOKENS,
    MAX_ATTEMPTS,
    GroqClient,
    LLMError,
    _describe_api_error,
    strip_code_fences,
)


def status_error(status: int, body: dict | None = None) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body or {})
    return openai.APIStatusError("boom", response=response, body=body)


# ------------------------------------------------------------- token budget

def test_the_default_token_budget_leaves_room_for_reasoning() -> None:
    """Measured reasoning on this task reached 376 tokens before any answer was
    written. A cap near that produces HTTP 400 json_validate_failed on live calls."""
    assert DEFAULT_MAX_TOKENS >= 1024


# --------------------------------------------------------- error reporting

def test_a_json_validate_failure_names_itself() -> None:
    """A bare 'HTTP 400' hides which of a dozen causes fired."""
    exc = status_error(
        400,
        {
            "error": {
                "code": "json_validate_failed",
                "message": "Failed to validate JSON.",
                "failed_generation": "",
            }
        },
    )

    assert "json_validate_failed" in _describe_api_error(exc)


def test_error_description_omits_failed_generation() -> None:
    """It echoes model output, which can carry whatever the user typed."""
    exc = status_error(
        400,
        {
            "error": {
                "code": "json_validate_failed",
                "message": "Failed to validate JSON.",
                "failed_generation": "patient@example.com wants 3pm",
            }
        },
    )

    described = _describe_api_error(exc)

    assert "patient@example.com" not in described
    assert "wants 3pm" not in described


def test_error_description_survives_an_unexpected_body_shape() -> None:
    assert _describe_api_error(status_error(500)) == "no detail"
    # Some gateways return the fields at the top level rather than nested under "error".
    assert "overloaded" in _describe_api_error(status_error(503, {"message": "overloaded"}))
    assert "no detail" in _describe_api_error(status_error(500, {"error": "a string"}))


def test_a_long_api_message_is_truncated() -> None:
    exc = status_error(400, {"error": {"code": "x", "message": "y" * 5000}})

    assert len(_describe_api_error(exc)) < 250


# ----------------------------------------------------------- retry policy

async def test_a_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """It would be refused identically the second time."""
    from pydantic import SecretStr

    client = GroqClient(SecretStr("k"), "m", "https://example.invalid")

    async def always_400(**_: object) -> object:
        raise status_error(400, {"error": {"code": "json_validate_failed"}})

    monkeypatch.setattr(client._client.chat.completions, "create", always_400)

    with pytest.raises(LLMError, match="json_validate_failed"):
        await client.complete_json("sys", "user")

    assert client.call_count == 1


async def test_a_5xx_is_retried_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    client = GroqClient(SecretStr("k"), "m", "https://example.invalid")

    async def always_500(**_: object) -> object:
        raise status_error(500, {"error": {"code": "server_error"}})

    monkeypatch.setattr(client._client.chat.completions, "create", always_500)
    monkeypatch.setattr("app.agent.llm.RETRY_BACKOFF_SECONDS", 0.0)

    with pytest.raises(LLMError):
        await client.complete_json("sys", "user")

    assert client.call_count == MAX_ATTEMPTS == 2


# -------------------------------------------------------------- fence stripping

@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('  {"a": 1}  ', '{"a": 1}'),
    ],
)
def test_code_fences_are_stripped(raw: str, expected: str) -> None:
    assert strip_code_fences(raw) == expected
