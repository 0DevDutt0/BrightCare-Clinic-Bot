"""Booking intent -- Phase 1 stub.

Two entry points, matching the two ways a booking message arrives:

``booking_reply``          a fresh request while the conversation is idle.
``continue_booking_flow``  a reply inside an active flow, reached only when
                           ``state.stage`` is not ``idle``.

The stub deliberately leaves ``stage`` at ``idle``. Advancing it here would make the
*next* unrelated message ("hi") route into a flow that cannot do anything yet, which
would break the demo without testing anything real. Phase 3 sets the stage at the
point it actually proposes a slot; the non-idle routing path is exercised by tests
that set the stage directly, so the wiring is covered without faking a live flow.
"""

from __future__ import annotations

from app.state.models import ConversationState

NOT_WIRED_YET = "Booking isn't wired up yet."


def booking_reply(state: ConversationState, raw_datetime_text: str | None) -> str:
    """Acknowledge the request, echoing the time phrase back for confirmation.

    Echoing verbatim is the cheapest way for the user to catch a misread before
    Phase 2 resolves the phrase to an actual slot.
    """
    state.raw_datetime_text = raw_datetime_text
    state.touch()

    if raw_datetime_text:
        return (
            f"Got it - you'd like an appointment for \"{raw_datetime_text}\". "
            f"{NOT_WIRED_YET}"
        )
    return (
        "Happy to help you book an appointment. "
        f"What day and time suit you? {NOT_WIRED_YET}"
    )


def continue_booking_flow(state: ConversationState) -> str:
    """Placeholder for an in-progress flow.

    Reached when ``state.stage`` is not ``idle``: the user is answering a question the
    bot asked, so the message must not be re-classified as a fresh intent. Phase 3 and
    Phase 4 fill in the per-stage behaviour.
    """
    return (
        "Thanks - I've noted that. The booking flow isn't wired up yet, "
        "so I can't take it further right now."
    )
