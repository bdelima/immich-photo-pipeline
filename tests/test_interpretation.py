"""How the pipeline acts on what the interpreter says a comment means."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import CommentIntent
from tests.test_rules import FakeImmich, handle, make, state_with


class Recording:
    """Records the context the pipeline gives the interpreter."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.contexts = []
        self.single_calls = []

    def interpret_comment(self, text, ctx=None):
        self.contexts.append(ctx)
        return self.verdict

    def run_single(self, source, output, note=None, rules=None):
        from app.recipe_runner import RecipeResult
        self.single_calls.append(note)
        return RecipeResult(status="done", output_path=output)


def wired(tmp_path, verdict):
    pipeline, immich, recipe, rules = make(tmp_path)
    recording = Recording(verdict)
    pipeline.recipe = recording
    immich.created = []
    immich.create_album = lambda name: immich.created.append(name) or f"id-{name}"
    immich.remove_assets_from_album = lambda album_id, ids: None
    return pipeline, immich, recording, rules


def test_a_reply_that_is_not_an_album_never_creates_one(tmp_path):
    # the live failure: "yes move its crop" while the recipe's question was open
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("answer"))
    state, img = state_with(awaiting=True)
    img.clarification_note, img.clarification_question = "recenter this", "Which way?"
    handle(pipeline, state, img, "yes move its crop", cid="c2")
    assert immich.created == [] and state.watched_albums == {}
    assert rec.single_calls and "yes move its crop" in rec.single_calls[0]
    assert rec.contexts[0].awaiting == "clarification" and rec.contexts[0].question == "Which way?"


def test_album_intent_in_review_promotes_even_without_a_question(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Holiday"))
    state, img = state_with()
    handle(pipeline, state, img, "put this in the holiday album")
    assert immich.created == ["Holiday"] and img.home == "Holiday"
    assert "c1" in img.acted_comment_ids and rec.contexts[0].awaiting is None


def test_album_intent_in_a_managed_album_explains_instead_of_moving(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Other"))
    state, img = state_with(home="Holiday")
    state.watched_albums["Holiday"] = "alb-h"
    from tests.test_rules import asset, comment
    pipeline._handle_fresh_comment(
        state, "L", img, asset(), comment("send to Other"), album_id="alb-h", in_review=False,
    )
    assert immich.created == [] and img.home == "Holiday"
    assert any("unlike it first" in t for t, _, _ in immich.posted)
    assert rec.contexts[0].where == "Holiday"


def test_unclear_asks_back_and_keeps_waiting(tmp_path):
    verdict = CommentIntent("unclear", question="Album 'move its crop', or a change to the photo?")
    pipeline, immich, rec, rules = wired(tmp_path, verdict)
    state, img = state_with(awaiting=True)
    img.awaiting_album = True
    handle(pipeline, state, img, "hmm maybe", cid="c2")
    assert [t for t, _, _ in immich.posted] == ["Album 'move its crop', or a change to the photo?"]
    assert "c2" in img.acted_comment_ids
    assert img.awaiting_album and img.awaiting_clarification and img.home == "review"
    assert immich.created == [] and rec.single_calls == []


def test_none_does_nothing_but_is_not_seen_again(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("none"))
    state, img = state_with()
    handle(pipeline, state, img, "thanks!")
    assert immich.posted == [] and rec.single_calls == [] and "c1" in img.acted_comment_ids


def test_an_uninterpretable_comment_is_left_for_the_next_cycle(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, None)
    state, img = state_with(awaiting=True)
    img.awaiting_album = True
    assert handle(pipeline, state, img, "Holiday", cid="c2") is False
    assert "c2" not in img.acted_comment_ids
    assert immich.created == [] and immich.posted == [] and immich.deleted == []


def test_forget_by_description_retires_the_matched_rule(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("forget", rule_id="r1"))
    rules.add("Prefer thin bevels.")
    state, img = state_with()
    handle(pipeline, state, img, "stop doing the thin bevel thing")
    assert rules.get("r1").status == "retired"
    assert any("Retired rule r1" in t for t, _, _ in immich.posted)
    assert rec.contexts[0].rules == [("r1", "Prefer thin bevels.")]


def test_a_natural_yes_activates_the_proposed_rule(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("yes"))
    rules.add("Keep mats pale.", "all", status="proposed", lineage_id="L", source_asset_id="a1")
    state, img = state_with()
    handle(pipeline, state, img, "yeah that's a good idea, keep that")  # not a bare "yes", so it reaches the interpreter
    assert rules.get("r1").status == "active"
    assert rec.contexts[0].proposal == ("r1", "Keep mats pale.")


def test_a_natural_no_drops_the_proposed_rule(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("no"))
    rules.add("Keep mats pale.", "all", status="proposed", lineage_id="L", source_asset_id="a1")
    state, img = state_with()
    handle(pipeline, state, img, "nah, that was a one-off")
    assert rules.get("r1").status == "retired"


def test_a_revision_while_the_album_question_is_open_edits_the_photo(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("revise"))
    state, img = state_with(awaiting=True)
    img.awaiting_album = True
    handle(pipeline, state, img, "actually make the mat darker", cid="c2")
    assert rec.single_calls == ["actually make the mat darker"]
    assert immich.created == [] and not img.awaiting_album and not img.awaiting_clarification


def test_context_for_the_album_question_lists_the_albums(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("none"))
    state, img = state_with()
    state.watched_albums.update({"Holiday": "a1", "Everyday": "a2"})
    pipeline._ask_which_album(state, "L", "a1")
    handle(pipeline, state, img, "holiday", cid="c2")
    ctx = rec.contexts[0]
    assert ctx.awaiting == "album" and ctx.albums == ["Holiday", "Everyday"] and ctx.where == "review"
