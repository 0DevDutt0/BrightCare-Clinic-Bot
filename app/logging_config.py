"""Structured JSON logging with secret redaction.

Two independent protections, because either alone is brittle:

1. *Structural* -- callers never pass raw message bodies to INFO logs. Use
   :func:`text_fingerprint` to record the shape of a message (length and a short
   hash) instead of its content.
2. *Defensive* -- :class:`RedactingFilter` scrubs anything that slips through.
   Credential patterns are scrubbed at every level; email addresses are scrubbed
   from user-content fields at INFO and above, and left intact at DEBUG so a
   developer can still troubleshoot deliberately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from typing import Any

# Attributes the stdlib puts on every LogRecord. Anything else was passed by us via
# `extra=` and belongs in the structured output.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info taskName thread threadName""".split()
)

# Fields that may carry text a user typed. Emails are stripped from these at INFO+.
_USER_CONTENT_FIELDS = frozenset({"text", "user_text", "reply", "message_text"})

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Credential shapes that must never reach a log at any level.
_SECRET_RES = (
    re.compile(r"\d{6,12}:AA[\w-]{25,}"),                      # Telegram bot token
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),                       # Groq API key
    re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----",
               re.DOTALL),                                     # PEM block
    re.compile(r'"private_key"\s*:\s*"(?:[^"\\]|\\.)*"'),      # JSON private_key field
)

_REDACTED = "[REDACTED]"


def _scrub_secrets(value: str) -> str:
    for pattern in _SECRET_RES:
        value = pattern.sub(_REDACTED, value)
    return value


def text_fingerprint(text: str) -> dict[str, Any]:
    """Describe a message without revealing it: safe to log at INFO."""
    normalised = (text or "").strip()
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:8]
    return {"text_len": len(normalised), "text_sha8": digest}


class RedactingFilter(logging.Filter):
    """Scrub credentials always, and emails from user-content fields at INFO+."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_emails = record.levelno >= logging.INFO

        message = record.getMessage()
        scrubbed = _scrub_secrets(message)
        if redact_emails:
            scrubbed = _EMAIL_RE.sub(_REDACTED, scrubbed)
        if scrubbed != message:
            # Collapse args into the rendered message so the change survives formatting.
            record.msg = scrubbed
            record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _RESERVED or not isinstance(value, str):
                continue
            cleaned = _scrub_secrets(value)
            if redact_emails and key in _USER_CONTENT_FIELDS:
                cleaned = _EMAIL_RE.sub(_REDACTED, cleaned)
            if cleaned != value:
                setattr(record, key, cleaned)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `extra=` keys promoted to top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs a line per request at INFO, which would echo the bot token in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
