import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import ImmichError
from app.library import REVIEW, LibraryStore, Photo, Source
from app.recipe_runner import RecipeResult
from app.revisions import RevisionStore
from app.rules import RulesStore
from app.worker import MAX_IMMICH_RETRIES, Worker


class FakeImmich:
    def __init__(self, fail_download=(), fail_upload=0, removable=True):
        self.fail_download = set(fail_download)
        self.fail_upload = fail_upload
        self.removable = removable
        self.uploads = []
        self.removed = []

    def download_asset_original(self, asset_id, dest_dir):
        if asset_id in self.fail_download:
            raise ImmichError(f"GET /assets/{asset_id}/original -> 500")
        path = os.path.join(dest_dir, f"{asset_id}.jpg")
        with open(path, "wb") as fh:
            fh.write(f"orig-{asset_id}".encode())
        return path

    def upload_asset(self, path, name):
        if self.fail_upload:
            self.fail_upload -= 1
            raise ImmichError("POST /assets -> 503")
        self.uploads.append((name, open(path, "rb").read()))
        return f"up-{len(self.uploads)}"

    def remove_assets_from_album(self, album_id, ids):
        if not self.removable:
            raise ImmichError("not allowed")
        self.removed.append((album_id, list(ids)))


class FakeRecipe:
    def __init__(self, status="done", error=None):
        self.status = status
        self.error = error
        self.calls = []

    def _result(self, out_path):
        if self.error:
            raise self.error
        if self.status == "needs_clarification":
            return RecipeResult(status="needs_clarification", question="Which one is the subject?", session_id="s1")
        with open(out_path, "wb") as fh:
            fh.write(b"matted")
        return RecipeResult(status="done", output_path=out_path)

    def run_single(self, source_path, output_path, note=None, rules=None):
        self.calls.append(("single", [source_path], rules))
        return self._result(output_path)

    def run_collage(self, source_paths, output_path, note=None, rules=None):
        self.calls.append(("collage", list(source_paths), rules))
        return self._result(output_path)


def build(tmp_path, immich=None, recipe=None, rules=None, **kw):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "rev"))
    immich = immich or FakeImmich()
    recipe = recipe or FakeRecipe()
    worker = Worker(store, revisions, immich, recipe, wallpaper_album_id="wp", collage_album_id="co",
                    rules=rules, **kw)
    return worker, store, revisions, immich, recipe


def queue(store, photo_id="a", sources=("a",), kind="single", media="image", queue="wallpaper", created=None):
    photo = Photo(id=photo_id, kind=kind, media_type=media, queue=queue, status="processing",
                  sources=[Source(asset_id=s, owner_id="u1") for s in sources])
    if created:
        photo.created_at = created
    store.update(lambda lib: lib.photos.update({photo_id: photo}))


def test_a_queued_image_is_processed_into_review(tmp_path):
    worker, store, revisions, immich, recipe = build(tmp_path)
    queue(store)
    assert worker.run_one() is True and worker.run_one() is False
    photo = store.load().photos["a"]
    assert (photo.status, photo.home, photo.current, photo.immich_asset_id) == ("ready", REVIEW, 0, "up-1")
    assert photo.error is None and photo.live is False and photo.trashed is False
    rev = photo.current_revision()
    assert rev.parent is None and rev.origin == "processed" and rev.instruction is None
    assert open(revisions.path(rev.file), "rb").read() == b"matted"
    assert open(revisions.path(photo.sources[0].file), "rb").read() == b"orig-a"
    assert immich.uploads == [("a.jpg", b"matted")]


def test_the_original_is_cleared_from_its_entry_queue(tmp_path):
    worker, store, _, immich, _ = build(tmp_path)
    queue(store)
    worker.run_one()
    assert immich.removed == [("wp", ["a"])]


def test_an_original_that_cannot_be_removed_does_not_fail_the_photo(tmp_path):
    worker, store, _, _, _ = build(tmp_path, immich=FakeImmich(removable=False))
    queue(store)
    worker.run_one()
    assert store.load().photos["a"].status == "ready"


def test_the_extra_accounts_are_tried_when_the_primary_cannot_remove(tmp_path):
    class Extra(FakeImmich):
        pass

    extra = Extra()
    worker, store, _, immich, _ = build(tmp_path, immich=FakeImmich(removable=False), extra_clients=[extra])
    queue(store)
    worker.run_one()
    assert extra.removed == [("wp", ["a"])]


def test_a_collage_copies_every_original_and_runs_the_collage_recipe(tmp_path):
    worker, store, revisions, _, recipe = build(tmp_path)
    queue(store, "a", ("a", "b"), kind="collage", queue="collage")
    worker.run_one()
    photo = store.load().photos["a"]
    assert recipe.calls[0][0] == "collage" and len(recipe.calls[0][1]) == 2
    assert [open(revisions.path(s.file), "rb").read() for s in photo.sources] == [b"orig-a", b"orig-b"]


def test_the_recipe_is_given_the_stored_copies_not_temporary_ones(tmp_path):
    worker, store, revisions, _, recipe = build(tmp_path)
    queue(store)
    worker.run_one()
    assert recipe.calls[0][1][0] == revisions.path(store.load().photos["a"].sources[0].file)


def test_active_rules_for_the_kind_reach_the_recipe_and_are_recorded(tmp_path):
    rules = RulesStore(str(tmp_path / "rules.json"))
    rules.add("Keep collage items balanced by size.", "collage")
    rules.add("Prefer thin bevels.", "single")
    worker, store, _, _, recipe = build(tmp_path, rules=rules)
    queue(store)
    worker.run_one()
    assert recipe.calls[0][2] == ["Prefer thin bevels."]
    assert store.load().photos["a"].current_revision().rules == ["Prefer thin bevels."]


def test_a_rules_file_that_cannot_be_read_does_not_stop_processing(tmp_path):
    class Broken:
        def active_texts(self, kind):
            raise OSError("unreadable")

    worker, store, _, _, recipe = build(tmp_path, rules=Broken())
    queue(store)
    worker.run_one()
    assert recipe.calls[0][2] == [] and store.load().photos["a"].status == "ready"


def test_a_video_is_copied_and_published_untouched_without_claude(tmp_path):
    worker, store, revisions, immich, recipe = build(tmp_path, can_run_recipe=lambda: False)
    queue(store, "v", ("v",), media="video")
    assert worker.run_one() is True
    photo = store.load().photos["v"]
    assert recipe.calls == [] and photo.status == "ready" and photo.home == REVIEW
    assert photo.current_revision().origin == "original"
    assert open(revisions.path(photo.current_revision().file), "rb").read() == b"orig-v"
    assert immich.uploads[0][0] == "v.jpg" and immich.uploads[0][1] == b"orig-v"


def test_an_image_waits_while_there_is_no_claude_session(tmp_path):
    ok = {"v": False}
    worker, store, _, _, recipe = build(tmp_path, can_run_recipe=lambda: ok["v"])
    queue(store)
    assert worker.run_one() is False and recipe.calls == []
    assert store.load().photos["a"].status == "processing"
    ok["v"] = True
    assert worker.run_one() is True and store.load().photos["a"].status == "ready"


def test_a_question_from_the_recipe_is_recorded_and_nothing_is_published(tmp_path):
    worker, store, _, immich, _ = build(tmp_path, recipe=FakeRecipe(status="needs_clarification"))
    queue(store)
    worker.run_one()
    photo = store.load().photos["a"]
    assert photo.status == "awaiting_answer" and photo.question == "Which one is the subject?"
    assert photo.session_id == "s1" and photo.revisions == [] and immich.uploads == [] and immich.removed == []
    assert worker.run_one() is False


def test_a_recipe_failure_marks_the_photo_failed_and_is_not_retried(tmp_path):
    recipe = FakeRecipe(error=RuntimeError("claude exploded"))
    worker, store, _, immich, _ = build(tmp_path, recipe=recipe)
    queue(store)
    worker.run_one()
    photo = store.load().photos["a"]
    assert photo.status == "failed" and "claude exploded" in photo.error
    assert worker.run_one() is False and len(recipe.calls) == 1 and immich.uploads == []


def test_a_temporary_immich_error_is_retried_without_running_the_recipe_again(tmp_path):
    worker, store, _, immich, recipe = build(tmp_path, immich=FakeImmich(fail_upload=1))
    queue(store)
    worker.run_one()
    photo = store.load().photos["a"]
    # the result is already saved, so Claude is not asked again
    assert photo.status == "processing" and photo.current == 0 and photo.immich_asset_id is None
    worker._retry_after.clear()
    worker.run_one()
    assert store.load().photos["a"].status == "ready" and len(recipe.calls) == 1


def test_a_photo_that_keeps_failing_to_download_is_eventually_marked_failed(tmp_path):
    worker, store, _, _, recipe = build(tmp_path, immich=FakeImmich(fail_download={"a"}))
    queue(store)
    for _ in range(MAX_IMMICH_RETRIES):
        worker._retry_after.clear()
        assert worker.run_one() is True
    photo = store.load().photos["a"]
    assert photo.status == "failed" and "Immich" in photo.error and recipe.calls == []


def test_a_photo_waits_out_its_retry_delay_instead_of_spinning(tmp_path):
    worker, store, _, _, _ = build(tmp_path, immich=FakeImmich(fail_download={"a"}))
    queue(store)
    assert worker.run_one() is True
    assert worker.run_one() is False


def test_a_restart_resumes_where_the_last_run_stopped(tmp_path):
    worker, store, revisions, immich, recipe = build(tmp_path)
    queue(store)
    # the first run got as far as saving the result, then the container stopped
    rel, digest = revisions.save_revision("a", 0, _write(tmp_path, b"matted"))
    from app.library import Revision
    store.update_photo("a", lambda p: (p.revisions.append(Revision(n=0, parent=None, file=rel, sha256=digest)),
                                       setattr(p, "current", 0), setattr(p, "home", REVIEW)))
    again = build(tmp_path)[0]  # a new process
    assert again.run_one() is True
    photo = store.load().photos["a"]
    assert photo.status == "ready" and photo.immich_asset_id == "up-1" and recipe.calls == []


def _write(tmp_path, data):
    path = tmp_path / "x.jpg"
    path.write_bytes(data)
    return str(path)


def test_photos_are_taken_oldest_first_and_trashed_ones_are_skipped(tmp_path):
    worker, store, _, _, _ = build(tmp_path)
    queue(store, "late", ("late",), created="2026-01-02T00:00:00+00:00")
    queue(store, "early", ("early",), created="2026-01-01T00:00:00+00:00")
    queue(store, "gone", ("gone",), created="2025-12-31T00:00:00+00:00")
    store.update_photo("gone", lambda p: setattr(p, "trashed", True))
    assert worker.pending() == ["early", "late"]


def test_a_photo_is_only_claimed_by_one_worker_at_a_time(tmp_path):
    worker, store, _, _, _ = build(tmp_path, count=2)
    queue(store)
    first = worker._claim()
    assert first == "a" and worker._claim() is None


def test_worker_count_is_at_least_one(tmp_path):
    assert build(tmp_path, count=0)[0].count == 1
    assert build(tmp_path, count=3)[0].count == 3


def test_the_threads_process_the_queue_and_stop(tmp_path):
    import time
    worker, store, _, _, _ = build(tmp_path, count=2)
    queue(store, "a", ("a",))
    queue(store, "b", ("b",))
    worker.start()
    for _ in range(100):
        if all(p.status == "ready" for p in store.load().photos.values()):
            break
        time.sleep(0.05)
    worker.stop()
    assert all(p.status == "ready" for p in store.load().photos.values())
