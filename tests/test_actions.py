import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import actions
from app.actions import ActionError
from app.library import Library, LibraryStore, Photo, Revision
from app.revisions import RevisionStore


def build(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "revs"))

    def photo(pid, home="review", **kw):
        p = Photo(id=pid, home=home, **kw)
        for n in range(3):
            src = tmp_path / f"{pid}-{n}.jpg"
            src.write_bytes(f"{pid} image {n}".encode())
            rel, sha = revisions.save_revision(pid, n, str(src))
            p.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=rel, sha256=sha,
                                        instruction=f"step {n}" if n else None))
        p.current = 2
        return p

    store.update(lambda lib: (
        lib.photos.update(
            a=photo("a", immich_asset_id="im-a", published_revision=2),
            b=photo("b", "Holiday", live=True, immich_asset_id="im-b", published_revision=2),
            c=photo("c", status="processing"),
            d=photo("d", status="failed", home="inbox"),
        ),
        lib.albums.update({"Holiday": "alb-h"}),
    ))
    return store, revisions


def lib(store):
    return store.load()


# ---- promote ---------------------------------------------------------------


def test_promote_and_unpromote(tmp_path):
    store, _ = build(tmp_path)
    r = actions.promote(store, ["a", "b"], True)
    assert r.done == ["a"] and r.skipped == [{"id": "b", "reason": "already in Live"}]
    assert lib(store).photos["a"].live
    r = actions.promote(store, ["a"], False)
    assert r.done == ["a"] and not lib(store).photos["a"].live
    assert actions.promote(store, ["a"], False).skipped[0]["reason"] == "not in Live"


def test_a_photo_that_is_not_finished_cannot_be_promoted(tmp_path):
    store, _ = build(tmp_path)
    r = actions.promote(store, ["c", "d", "zzz"])
    assert r.done == [] and [s["reason"] for s in r.skipped] == [
        "it isn't finished yet", "it isn't finished yet", "no such photo"]


def test_a_bad_request_is_rejected_as_a_whole(tmp_path):
    store, _ = build(tmp_path)
    for bad in (None, [], "a", [1], [""], ["x"] * (actions.MAX_BATCH + 1)):
        with pytest.raises(ActionError):
            actions.promote(store, bad)
    assert actions.promote(store, ["a", "a"]).done == ["a"]       # duplicates collapse


# ---- move ------------------------------------------------------------------


def test_move_to_an_album_a_new_album_and_back_to_review(tmp_path):
    store, _ = build(tmp_path)
    assert actions.move(store, ["a"], "Holiday").done == ["a"]
    assert lib(store).photos["a"].home == "Holiday"
    assert actions.move(store, ["a"], "  Summer   2026 ").done == ["a"]
    assert lib(store).photos["a"].home == "Summer 2026"      # the album is made by the next cycle
    assert actions.move(store, ["a", "b"], "Review").done == ["a", "b"]
    assert lib(store).photos["b"].home == "review"


def test_move_keeps_the_live_flag(tmp_path):
    store, _ = build(tmp_path)
    actions.move(store, ["b"], "Review")
    assert lib(store).photos["b"].live


def test_move_matches_an_existing_album_ignoring_case(tmp_path):
    store, _ = build(tmp_path)
    actions.move(store, ["a"], "Summer")
    r = actions.move(store, ["b"], "summer")
    assert lib(store).photos["b"].home == "Summer" and r.done == ["b"]
    actions.move(store, ["a"], "HOLIDAY")
    assert lib(store).photos["a"].home == "Holiday"


def test_move_skips_what_is_already_there_or_not_finished(tmp_path):
    store, _ = build(tmp_path)
    r = actions.move(store, ["b", "c"], "Holiday")
    assert r.done == [] and [s["reason"] for s in r.skipped] == ["already there", "it isn't finished yet"]


def test_bad_album_names_are_refused(tmp_path):
    store, _ = build(tmp_path)
    for bad in ("", "   ", None, 5, "x" * 61, "a/b", "a\\b", "bad\x00name", "Live", "inbox", "Trash"):
        with pytest.raises(ActionError):
            actions.move(store, ["a"], bad)
    with pytest.raises(ActionError):
        actions.move(store, ["a"], "wallpaper maker", {"Wallpaper Maker"})    # an entry queue
    assert lib(store).photos["a"].home == "review"


# ---- trash and restore -----------------------------------------------------


def test_trash_keeps_everything_and_queues_the_immich_copy(tmp_path):
    store, _ = build(tmp_path)
    r = actions.trash(store, ["a", "b"])
    assert r.done == ["a", "b"]
    p = lib(store).photos["b"]
    assert p.trashed and p.trashed_at and p.trashed_from == "Holiday"
    assert p.immich_asset_id is None and p.stale_asset_ids == ["im-b"] and p.published_revision is None
    assert p.live and len(p.revisions) == 3 and p.current == 2           # state is kept


def test_trash_skips_trashed_and_processing_photos(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["a"])
    r = actions.trash(store, ["a", "c", "d"])
    assert r.done == ["d"]
    assert {s["id"]: s["reason"] for s in r.skipped} == {"a": "already in the trash", "c": "it is being processed"}


def test_trashing_twice_does_not_queue_the_asset_twice(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["a"])
    actions.trash(store, ["a"])
    assert lib(store).photos["a"].stale_asset_ids == ["im-a"]


def test_a_trashed_photo_cannot_be_promoted_or_moved(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["a"])
    assert actions.promote(store, ["a"]).skipped[0]["reason"] == "it is in the trash"
    assert actions.move(store, ["a"], "Holiday").skipped[0]["reason"] == "it is in the trash"


def test_restore_puts_the_photo_back_where_it_was(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["b"])
    r = actions.restore(store, ["b", "a"])
    assert r.done == ["b"] and r.skipped == [{"id": "a", "reason": "not in the trash"}]
    p = lib(store).photos["b"]
    assert not p.trashed and p.home == "Holiday" and p.trashed_at is None and p.live
    assert p.immich_asset_id is None          # a fresh copy is uploaded by the next cycle


def test_restore_goes_to_review_if_the_album_is_gone(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["b"])
    store.update(lambda l: l.albums.pop("Holiday"))
    store.update(lambda l: setattr(l.photos["b"], "trashed_from", "Gone"))
    actions.restore(store, ["b"])
    assert lib(store).photos["b"].home == "review"


def test_a_failed_photo_can_be_trashed_and_restored(tmp_path):
    store, _ = build(tmp_path)
    actions.trash(store, ["d"])
    actions.restore(store, ["d"])
    p = lib(store).photos["d"]
    assert p.home == "inbox" and p.status == "failed"


# ---- revert ----------------------------------------------------------------


def test_revert_stacks_a_step_and_leaves_publishing_to_the_cycle(tmp_path):
    store, revisions = build(tmp_path)
    out = actions.revert(store, revisions, "a", 0)
    assert out == {"step": 3}
    p = lib(store).photos["a"]
    assert p.current == 3 and p.current_revision().reverts_to == 0
    assert p.immich_asset_id == "im-a" and p.published_revision == 2     # now out of date


def test_revert_to_the_image_already_shown_changes_nothing(tmp_path):
    store, revisions = build(tmp_path)
    assert actions.revert(store, revisions, "a", 2) == {"unchanged": True}
    assert len(lib(store).photos["a"].revisions) == 3


def test_revert_refuses_bad_requests(tmp_path):
    store, revisions = build(tmp_path)
    actions.trash(store, ["b"])
    for photo_id, step in (("nope", 0), ("a", 9), ("a", "0"), ("a", True), ("b", 0), ("c", 0)):
        with pytest.raises(ActionError):
            actions.revert(store, revisions, photo_id, step)
