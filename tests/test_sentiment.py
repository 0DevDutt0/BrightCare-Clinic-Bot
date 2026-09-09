"""Yes/no parsing for flow replies.

Deterministic on purpose: spending a model call to read "yes" would double the cost
of every booking and add a failure mode to the least ambiguous message in the
conversation.

The interesting cases are the ones that must return None. A reply that opens with an
affirmative but carries content the bot did not offer ("ok Tuesday") is a changed
request, and confirming the *old* slot on it would book the wrong appointment.

The second interesting case is "cancel", which means opposite things in the two flows.
The last section pins both readings: getting it backwards in a cancellation would either
refuse one the user asked for, or perform one they didn't.
"""

from __future__ import annotations

import pytest

from app.agent.parsing import CANCEL_IS_NEUTRAL, read_yes_no


def _sentiment(text: str) -> bool | None:
    """The booking flow's reading: nothing is neutral."""
    return read_yes_no(text)


def _cancel_sentiment(text: str) -> bool | None:
    """The cancellation flow's reading: "cancel" decides nothing on its own."""
    return read_yes_no(text, neutral=CANCEL_IS_NEUTRAL)


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


# ------------------------------------------- "cancel" means opposite things per flow

@pytest.mark.parametrize(
    "text, booking, cancelling",
    [
        # In a booking, "cancel it" refuses. In a cancellation it is the thing being
        # asked for -- and, alone, still not an answer, so it must not decide either way.
        ("cancel", False, None),
        ("cancel it", False, None),
        ("cancel that", False, None),
        # "yes cancel it" is the sharpest case: a refusal in a booking, because a
        # negative anywhere wins there -- and a plain yes in a cancellation.
        ("yes cancel it", False, True),
        # A real negative decides the same way in both.
        ("no dont cancel", False, False),
        ("no, cancel it", False, False),
    ],
)
def test_cancel_reads_differently_inside_a_cancellation(
    text: str, booking: bool | None, cancelling: bool | None
) -> None:
    assert _sentiment(text) is booking
    assert _cancel_sentiment(text) is cancelling


def test_neutralising_a_word_does_not_make_it_unrecognised() -> None:
    """The cheap fix -- dropping "cancel" from the known words -- loses real answers.

    It would turn "yes cancel it" and "no don't cancel" into None alongside the genuinely
    ambiguous "cancel it", throwing away two answers the user gave plainly.
    """
    assert _cancel_sentiment("yes cancel it") is True
    assert _cancel_sentiment("no dont cancel") is False
    assert _cancel_sentiment("cancel it") is None
