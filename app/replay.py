"""How earlier instructions are restated to the recipe.

Every revision re-runs the recipe from the original photo(s), so the
instructions already given have to be replayed in the prompt or they are
lost. (Reverting to an earlier step does not use this at all: it just
points the photo at the revision that was saved at that step.)
"""
from __future__ import annotations

# How many earlier instructions are replayed (the most recent ones), and how
# much of each is kept. Bounds the prompt for a photo adjusted many times.
MAX_REVISION_NOTES = 15
MAX_REVISION_NOTE_CHARS = 400


def note_with_history(history: list[str], instruction: str) -> str:
    """The instruction for a recipe run, with the adjustments already made
    to this photo in front of it. An instruction like "swap the first two"
    only makes sense relative to the arrangement the earlier ones produced.
    With no history the instruction is passed through unchanged."""
    if not history:
        return instruction
    steps = " ".join(f"{i}) {note}" for i, note in enumerate(history, 1))
    return (
        "This image has already been adjusted at the reviewer's request, in this order, "
        f"each on top of the previous: {steps} "
        "Reproduce all of those adjustments (a rearranged or swapped layout stays as "
        "arranged unless a later step changes it), then apply this new request on top of "
        f"the result: {instruction}"
    )


def replay_note(history: list[str]) -> str | None:
    """The instruction for a re-run that should reproduce exactly the
    adjustments in `history` and nothing more. None when there is nothing to
    replay: the photo is processed as it was the first time."""
    if not history:
        return None
    steps = " ".join(f"{i}) {note}" for i, note in enumerate(history, 1))
    return (
        "This image is being redone at the reviewer's request with these adjustments, in this "
        f"order, each on top of the previous: {steps} "
        "Apply exactly those and nothing else."
    )


def bounded_history(history: list[str]) -> list[str]:
    """The most recent instructions, each cut to a sane length."""
    return [h[:MAX_REVISION_NOTE_CHARS] for h in history][-MAX_REVISION_NOTES:]
