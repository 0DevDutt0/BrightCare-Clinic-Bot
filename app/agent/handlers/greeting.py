"""Greeting intent: acknowledge, then say what the bot is for."""

from __future__ import annotations

from app.domain.business import CAPABILITIES, CLINIC_NAME


def greeting_reply() -> str:
    """A greeting is only useful if it also tells the user what to ask next."""
    return f"Hello! You've reached {CLINIC_NAME}. {CAPABILITIES}"
