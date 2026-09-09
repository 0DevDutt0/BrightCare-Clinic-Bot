"""One-time codes, for the one action the bot cannot undo.

Cancelling an appointment is destructive, and the only thing tying the person typing
into a Telegram chat to the booking they are asking about is an email address they typed
a moment earlier -- which is a claim, not proof. A code delivered to that address turns
the claim into proof: whoever answers with it controls the inbox the appointment was
booked from.

Three properties carry that weight:

*The code comes from* ``secrets``, *never* ``random``. A predictable code proves nothing
about who is holding it.

*Only a salted hash is kept.* The plaintext exists in one local variable, long enough to
be written into an email, and is then dropped -- so a state dump, a Redis snapshot or a
traceback carries no live code. Six digits is a small space, and a salted hash of one is
brute-forceable in milliseconds by anyone who already holds the state; this defends
against the code being *seen*, not against an attacker who owns the process.

*The budget that matters is on attempts, not on time.* Three wrong entries end the
challenge, which puts a blind guess at three in a million. The ten-minute expiry bounds
something different: how long a code stays useful to whoever reads the mailbox later.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# Six digits is the length people expect and will retype without complaint. The strength
# comes from MAX_ATTEMPTS below, not from the length.
OTP_DIGITS = 6

# Long enough for SMTP and a spam folder detour; short enough that a code left sitting in
# an inbox stops working before it becomes interesting.
OTP_TTL = timedelta(minutes=10)

# Three guesses against 10^6 possibilities. This, not the hash, is what makes the code
# unguessable in practice.
MAX_ATTEMPTS = 3

# The first send plus two resends. Codes are only ever sent to an address that already
# has an appointment, so this is not an open relay -- but it still bounds how much mail
# one conversation can cause.
MAX_SENDS = 3

_NON_DIGITS = re.compile(r"\D+")


class OtpVerdict(str, Enum):
    """Why a typed code was or was not accepted."""

    OK = "ok"
    WRONG = "wrong"            # incorrect, and attempts remain
    EXHAUSTED = "exhausted"    # the attempt budget is spent; the challenge is dead
    EXPIRED = "expired"        # past its window, whatever was typed


def read_code(text: str) -> str | None:
    """Pull a code out of a typed reply, or None when there isn't one.

    People send "123456", "123 456" and "my code is 123456". Digits are collected and
    the length is then checked exactly: a 5- or 7-digit result is a typo, and guessing
    which digit to drop would spend one of only three attempts on the guess.
    """
    digits = _NON_DIGITS.sub("", text)
    return digits if len(digits) == OTP_DIGITS else None


def _digest(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


class OtpChallenge(BaseModel):
    """A live code, stored as a hash, with its clock and its budget.

    Held in :class:`~app.state.models.ConversationState`, so it is serialised wherever
    that is. Nothing here reveals the code.
    """

    salt: str
    digest: str
    expires_at: datetime
    attempts: int = Field(default=0, ge=0)

    @field_validator("expires_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("expires_at must be timezone-aware")
        return value

    @property
    def remaining_attempts(self) -> int:
        return max(MAX_ATTEMPTS - self.attempts, 0)

    def verify(self, code: str, now: datetime) -> OtpVerdict:
        """Check one typed code, spending an attempt if it is wrong.

        Mutating on failure is the point: the counter has to survive the turn, and a
        caller that forgot to increment it would hand out unlimited guesses. A *correct*
        code costs nothing, so a retry after a calendar failure does not eat the budget.

        Expiry is checked before the comparison, so a stale code is never even compared.
        """
        if now >= self.expires_at:
            return OtpVerdict.EXPIRED
        if self.attempts >= MAX_ATTEMPTS:
            return OtpVerdict.EXHAUSTED
        # Constant-time: a timing difference would leak the code a digit at a time.
        if secrets.compare_digest(self.digest, _digest(self.salt, code)):
            return OtpVerdict.OK
        self.attempts += 1
        return OtpVerdict.EXHAUSTED if self.attempts >= MAX_ATTEMPTS else OtpVerdict.WRONG


def issue(now: datetime) -> tuple[OtpChallenge, str]:
    """Mint a challenge, returning it with the plaintext code.

    The code is returned rather than stored because this is the only moment it exists:
    the caller puts it in an email and lets the local variable fall out of scope. Never
    write the second element of this tuple into state, a log, or a reply.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("issue requires a timezone-aware datetime")

    # randbelow, not randint or choice: uniform over the whole space, from a CSPRNG.
    # Zero-padded, so 000042 is as likely as any other code.
    code = f"{secrets.randbelow(10 ** OTP_DIGITS):0{OTP_DIGITS}d}"
    salt = secrets.token_hex(16)
    challenge = OtpChallenge(
        salt=salt, digest=_digest(salt, code), expires_at=now + OTP_TTL
    )
    return challenge, code
