"""Yes/no parsing for flow replies.

Deterministic on purpose: spending a model call to read "yes" would double the cost
of every booking and add a failure mode to the least ambiguous message in the
conversation.

The interesting cases are the ones that must return None. A reply that opens with an
affirmative but carries content the bot did not offer ("ok Tuesday") is a changed
request, and confirming the *old* slot on it would book the wrong appointment.
"""

from __future__ import annotations

import pytest

from app.agent.handlers.booking import _sentiment


@pytest.mark.parametrize(
    "text",
    [
        "yes", "Yes", "YES", "yes.", "yes!", "  yes  ",
        "yeah", "yep", "yup", "sure", "ok", "okay", "y",
        "yes please", "yeah go ahead", "sounds good", "that works",
        "perfect", "book it", "confirm", "go ahead", "great, book it",
        "ok that works", "fine", "yes that's right",
    ],
)
def test_affirmatives(text: str) -> None:
    assert _sentiment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "no", "No", "no.", "nope", "nah", "n",
        "no thanks", "no thank you", "not really", "cancel", "stop",
        "no, cancel it",
    ],
)
def test_negatives(text: str) -> None:
    assert _sentiment(text) is False


@pytest.mark.parametrize(
    "text",
    [
        # Opens affirmative but names a time the bot never proposed.
        "ok Tuesday",
        "sure, but 4pm instead",
        "yes but can we do Wednesday",
        # Opens negative but proposes an alternative -- the alternative matters more.
        "no, how about 4pm",
        "not Monday, Tuesday please",
        # Plainly a different request.
        "actually can we do 4pm?",
        "where are you located?",
        "what about parking",
        # Nothing to go on.
        "",
        "   ",
        "hmm",
        "12345",
    ],
)
def test_neither_yes_nor_no(text: str) -> None:
    """These are handed back to the router rather than answered as a yes or a no."""
    assert _sentiment(text) is None


def test_a_negative_anywhere_beats_an_affirmative() -> None:
    """"no thanks" must not be read as thanks."""
    assert _sentiment("no thanks") is False
    assert _sentiment("thanks no") is False


def test_a_long_message_is_never_a_bare_yes() -> None:
    assert _sentiment("yes yes yes yes yes yes yes") is None
