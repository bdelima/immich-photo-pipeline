"""A reply to the recipe's own question about a photo ("which way should I
recenter?") is the answer to it. It used to be taken as the name of an album
to promote the photo to, because both questions shared one flag."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import CommentIntent, RecipeResult
from tests.test_rules import FakeImmich, FakeRecipe, handle, make, state_with


def test_recipe_question_is_remembered_with_the_request(tmp_path):
    result = RecipeResult(status="needs_clarification", question="Recenter on the dog or the house?")
    pipeline, immich, recipe, rules = make(tmp_path, result=result)
    state, img = state_with()
    handle(pipeline, state, img, "recenter this")
    assert img.awaiting_clarification and not img.awaiting_album
    assert (img.clarification_note, img.clarification_question) == ("recenter this", "Recenter on the dog or the house?")


def test_the_reply_answers_the_question_instead_of_naming_an_album(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path)
    state, img = state_with(awaiting=True)
    img.clarification_note, img.clarification_question = "recenter this", "On the dog or the house?"
    handle(pipeline, state, img, "on the dog", cid="c2")
    assert state.watched_albums == {} and img.home == "review"
    assert "recenter this" in recipe.single_calls[0]["note"] and "on the dog" in recipe.single_calls[0]["note"]
    assert not img.awaiting_clarification and img.clarification_note is None
    assert any(t == "Applied: on the dog" for t, _, _ in immich.posted)


def test_unanswered_question_keeps_waiting_and_asks_again_if_needed(tmp_path):
    result = RecipeResult(status="needs_clarification", question="Still unclear: left or right?")
    pipeline, immich, recipe, rules = make(tmp_path, result=result)
    state, img = state_with(awaiting=True)
    img.clarification_note, img.clarification_question = "recenter this", "First question?"
    handle(pipeline, state, img, "somewhere", cid="c2")
    assert img.awaiting_clarification and img.clarification_question == "Still unclear: left or right?"
    assert state.watched_albums == {}


def test_album_question_still_takes_the_reply_as_the_album_name(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path)
    created = []
    immich.create_album = lambda name: created.append(name) or "album-1"
    immich.remove_assets_from_album = lambda album_id, ids: None
    state, img = state_with(awaiting=True)
    img.awaiting_album = True
    handle(pipeline, state, img, "Holiday", cid="c2")
    assert created == ["Holiday"] and img.home == "Holiday"
    assert not img.awaiting_clarification and not img.awaiting_album
    assert recipe.single_calls == []


def test_asking_which_album_marks_the_album_question(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path)
    state, img = state_with()
    pipeline._ask_which_album(state, "L", "a1")
    assert img.awaiting_clarification and img.awaiting_album


def test_state_saved_before_the_split_still_means_the_album_question(tmp_path):
    pipeline, immich, recipe, rules = make(tmp_path)
    immich.create_album = lambda name: "album-1"
    immich.remove_assets_from_album = lambda album_id, ids: None
    state, img = state_with(awaiting=True)  # old flag only, no awaiting_album, no note
    handle(pipeline, state, img, "Holiday", cid="c2")
    assert img.home == "Holiday" and recipe.single_calls == []
