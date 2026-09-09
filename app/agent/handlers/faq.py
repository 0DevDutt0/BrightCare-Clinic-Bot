"""FAQ intent: answer strictly from domain constants.

The fact is returned verbatim from :data:`app.domain.business.FAQ_FACTS` rather than
handed to the model to rephrase. Both are allowed by the spec; this one is chosen
because clinic facts are the thing most damaging to get wrong, and a template makes
invention structurally impossible instead of merely discouraged. It also keeps an FAQ
answer at exactly one model call -- the classification -- with no added latency.

To switch to model phrasing later, call the LLM here with the selected fact as the
only permitted source; the classifier and dispatch stay unchanged.
"""

from __future__ import annotations

from app.domain.business import FAQ_FACTS

TOPIC_PROMPT = (
    "Happy to help. I can tell you about our location, opening hours, parking, "
    "walk-in policy, how long an appointment takes, or how to change one. "
    "Which would you like to know?"
)


def faq_reply(topic: str | None) -> str:
    """Answer a known topic, or ask which topic when the classifier could not tell."""
    if topic is None:
        return TOPIC_PROMPT
    fact = FAQ_FACTS.get(topic)
    if fact is None:
        # Defensive: the router already drops unknown topics.
        return TOPIC_PROMPT
    return fact
