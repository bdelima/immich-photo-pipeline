"""\"Undo\" takes back the last adjustment(s) by re-running the recipe with the
earlier ones only. It used to be read as just another instruction, so the
photo was redone from the original with the word \"undo\" as the request."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import CommentIntent, RecipeResult
from tests.test_rules import handle, make, state_with


def revise_twice(tmp_path, **kw):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("revise"), **kw)
    state, img = state_with()
    handle(pipeline, state, img, "swap the first two photos")
    handle(pipeline, state, img, "make the mat darker", cid="c2")
    return pipeline, immich, recipe, rules, state, img


def test_undo_reruns_with_all_but_the_last_adjustment(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    pipeline.recipe.verdict = CommentIntent("undo", steps=1)
    handle(pipeline, state, img, "undo", cid="c3")
    note = recipe.single_calls[2]["note"]
    assert "1) swap the first two photos" in note and "darker" not in note and "undo" not in note
    assert note.endswith("Apply exactly those and nothing else.")
    assert img.revision_notes == ["swap the first two photos"]


def test_undo_says_what_it_is_doing_and_what_it_did(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    before = len(immich.posted)
    pipeline.recipe.verdict = CommentIntent("undo", steps=1)
    handle(pipeline, state, img, "undo", cid="c3")
    texts = [t for t, _, _ in immich.posted[before:]]
    assert texts[0].startswith("Got it: undoing make the mat darker.")
    assert texts[1].startswith("Undid: make the mat darker.")
    assert immich.posted[before + 1][2] == "new-asset"  # the result goes on the new version


def test_undoing_everything_processes_the_photo_as_it_first_was(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    pipeline.recipe.verdict = CommentIntent("undo", steps=2)
    handle(pipeline, state, img, "start over", cid="c3")
    assert recipe.single_calls[2]["note"] is None
    assert img.revision_notes == []


def test_more_steps_than_there_are_is_clamped(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    pipeline.recipe.verdict = CommentIntent("undo", steps=9)
    handle(pipeline, state, img, "undo everything", cid="c3")
    assert img.revision_notes == [] and recipe.single_calls[2]["note"] is None


def test_undo_twice_goes_back_two_steps(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    pipeline.recipe.verdict = CommentIntent("undo", steps=1)
    handle(pipeline, state, img, "undo", cid="c3")
    handle(pipeline, state, img, "undo", cid="c4")
    assert img.revision_notes == [] and recipe.single_calls[3]["note"] is None


def test_a_new_request_after_an_undo_builds_on_what_is_left(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    pipeline.recipe.verdict = CommentIntent("undo", steps=1)
    handle(pipeline, state, img, "undo", cid="c3")
    pipeline.recipe.verdict = CommentIntent("revise")
    handle(pipeline, state, img, "make the mat lighter", cid="c4")
    note = recipe.single_calls[3]["note"]
    assert "1) swap the first two photos" in note and "darker" not in note and note.endswith("make the mat lighter")
    assert img.revision_notes == ["swap the first two photos", "make the mat lighter"]


def test_nothing_to_undo_says_so_and_runs_nothing(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path, verdict=CommentIntent("undo", steps=1))
    state, img = state_with()
    handle(pipeline, state, img, "undo")
    assert recipe.single_calls == [] and immich.deleted == [] and "c1" in img.acted_comment_ids
    assert any("don't have any changes recorded" in t for t, _, _ in immich.posted)


def test_an_undo_never_proposes_a_rule(tmp_path):
    result = RecipeResult(status="done", output_path="/tmp/out.jpg", lesson="Keep mats pale.", lesson_scope="all")
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path, result=result)
    rules_before = len(rules.all())
    pipeline.recipe.verdict = CommentIntent("undo", steps=1)
    handle(pipeline, state, img, "undo", cid="c3")
    assert len(rules.all()) == rules_before
    assert not any("Should I remember" in t for t, _, _ in immich.posted[-2:])


def test_the_interpreter_is_told_what_has_been_applied(tmp_path):
    pipeline, immich, recipe, rules, state, img = revise_twice(tmp_path)
    seen = []

    def interpret(text, ctx=None):
        seen.append(ctx)
        return CommentIntent("none")

    pipeline.recipe.interpret_comment = interpret
    handle(pipeline, state, img, "thanks", cid="c3")
    assert seen[0].history == ["swap the first two photos", "make the mat darker"]
