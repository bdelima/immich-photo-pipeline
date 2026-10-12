import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.replay import MAX_REVISION_NOTE_CHARS, MAX_REVISION_NOTES, bounded_history, note_with_history, replay_note


def test_the_first_instruction_is_passed_through_unchanged():
    assert note_with_history([], "darker") == "darker"


def test_a_later_instruction_replays_the_earlier_ones_in_order():
    text = note_with_history(["swap", "tighter gaps"], "darker")
    assert "1) swap 2) tighter gaps" in text and text.endswith("darker") and "stays as arranged" in text


def test_a_replay_reproduces_exactly_the_history():
    assert replay_note([]) is None
    text = replay_note(["swap", "darker"])
    assert "1) swap 2) darker" in text and text.endswith("Apply exactly those and nothing else.")


def test_only_the_most_recent_instructions_are_kept_and_each_is_cut():
    history = [f"step {i}" for i in range(MAX_REVISION_NOTES + 5)]
    kept = bounded_history(history)
    assert len(kept) == MAX_REVISION_NOTES and kept[-1] == history[-1] and kept[0] == "step 5"
    assert len(bounded_history(["x" * 1000])[0]) == MAX_REVISION_NOTE_CHARS
