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


# ---- existing albums are used, not duplicated ------------------------------


def test_an_album_made_by_hand_in_immich_is_used_not_duplicated(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Holiday"))
    immich.albums = [{"id": "hand-made", "albumName": "Holiday"}]
    state, img = state_with()
    handle(pipeline, state, img, "Holiday")
    assert immich.created == []
    assert img.home == "Holiday" and state.watched_albums == {"Holiday": "hand-made"}
    assert ("hand-made", ["a1"]) in immich.added
    assert [(a, i) for _, a, i in immich.posted] == [("hand-made", "a1")]  # said in the album it went to
    assert "Moved here from Review" in immich.posted[0][0] and "I created" not in immich.posted[0][0]


def test_the_name_match_ignores_case_and_uses_the_albums_own_spelling(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="holiday"))
    immich.albums = [{"id": "hand-made", "albumName": "Holiday"}]
    state, img = state_with()
    handle(pipeline, state, img, "holiday")
    assert immich.created == [] and img.home == "Holiday"


def test_an_album_the_pipeline_already_manages_is_reused(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="HOLIDAY"))
    state, img = state_with()
    state.watched_albums["Holiday"] = "managed-1"
    handle(pipeline, state, img, "HOLIDAY")
    assert immich.created == [] and img.home == "Holiday" and ("managed-1", ["a1"]) in immich.added


def test_a_genuinely_new_album_is_created_and_the_ack_says_so(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Trips"))
    state, img = state_with()
    handle(pipeline, state, img, "Trips")
    assert immich.created == ["Trips"]
    assert [(a, i) for _, a, i in immich.posted] == [("id-Trips", "a1")]
    assert "(I created it)" in immich.posted[0][0]


def test_the_pipelines_own_albums_are_never_promotion_targets(tmp_path):
    from types import SimpleNamespace
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Review"))
    pipeline.cfg = SimpleNamespace(review_album_id="review-album", review_album_name="Review")
    immich.albums = [{"id": "review-album", "albumName": "Review"}]
    state, img = state_with()
    handle(pipeline, state, img, "Review")
    assert immich.created == [] and img.home == "review"
    assert any("one of this pipeline's own albums" in t for t, _, _ in immich.posted)


def test_if_albums_cannot_be_listed_nothing_is_created(tmp_path):
    import pytest
    from app.immich_client import ImmichError
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Holiday"))

    def boom():
        raise ImmichError("down")

    immich.list_albums = boom
    state, img = state_with()
    with pytest.raises(ImmichError):
        handle(pipeline, state, img, "Holiday")
    assert immich.created == [] and "c1" not in img.acted_comment_ids


def test_the_interpreter_is_told_about_albums_made_by_hand(tmp_path):
    from types import SimpleNamespace
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("none"))
    pipeline.cfg = SimpleNamespace(review_album_id="review-album", live_album_id="live-id")
    immich.albums = [
        {"id": "x1", "albumName": "Family"}, {"id": "live-id", "albumName": "Live"},
        {"id": "review-album", "albumName": "Review"}, {"id": "x2", "albumName": "holiday"},
    ]
    state, img = state_with()
    state.watched_albums["Holiday"] = "m1"
    handle(pipeline, state, img, "hello")
    assert rec.contexts[0].albums == ["Holiday", "Family"]


# ---- the reviewer is told what was understood -------------------------------


def test_a_revision_is_acknowledged_before_it_runs(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("revise"))
    order = []
    original_post = immich.post_comment
    immich.post_comment = lambda text, **kw: order.append("post") or original_post(text, **kw)
    rec_run = rec.run_single
    rec.run_single = lambda *a, **kw: order.append("run") or rec_run(*a, **kw)
    state, img = state_with()
    handle(pipeline, state, img, "tilt it one degree counter-clockwise")
    assert order[0] == "post" and "run" in order
    ack = immich.posted[0][0]
    assert ack.startswith("Got it: revising (tilt it one degree counter-clockwise)") and "minute or two" in ack
    assert immich.posted[0][2] == "a1"


def test_a_long_comment_is_shortened_in_the_acknowledgment(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("revise"))
    state, img = state_with()
    handle(pipeline, state, img, "make it darker " * 20)
    assert len(immich.posted[0][0]) < 200 and "…" in immich.posted[0][0]


def test_an_answer_is_acknowledged_as_such(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("answer"))
    state, img = state_with(awaiting=True)
    img.clarification_note, img.clarification_question = "recenter this", "Which way?"
    handle(pipeline, state, img, "on the dog", cid="c2")
    assert immich.posted[0][0].startswith("Got it: applying your answer and re-running.")


def test_the_pipelines_own_acknowledgments_are_never_read_back(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("revise"))
    state, img = state_with()
    handle(pipeline, state, img, "darker")
    # every post the pipeline made was recorded as already handled
    assert all(f"posted-{i}" in img.acted_comment_ids for i in range(1, len(immich.posted) + 1))


def test_nothing_is_acknowledged_when_nothing_will_happen(tmp_path):
    for verdict in (CommentIntent("none"), CommentIntent("delete")):
        pipeline, immich, rec, rules = wired(tmp_path, verdict)
        state, img = state_with()
        handle(pipeline, state, img, "whatever")
        assert immich.posted == []


def test_an_imported_photo_gets_only_the_no_original_explanation(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("revise"))
    state, img = state_with(home="Everyday", imported=True)
    handle(pipeline, state, img, "darker", in_review=False)
    assert [t for t, _, _ in immich.posted if "Got it" in t] == []
    assert any("no original" in t for t, _, _ in immich.posted)


# ---- comments are posted where the photo is --------------------------------


def test_nothing_is_left_behind_in_review_when_a_photo_moves_albums(tmp_path):
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("album", album="Holiday"))
    state, img = state_with()
    handle(pipeline, state, img, "Holiday")
    assert all(album != "review-album" for _, album, _ in immich.posted)


def test_unliking_in_a_managed_album_says_so_in_review(tmp_path):
    from types import SimpleNamespace
    from app.immich_client import Asset
    pipeline, immich, rec, rules = wired(tmp_path, CommentIntent("none"))
    pipeline.cfg = SimpleNamespace(review_album_id="review-album")
    immich.list_album_assets = lambda album_id: [Asset(id="a1", original_file_name="a.jpg", is_favorite=False)]
    state, img = state_with(home="Holiday")
    state.watched_albums["Holiday"] = "alb-h"
    pipeline._flow3_managed(state)
    assert img.home == "review"
    assert [(a, i) for _, a, i in immich.posted] == [("review-album", "a1")]
    assert "Moved back to Review" in immich.posted[0][0] and "Holiday" in immich.posted[0][0]
