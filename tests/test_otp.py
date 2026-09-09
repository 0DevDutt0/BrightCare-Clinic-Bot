"""One-time codes: generation, the attempt budget, expiry, and what is never stored.

The last of those is the one worth a test rather than a comment. A code that survives
into serialised state is a code that survives into a Redis snapshot, a log line and a
traceback, and no amount of care at the call site puts it back.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.otp import (
    MAX_ATTEMPTS,
    OTP_DIGITS,
    OTP_TTL,
    OtpChallenge,
    OtpVerdict,
    issue,
    read_code,
)

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 4, 11, 30, tzinfo=TZ)


# ------------------------------------------------------------------ generation

def test_a_code_is_six_digits() -> None:
    _, code = issue(NOW)

    assert len(code) == OTP_DIGITS
    assert code.isdigit()


def test_low_codes_are_zero_padded_not_shortened() -> None:
    """randbelow can return 42; "42" would be a code nobody could type correctly."""
    for _ in range(200):
        _, code = issue(NOW)
        assert len(code) == OTP_DIGITS


def test_codes_are_not_repeated() -> None:
    """A weak sample, but it catches the failure that matters: a constant."""
    codes = {issue(NOW)[1] for _ in range(50)}

    assert len(codes) > 40


def test_the_challenge_never_carries_the_code() -> None:
    """Serialised state is the blast radius: Redis, logs, tracebacks, model dumps."""
    challenge, code = issue(NOW)

    dumped = challenge.model_dump_json()

    assert code not in dumped
    assert code not in repr(challenge)


def test_two_challenges_for_the_same_code_hash_differently() -> None:
    """A per-challenge salt, so one recovered digest does not unlock another."""
    first, _ = issue(NOW)
    second = OtpChallenge(
        salt=first.salt, digest=first.digest, expires_at=first.expires_at
    )
    third, _ = issue(NOW)

    assert second.digest == first.digest        # same salt, same input
    assert third.salt != first.salt


def test_the_window_starts_from_the_moment_given() -> None:
    challenge, _ = issue(NOW)

    assert challenge.expires_at == NOW + OTP_TTL


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        issue(datetime(2026, 9, 4, 11, 30))


# ---------------------------------------------------------------- verification

def test_the_right_code_verifies() -> None:
    challenge, code = issue(NOW)

    assert challenge.verify(code, NOW) is OtpVerdict.OK


def test_a_correct_code_costs_no_attempt() -> None:
    """So retrying after a calendar failure does not eat into the budget."""
    challenge, code = issue(NOW)

    challenge.verify(code, NOW)
    challenge.verify(code, NOW)

    assert challenge.attempts == 0
    assert challenge.verify(code, NOW) is OtpVerdict.OK


def test_a_wrong_code_spends_one_attempt() -> None:
    challenge, code = issue(NOW)
    wrong = f"{(int(code) + 1) % 10 ** OTP_DIGITS:0{OTP_DIGITS}d}"

    assert challenge.verify(wrong, NOW) is OtpVerdict.WRONG
    assert challenge.attempts == 1
    assert challenge.remaining_attempts == MAX_ATTEMPTS - 1


def test_the_budget_runs_out_and_stays_out() -> None:
    """Three guesses against a million. After that the right code is too late."""
    challenge, code = issue(NOW)

    verdicts = [challenge.verify("000000" if code != "000000" else "111111", NOW)
                for _ in range(MAX_ATTEMPTS)]

    assert verdicts[-1] is OtpVerdict.EXHAUSTED
    assert challenge.remaining_attempts == 0
    assert challenge.verify(code, NOW) is OtpVerdict.EXHAUSTED


def test_expiry_beats_a_correct_code() -> None:
    challenge, code = issue(NOW)

    assert challenge.verify(code, NOW + OTP_TTL + timedelta(seconds=1)) is (
        OtpVerdict.EXPIRED
    )


def test_the_expiry_boundary_is_closed() -> None:
    """At exactly the expiry instant the code is already gone -- no off-by-one grace."""
    challenge, code = issue(NOW)

    assert challenge.verify(code, NOW + OTP_TTL) is OtpVerdict.EXPIRED
    assert challenge.verify(code, NOW + OTP_TTL - timedelta(seconds=1)) is OtpVerdict.OK


def test_expiry_is_checked_before_the_attempt_is_spent() -> None:
    """A stale code is not a guess, so it must not cost one."""
    challenge, _ = issue(NOW)

    challenge.verify("000000", NOW + OTP_TTL + timedelta(minutes=1))

    assert challenge.attempts == 0


def test_a_naive_expiry_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OtpChallenge(salt="s", digest="d", expires_at=datetime(2026, 9, 4, 11, 40))


# ------------------------------------------------------------------- parsing

@pytest.mark.parametrize(
    "text, expected",
    [
        ("123456", "123456"),
        ("  123456  ", "123456"),
        ("123 456", "123456"),
        ("123-456", "123456"),
        ("my code is 123456", "123456"),
        ("its 123456 thanks", "123456"),
        ("000042", "000042"),
    ],
)
def test_a_code_is_found_however_it_is_typed(text: str, expected: str) -> None:
    assert read_code(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "resend",
        "12345",        # a digit short -- a typo, not a guess
        "1234567",      # a digit over
        "yes",
        "dev@example.com",
        "I don't have it",
    ],
)
def test_a_reply_that_is_not_a_code_is_not_guessed_at(text: str) -> None:
    """Padding or trimming to six digits would spend one of three attempts on a guess
    the user never made."""
    assert read_code(text) is None
