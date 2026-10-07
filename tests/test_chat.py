import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import chat
from app.chat import ChatError
from app.library import (
    CHAT_DONE, CHAT_FAILED, CHAT_QUEUED, REVIEW, LibraryStore, Photo, Revision, Source,
)
from app.recipe_runner import CommentIntent, RecipeResult
from app.revisions import RevisionStore
from app.rules import MAX_ACTIVE_RULES, RulesStore
from app.worker import Worker


class FakeRecipe:
    """Interprets with a canned intent and 'makes' images by writing bytes."""

    def __init__(self, intent=None, results=None, can_interpret=True):
        self.intent = intent or CommentIntent("revise")
        self.results = list(results or [])
        self.interpreted = []
        self.runs = []
        self.resumed = []
        self.can_interpret = can_interpret

    def interpret_comment(self, text, ctx=None):
        self.interpreted.append((text, ctx))
        return self.intent if self.can_interpret else None

    def _next(self, out):
        result = self.results.pop(0) if self.results else RecipeResult(status="done")
        if result.status == "done":
            with open(out, "wb") as fh:
                fh.write(b"made-%d" % (len(self.runs) + len(self.resumed)))
            result.output_path = out
        return result

    def run_single(self, source_path, output_path, note=None, rules=None):
        self.runs.append((source_path, note, rules))
        return self._next(output_path)

    def run_collage(self, source_paths, output_path, note=None, rules=None):
        self.runs.append((list(source_paths), note, rules))
        return self._next(output_path)

    def resume(self, session_id, answer, output_path=None):
        self.resumed.append((session_id, answer, output_path))
        return self._next(output_path)


def build(tmp_path, recipe=None, with_rules=True):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "rev"))
    rules = RulesStore(str(tmp_path / "rules.json")) if with_rules else None
    return store, revisions, recipe or FakeRecipe(), rules


def add_photo(tmp_path, store, revisions, photo_id="a", steps=("first",), **kw):
    """A ready photo with one stored original and one revision per entry of
    `steps` (the first has no instruction)."""
    src = tmp_path / f"{photo_id}-src.jpg"
    src.write_bytes(b"orig")
    rel = revisions.save_source(photo_id, 0, str(src), "orig.jpg")
    photo = Photo(id=photo_id, home=REVIEW, status="ready", sources=[Source(asset_id="x", file=rel)], **kw)
    for n, instruction in enumerate(steps):
        img = tmp_path / f"{photo_id}-{n}.jpg"
        img.write_bytes(b"img%d" % n)
        r, sha = revisions.save_revision(photo_id, n, str(img))
        photo.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=r, sha256=sha,
                                        instruction=None if n == 0 else instruction))
    photo.current = len(steps) - 1
    store.update(lambda lib: lib.photos.update({photo_id: photo}))
    return photo


def run(store, revisions, recipe, rules, photo_id="a"):
    return chat.run_job(store, revisions, recipe, rules, photo_id)


def texts(store, photo_id="a"):
    return [(m.role, m.text, m.state) for m in store.load().photos[photo_id].chat]


# ---- enqueue ---------------------------------------------------------------------


def test_enqueue_records_a_queued_message_and_cleans_it(tmp_path):
    store, revisions, _, _ = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    assert chat.enqueue(store, "a", "  make it\n  warmer \x07 ") == 1
    assert chat.enqueue(store, "a", "and brighter") == 2
    photo = store.load().photos["a"]
    assert [(m.id, m.role, m.text, m.state) for m in photo.chat] == [
        (1, "user", "make it warmer", CHAT_QUEUED), (2, "user", "and brighter", CHAT_QUEUED)]
    assert photo.busy


@pytest.mark.parametrize("text", ["", "   ", None, 5, "x" * 1001])
def test_enqueue_rejects_a_bad_message(tmp_path, text):
    store, revisions, _, _ = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    with pytest.raises(ChatError) as exc:
        chat.enqueue(store, "a", text)
    assert exc.value.status == 400
    assert store.load().photos["a"].chat == []


def test_enqueue_unknown_photo_is_a_404(tmp_path):
    store, *_ = build(tmp_path)
    with pytest.raises(ChatError) as exc:
        chat.enqueue(store, "nope", "hi")
    assert exc.value.status == 404


@pytest.mark.parametrize("fields,fragment", [
    ({"trashed": True}, "trash"),
    ({"media_type": "video"}, "Videos"),
    ({"imported": True}, "already finished"),
    ({"status": "processing"}, "being processed"),
    ({"status": "waiting"}, "partner"),
    ({"status": "failed"}, "failed"),
])
def test_enqueue_is_refused_when_the_photo_cannot_be_modified(tmp_path, fields, fragment):
    store, revisions, _, _ = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    store.update_photo("a", lambda p: [setattr(p, k, v) for k, v in fields.items()])
    with pytest.raises(ChatError) as exc:
        chat.enqueue(store, "a", "make it warmer")
    assert fragment in str(exc.value) and exc.value.status == 400


def test_enqueue_limits_how_many_messages_wait(tmp_path):
    store, revisions, _, _ = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    for i in range(chat.MAX_QUEUED):
        chat.enqueue(store, "a", f"m{i}")
    with pytest.raises(ChatError) as exc:
        chat.enqueue(store, "a", "one more")
    assert exc.value.status == 429


def test_the_transcript_is_capped_but_waiting_messages_are_kept(tmp_path):
    store, revisions, _, _ = build(tmp_path)
    add_photo(tmp_path, store, revisions)

    def fill(p):
        for i in range(chat.MAX_TRANSCRIPT + 20):
            chat._append(p, "claude", f"c{i}", CHAT_DONE)

    store.update_photo("a", fill)
    chat.enqueue(store, "a", "latest")
    photo = store.load().photos["a"]
    assert len(photo.chat) == chat.MAX_TRANSCRIPT
    assert photo.chat[-1].text == "latest" and photo.chat[-1].state == CHAT_QUEUED
    assert photo.chat[0].text == "c21"


# ---- revising ----------------------------------------------------------------------


def test_a_revision_request_makes_a_new_step_with_the_history_replayed(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, steps=("first", "crop tighter"))
    chat.enqueue(store, "a", "make it warmer")
    assert run(store, revisions, recipe, rules) is True
    photo = store.load().photos["a"]
    assert photo.current == 2 and photo.status == "ready"
    rev = photo.current_revision()
    assert (rev.parent, rev.instruction, rev.origin) == (1, "make it warmer", "processed")
    assert open(revisions.path(rev.file), "rb").read().startswith(b"made-")
    source, note, _ = recipe.runs[0]
    assert source == revisions.path(photo.sources[0].file)
    assert "crop tighter" in note and note.endswith("make it warmer")
    assert texts(store)[-2:] == [("user", "make it warmer", CHAT_DONE), ("claude", "Done. This is step 2.", CHAT_DONE)]
    assert not photo.busy


def test_the_interpreter_is_told_where_the_photo_is_and_what_was_done(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    rules.add("keep borders thin")
    add_photo(tmp_path, store, revisions, steps=("first", "crop tighter"))
    store.update(lambda lib: lib.albums.update({"Holiday": "alb"}))
    chat.enqueue(store, "a", "warmer")
    run(store, revisions, recipe, rules)
    text, ctx = recipe.interpreted[0]
    assert text == "warmer" and ctx.where == REVIEW and ctx.awaiting is None
    assert ctx.history == ["crop tighter"] and ctx.albums == ["Holiday"]
    assert ctx.rules == [("r1", "keep borders thin")]


def test_a_collage_is_revised_from_all_its_originals(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, kind="collage")
    src = tmp_path / "second.jpg"
    src.write_bytes(b"orig2")
    rel = revisions.save_source("a", 1, str(src), "second.jpg")
    store.update_photo("a", lambda p: p.sources.append(Source(asset_id="y", file=rel)))
    chat.enqueue(store, "a", "swap them")
    run(store, revisions, recipe, rules)
    paths, _, _ = recipe.runs[0]
    assert paths == [revisions.path(s.file) for s in store.load().photos["a"].sources]


def test_active_rules_are_used_and_recorded_on_the_revision(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    rules.add("keep borders thin")
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    run(store, revisions, recipe, rules)
    assert recipe.runs[0][2] == ["keep borders thin"]
    assert store.load().photos["a"].current_revision().rules == ["keep borders thin"]


def test_a_lesson_from_a_revision_becomes_a_proposal(tmp_path):
    result = RecipeResult(status="done", lesson="Keep borders thin", lesson_scope="single")
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(results=[result]))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "thinner border")
    run(store, revisions, recipe, rules)
    found = rules.pending_proposal_for("a")
    assert (found.text, found.scope, found.status) == ("Keep borders thin", "single", "proposed")
    assert "Keep borders thin" in texts(store)[-1][1]
    assert rules.active_texts("single") == []


def test_a_failed_recipe_marks_the_message_failed_and_is_not_retried(tmp_path):
    class Broken(FakeRecipe):
        def run_single(self, *a, **k):
            raise RuntimeError("claude invocation failed (1)")

    store, revisions, _, rules = build(tmp_path)
    recipe = Broken()
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    assert run(store, revisions, recipe, rules) is False
    photo = store.load().photos["a"]
    assert photo.current == 0 and not photo.busy
    assert [m.state for m in photo.chat] == [CHAT_FAILED, CHAT_DONE]
    assert "claude invocation failed" in photo.chat[-1].text
    assert run(store, revisions, recipe, rules) is False  # nothing left to retry


def test_an_uninterpretable_message_fails_without_spending_a_recipe_run(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(can_interpret=False))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    assert run(store, revisions, recipe, rules) is False
    assert recipe.runs == []
    assert store.load().photos["a"].chat[0].state == CHAT_FAILED


def test_a_missing_original_fails_the_message(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    store.update_photo("a", lambda p: setattr(p.sources[0], "file", ""))
    chat.enqueue(store, "a", "warmer")
    assert run(store, revisions, recipe, rules) is False
    assert "original" in texts(store)[-1][1]


def test_a_photo_trashed_while_waiting_is_left_alone(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    store.update_photo("a", lambda p: setattr(p, "trashed", True))
    assert run(store, revisions, recipe, rules) is False
    assert recipe.runs == [] and recipe.interpreted == []
    assert store.load().photos["a"].chat[0].state == CHAT_FAILED


def test_a_change_during_the_run_saves_nothing(tmp_path):
    class Racing(FakeRecipe):
        def run_single(self, source_path, output_path, note=None, rules=None):
            # a revert lands while Claude is working
            from app.revert import revert_to
            revert_to(store, revisions, "a", 0)
            return super().run_single(source_path, output_path, note, rules)

    store, revisions, _, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, steps=("first", "crop"))
    chat.enqueue(store, "a", "warmer")
    assert run(store, revisions, Racing(), rules) is False
    photo = store.load().photos["a"]
    assert [r.instruction for r in photo.revisions] == [None, "crop", None]  # only the revert
    assert chat.next_open_message(photo) is None and photo.chat[0].state == CHAT_FAILED
    assert not os.path.exists(revisions.path(os.path.join("a", "revisions", "3.jpg")))


def test_messages_are_handled_in_the_order_sent(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "first ask")
    chat.enqueue(store, "a", "second ask")
    run(store, revisions, recipe, rules)
    assert [t for t, _ in recipe.interpreted] == ["first ask"]
    assert store.load().photos["a"].busy
    run(store, revisions, recipe, rules)
    photo = store.load().photos["a"]
    assert not photo.busy
    assert photo.instructions() == ["first ask", "second ask"]
    assert "first ask" in recipe.runs[1][1] and recipe.runs[1][1].endswith("second ask")


# ---- questions ------------------------------------------------------------------------


def test_a_question_while_revising_is_asked_and_the_answer_completes_it(tmp_path):
    ask = RecipeResult(status="needs_clarification", question="Which person?", session_id="s9")
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(results=[ask]))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "remove the stranger")
    assert run(store, revisions, recipe, rules) is False
    photo = store.load().photos["a"]
    assert (photo.status, photo.question, photo.session_id) == ("awaiting_answer", "Which person?", "s9")
    assert photo.pending_instruction == "remove the stranger" and photo.current == 0
    assert texts(store)[-1] == ("claude", "Which person?", CHAT_DONE)

    recipe.intent = CommentIntent("answer")
    chat.enqueue(store, "a", "the one on the left")
    assert run(store, revisions, recipe, rules) is True
    assert recipe.resumed[0][:2] == ("s9", "the one on the left")
    assert recipe.resumed[0][2].endswith("output.jpg")
    _, ctx = recipe.interpreted[-1]
    assert ctx.awaiting == "clarification" and ctx.request == "remove the stranger" and ctx.question == "Which person?"
    photo = store.load().photos["a"]
    assert (photo.status, photo.current, photo.question, photo.pending_instruction) == ("ready", 1, None, None)
    assert photo.current_revision().instruction == "remove the stranger"


def test_a_new_request_while_a_revision_question_is_open_replaces_it(tmp_path):
    ask = RecipeResult(status="needs_clarification", question="Which person?", session_id="s9")
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(results=[ask]))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "remove the stranger")
    run(store, revisions, recipe, rules)
    recipe.intent = CommentIntent("revise")
    chat.enqueue(store, "a", "never mind, make it warmer")
    assert run(store, revisions, recipe, rules) is True
    photo = store.load().photos["a"]
    assert (photo.status, photo.question, photo.current) == ("ready", None, 1)
    assert photo.current_revision().instruction == "never mind, make it warmer"
    assert recipe.resumed == []


def test_answering_a_first_run_question_saves_revision_zero_for_the_worker_to_publish(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    src = tmp_path / "src.jpg"
    src.write_bytes(b"orig")
    rel = revisions.save_source("a", 0, str(src), "src.jpg")
    store.update(lambda lib: lib.photos.update({"a": Photo(
        id="a", status="awaiting_answer", question="Which one is the subject?", session_id="s1",
        queue="wallpaper", sources=[Source(asset_id="x", file=rel)])}))
    recipe.intent = CommentIntent("revise")  # with no image yet, guidance is the answer
    chat.enqueue(store, "a", "the dog")
    assert run(store, revisions, recipe, rules) is True
    assert recipe.resumed[0][:2] == ("s1", "the dog")
    photo = store.load().photos["a"]
    assert (photo.status, photo.home, photo.current, photo.question) == ("processing", REVIEW, 0, None)
    assert photo.current_revision().instruction is None


def test_a_second_question_keeps_the_photo_waiting(tmp_path):
    again = RecipeResult(status="needs_clarification", question="And the second one?", session_id="s2")
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(results=[again]))
    store.update(lambda lib: lib.photos.update({"a": Photo(
        id="a", status="awaiting_answer", question="Q1", session_id="s1", sources=[Source(asset_id="x")])}))
    recipe.intent = CommentIntent("answer")
    chat.enqueue(store, "a", "the dog")
    assert run(store, revisions, recipe, rules) is False
    photo = store.load().photos["a"]
    assert (photo.status, photo.question, photo.session_id) == ("awaiting_answer", "And the second one?", "s2")


# ---- undo --------------------------------------------------------------------------------


def test_undo_stacks_a_revert_to_the_step_before_the_last_adjustment(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("undo", steps=1)))
    add_photo(tmp_path, store, revisions, steps=("first", "crop", "warmer"))
    chat.enqueue(store, "a", "undo that")
    assert run(store, revisions, recipe, rules) is True
    photo = store.load().photos["a"]
    rev = photo.current_revision()
    assert (rev.n, rev.origin, rev.reverts_to) == (3, "reverted", 1)
    assert photo.instructions() == ["crop"]
    assert recipe.runs == []
    assert "step 3" in texts(store)[-1][1]


def test_undo_can_take_back_several_steps_but_stops_at_the_first_result(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("undo", steps=9)))
    add_photo(tmp_path, store, revisions, steps=("first", "crop", "warmer"))
    chat.enqueue(store, "a", "undo everything")
    run(store, revisions, recipe, rules)
    assert store.load().photos["a"].current_revision().reverts_to == 0


def test_undo_with_nothing_to_undo_just_says_so(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("undo")))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "undo")
    assert run(store, revisions, recipe, rules) is False
    assert "nothing to undo" in texts(store)[-1][1]
    assert store.load().photos["a"].current == 0


# ---- rules, buttons, small talk -----------------------------------------------------------


def test_teach_saves_a_rule_and_says_so(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("teach", rule="Always keep borders thin", scope="collage")))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "always keep borders thin on collages")
    assert run(store, revisions, recipe, rules) is False
    assert rules.active_texts("collage") == ["Always keep borders thin"]
    assert "Always keep borders thin" in texts(store)[-1][1]


def test_teach_reports_a_full_rules_list(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("teach", rule="one more")))
    for i in range(MAX_ACTIVE_RULES):
        rules.add(f"rule {i}")
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "remember this")
    run(store, revisions, recipe, rules)
    assert "limit" in texts(store)[-1][1]
    assert store.load().photos["a"].chat[0].state == CHAT_DONE


def test_forget_retires_the_named_rule(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("forget", rule_id="r1")))
    rules.add("thin borders")
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "stop doing that")
    run(store, revisions, recipe, rules)
    assert rules.active_texts("single") == []


@pytest.mark.parametrize("intent,active", [("yes", True), ("no", False)])
def test_yes_and_no_answer_a_proposed_rule(tmp_path, intent, active):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent(intent)))
    rules.add("thin borders", status="proposed", origin="proposed", lineage_id="a", source_asset_id="a")
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "yes please")
    run(store, revisions, recipe, rules)
    assert (rules.active_texts("single") == ["thin borders"]) is active
    assert rules.pending_proposal_for("a") is None
    assert recipe.interpreted[0][1].proposal == ("r1", "thin borders")


def test_yes_with_nothing_proposed_does_nothing(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("yes")))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "yes")
    run(store, revisions, recipe, rules)
    assert "no suggested rule" in texts(store)[-1][1]


@pytest.mark.parametrize("intent,fragment", [
    (CommentIntent("album", album="Holiday"), "buttons"),
    (CommentIntent("delete"), "buttons"),
    (CommentIntent("unclear", question="Warmer how?"), "Warmer how?"),
    (CommentIntent("none"), "OK"),
])
def test_other_intents_get_a_short_reply_and_change_nothing(tmp_path, intent, fragment):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(intent))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "whatever")
    assert run(store, revisions, recipe, rules) is False
    photo = store.load().photos["a"]
    assert fragment in photo.chat[-1].text and photo.current == 0 and not photo.trashed and photo.home == REVIEW
    assert recipe.runs == []


def test_run_job_with_nothing_waiting_does_nothing(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    assert run(store, revisions, recipe, rules) is False
    assert run(store, revisions, recipe, rules, "missing") is False
    assert recipe.interpreted == []


# ---- the worker -----------------------------------------------------------------------------


class NullImmich:
    pass


def make_worker(store, revisions, recipe, rules, **kw):
    return Worker(store, revisions, NullImmich(), recipe, wallpaper_album_id="wp", collage_album_id="co",
                  rules=rules, **kw)


def test_the_worker_handles_a_chat_message_and_nudges_the_cycle(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    pings = []
    worker = make_worker(store, revisions, recipe, rules, on_change=lambda: pings.append(1))
    chat.enqueue(store, "a", "warmer")
    assert worker.run_one() is True
    assert store.load().photos["a"].current == 1 and pings == [1]
    assert worker.run_one() is False


def test_the_worker_does_not_nudge_when_nothing_changed(tmp_path):
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(CommentIntent("none")))
    add_photo(tmp_path, store, revisions)
    pings = []
    worker = make_worker(store, revisions, recipe, rules, on_change=lambda: pings.append(1))
    chat.enqueue(store, "a", "thanks")
    assert worker.run_one() is True and pings == []


def test_the_oldest_waiting_message_goes_first_across_photos(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, "a")
    add_photo(tmp_path, store, revisions, "b")
    chat.enqueue(store, "b", "for b")
    chat.enqueue(store, "a", "for a")
    store.update_photo("b", lambda p: setattr(p.chat[0], "at", "2026-01-01T00:00:00+00:00"))
    store.update_photo("a", lambda p: setattr(p.chat[0], "at", "2026-01-02T00:00:00+00:00"))
    worker = make_worker(store, revisions, recipe, rules)
    worker.run_one()
    assert [t for t, _ in recipe.interpreted] == ["for b"]


def test_chat_is_not_started_while_claude_is_unavailable(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    worker = make_worker(store, revisions, recipe, rules, can_run_recipe=lambda: False)
    chat.enqueue(store, "a", "warmer")
    assert worker.run_one() is False
    assert recipe.interpreted == [] and store.load().photos["a"].busy


def test_a_message_left_working_by_a_restart_is_picked_up_again(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    store.update_photo("a", lambda p: setattr(p.chat[0], "state", "working"))
    worker = make_worker(store, revisions, recipe, rules)
    assert worker.run_one() is True
    assert store.load().photos["a"].current == 1


def test_a_trashed_photo_is_not_claimed_for_chat(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "warmer")
    store.update_photo("a", lambda p: setattr(p, "trashed", True))
    worker = make_worker(store, revisions, recipe, rules)
    assert worker.run_one() is False
