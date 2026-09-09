"""Reading a yes, a no, or an email address out of a free-text reply.

Deterministic on purpose. Spending a model call to read "yes" would double the cost of
every booking and add a failure mode to the least ambiguous message in the conversation.

Both flows share this module, and the one word they disagree about is "cancel". In a
booking, "cancel it" means *don't book* -- a plain no. In a cancellation, the same two
words mean *do the thing I asked for*. A single word list cannot be right for both, so
the caller passes the words that must be read as carrying no decision; see ``neutral``.
"""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

AFFIRMATIVE_WORDS = frozenset(
    {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
     "book", "please", "perfect", "great", "correct", "right", "good", "works",
     "fine", "ahead"}
)
NEGATIVE_WORDS = frozenset(
    {"no", "n", "nope", "nah", "cancel", "dont", "stop", "never", "not"}
)
# Words that carry no decision but commonly pad one: "yeah go ahead", "no thanks".
FILLER_WORDS = frozenset(
    {"thanks", "thank", "you", "that", "thats", "sounds", "go", "it", "then",
     "lets", "do", "and", "me", "sound", "is", "one", "really", "all"}
)
KNOWN_WORDS = AFFIRMATIVE_WORDS | NEGATIVE_WORDS | FILLER_WORDS

# What the cancel/reschedule flow passes as ``neutral``: inside it, "cancel" is the
# subject of the conversation rather than a refusal, so it decides nothing on its own.
CANCEL_IS_NEUTRAL = frozenset({"cancel"})

# Longer than this and the message is making a request, not answering yes or no.
MAX_DECISION_WORDS = 5

# Apostrophes are dropped before matching so contractions normalise onto the word lists:
# "that's" -> thats, "don't" -> dont, "let's" -> lets.
_APOSTROPHES = str.maketrans("", "", "'’")
_WORD = re.compile(r"[a-z]+")
_EMAIL_CANDIDATE = re.compile(r"[^\s<>,;]+@[^\s<>,;]+")


def read_yes_no(text: str, *, neutral: frozenset[str] = frozenset()) -> bool | None:
    """True for yes, False for no, None when the reply answers neither.

    Every word must be recognised before a verdict is returned. That is what keeps
    "ok Tuesday" out of the yes bucket: it opens with an affirmative but carries a day
    the bot did not propose, so it belongs to the router, not to this question. "yeah go
    ahead" is entirely known words, so it is a yes.

    A negative anywhere wins, so "no thanks" cannot be read as thanks.

    ``neutral`` moves words out of the deciding sets without making them unrecognised.
    With ``{"cancel"}``: "cancel it" is None rather than a no, "yes cancel it" is still a
    yes, and "no don't cancel" is still a no. Making the word *unknown* instead would
    turn all three into None, which loses two answers the user clearly gave.
    """
    words = _WORD.findall(text.lower().translate(_APOSTROPHES))
    if not words or len(words) > MAX_DECISION_WORDS:
        return None

    unique = set(words)
    if not unique <= (KNOWN_WORDS | neutral):
        return None
    if unique & (NEGATIVE_WORDS - neutral):
        return False
    if unique & (AFFIRMATIVE_WORDS - neutral):
        return True
    return None


def extract_email(text: str) -> str | None:
    """Pull a valid address out of a message, or None.

    Validated with email-validator rather than a regex: the regex only finds the
    candidate. A wrong address means the confirmation silently never arrives -- and, in
    a cancellation, that no appointment is ever found under it.
    """
    for candidate in _EMAIL_CANDIDATE.findall(text):
        try:
            return validate_email(candidate, check_deliverability=False).normalized
        except EmailNotValidError:
            continue
    return None
