"""Every revision re-runs the recipe from the original photo(s), so the
adjustments made so far must be replayed or a later one undoes them (seen
live: swap two photos in a collage, then ask for another change, and the
original arrangement came back)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline import MAX_REVISION_NOTES, _note_with_history
from app.recipe_runner import CommentIntent, RecipeResult
from app.state import ImageState, PipelineState, StateStore
from tests.test_rules import handle, make, state_with


def test_the_first_revision_is_passed_through_unchanged(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"))
    state, img = state_with()
    handle(pipeline, state, img, "swap the first two photos")
    assert recipe.single_calls[0]["note"] == "swap the first two photos"
    assert img.revision_notes == ["swap the first two photos"]


def test_a_later_revision_replays_the_earlier_ones_in_order(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"))
    state, img = state_with()
    handle(pipeline, state, img, "swap the first two photos")
    handle(pipeline, state, img, "make the mat darker", cid="c2")
    handle(pipeline, state, img, "tighten the gaps", cid="c3")
    third = recipe.single_calls[2]["note"]
    assert "1) swap the first two photos 2) make the mat darker" in third
    assert third.endswith("tighten the gaps")
    assert "swap" not in recipe.single_calls[1]["note"].split("1)")[0]
    assert img.revision_notes == ["swap the first two photos", "make the mat darker", "tighten the gaps"]


def test_collages_get_the_history_too(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"))
    state, img = state_with()
    img.source_asset_ids = ["s1", "s2"]
    handle(pipeline, state, img, "put the dog photo on the left")
    handle(pipeline, state, img, "darker mat", cid="c2")
    assert "1) put the dog photo on the left" in recipe.collage_calls[1]["note"]


def test_a_run_that_asks_a_question_records_nothing_yet(tmp_path):
    result = RecipeResult(status="needs_clarification", question="Which photo?")
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"), result=result)
    state, img = state_with()
    handle(pipeline, state, img, "swap them")
    assert img.revision_notes == [] and img.awaiting_clarification


def test_an_answered_question_is_recorded_as_one_adjustment_and_history_still_applies(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"))
    state, img = state_with()
    handle(pipeline, state, img, "make the mat darker")
    img.awaiting_clarification = True
    img.clarification_note, img.clarification_question = "swap them", "Which two?"
    pipeline.recipe.verdict = CommentIntent("answer")
    handle(pipeline, state, img, "the first and last", cid="c2")
    note = recipe.single_calls[1]["note"]
    assert "1) make the mat darker" in note and "swap them (You asked: Which two? The reviewer answered: the first and last)" in note
    assert img.revision_notes[-1].startswith("swap them (You asked")


def test_only_the_most_recent_adjustments_are_kept(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"))
    state, img = state_with()
    for i in range(MAX_REVISION_NOTES + 5):
        handle(pipeline, state, img, f"change {i}", cid=f"c{i}")
    assert len(img.revision_notes) == MAX_REVISION_NOTES
    assert img.revision_notes[-1] == f"change {MAX_REVISION_NOTES + 4}"


def test_history_survives_a_restart_and_old_state_still_loads(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = PipelineState(images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a", revision_notes=["swap"])})
    store.save(state)
    assert store.load().images["L"].revision_notes == ["swap"]
    # a state file written before this field existed
    import json
    raw = json.loads((tmp_path / "state.json").read_text())
    del raw["images"]["L"]["revision_notes"]
    (tmp_path / "state.json").write_text(json.dumps(raw))
    assert store.load().images["L"].revision_notes == []


def test_note_with_history_helper():
    assert _note_with_history([], "darker") == "darker"
    text = _note_with_history(["swap"], "darker")
    assert "1) swap" in text and text.endswith("darker") and "stays as arranged" in text
