import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import actions
from app.immich_client import ImmichError
from app.library import LibraryStore, Photo, Revision
from app.publish import needs_publish, publish_pending, publish_photo, tidy_stale
from app.revisions import RevisionStore


class FakeImmich:
    def __init__(self):
        self.uploads = []
        self.deleted = []
        self.fail_upload = False
        self.fail_delete = False

    def upload_asset(self, path, name):
        if self.fail_upload:
            raise ImmichError("POST /assets -> 500")
        self.uploads.append((open(path, "rb").read(), name))
        return f"asset-{len(self.uploads)}"

    def delete_assets(self, ids, force=True):
        if self.fail_delete:
            raise ImmichError("DELETE /assets -> 500")
        self.deleted.append((list(ids), force))


def build(tmp_path, asset="old", published=0, current=0, steps=2, **kw):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "revs"))
    photo = Photo(id="p", home="review", immich_asset_id=asset, published_revision=published, **kw)
    for n in range(steps):
        src = tmp_path / f"s{n}.jpg"
        src.write_bytes(f"image {n}".encode())
        rel, sha = revisions.save_revision("p", n, str(src))
        photo.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=rel, sha256=sha))
    photo.current = current
    store.update(lambda lib: lib.photos.__setitem__("p", photo))
    return store, revisions


def p(store):
    return store.load().photos["p"]


def test_up_to_date_photos_need_nothing(tmp_path):
    store, revisions = build(tmp_path)
    assert not needs_publish(p(store))
    assert publish_pending(FakeImmich(), store, revisions) == []


def test_a_photo_with_no_asset_is_uploaded(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None)
    immich = FakeImmich()
    assert publish_pending(immich, store, revisions) == ["p"]
    assert immich.uploads[0][0] == b"image 0"
    assert p(store).immich_asset_id == "asset-1" and p(store).published_revision == 0
    assert p(store).stale_asset_ids == []


def test_a_new_current_revision_replaces_the_asset_and_queues_the_old_one(tmp_path):
    store, revisions = build(tmp_path, current=1)
    immich = FakeImmich()
    publish_pending(immich, store, revisions)
    photo = p(store)
    assert immich.uploads[0][0] == b"image 1"
    assert (photo.immich_asset_id, photo.published_revision, photo.stale_asset_ids) == ("asset-1", 1, ["old"])
    assert publish_pending(immich, store, revisions) == []         # now up to date


def test_an_asset_with_no_recorded_revision_is_trusted(tmp_path):
    store, revisions = build(tmp_path, published=None, current=1)
    assert not needs_publish(p(store))


def test_trashed_and_unfinished_photos_are_not_published(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None, status="processing")
    assert publish_pending(FakeImmich(), store, revisions) == []
    store.update_photo("p", lambda x: setattr(x, "status", "ready"))
    store.update_photo("p", lambda x: setattr(x, "trashed", True))
    assert publish_pending(FakeImmich(), store, revisions) == []


def test_a_failed_upload_changes_nothing_and_is_retried(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None)
    immich = FakeImmich()
    immich.fail_upload = True
    assert publish_pending(immich, store, revisions) == []
    assert p(store).immich_asset_id is None
    immich.fail_upload = False
    assert publish_pending(immich, store, revisions) == ["p"]


def test_a_missing_revision_file_is_skipped_not_fatal(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None)
    os.unlink(revisions.path(p(store).current_revision().file))
    assert publish_pending(FakeImmich(), store, revisions) == []


def test_a_photo_trashed_during_the_upload_does_not_get_the_new_asset(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None)

    class Racing(FakeImmich):
        def upload_asset(self, path, name):
            out = super().upload_asset(path, name)
            store.update_photo("p", lambda x: setattr(x, "trashed", True))
            return out

    publish_photo(Racing(), store, revisions, "p")
    photo = p(store)
    assert photo.immich_asset_id is None and photo.stale_asset_ids == ["asset-1"]


def test_tidy_moves_old_assets_to_the_immich_trash_without_forcing(tmp_path):
    store, revisions = build(tmp_path, stale_asset_ids=["x", "y"])
    immich = FakeImmich()
    assert tidy_stale(immich, store) == 2
    assert immich.deleted == [(["x", "y"], False)] and p(store).stale_asset_ids == []
    assert tidy_stale(immich, store) == 0


def test_tidy_keeps_the_ids_when_immich_fails_and_tries_again(tmp_path):
    store, revisions = build(tmp_path, stale_asset_ids=["x"])
    immich = FakeImmich()
    immich.fail_delete = True
    assert tidy_stale(immich, store) == 0 and p(store).stale_asset_ids == ["x"]
    immich.fail_delete = False
    assert tidy_stale(immich, store) == 1 and p(store).stale_asset_ids == []


def test_trash_then_restore_round_trip(tmp_path):
    store, revisions = build(tmp_path, asset="im-1", published=0)
    immich = FakeImmich()
    actions.trash(store, ["p"])
    tidy_stale(immich, store)
    assert immich.deleted == [(["im-1"], False)]
    actions.restore(store, ["p"])
    publish_pending(immich, store, revisions)
    photo = p(store)
    assert photo.immich_asset_id == "asset-1" and photo.published_revision == 0 and not photo.trashed
