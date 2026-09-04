"""Out-of-scope intent: decline and redirect, without attempting an answer.

This is a hard stop. The message is not passed to the model for a response, not
partially answered, and not hedged -- an assistant that answers weather questions
invites medical ones, which is the failure mode that actually matters for a clinic.
"""

from __future__ import annotations

from app.domain.business import CAPABILITIES


def out_of_scope_reply() -> str:
    return f"Sorry, I can't help with that one. {CAPABILITIES}"
