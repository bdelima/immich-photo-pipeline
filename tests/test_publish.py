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
        self.trashed = set()       # asset ids sitting in Immich's trash
        self.gone = set()          # asset ids Immich no longer has
        self.restored = []
        self.dedupe = {}           # bytes -> asset id, like Immich's checksum match

    def upload_asset(self, path, name):
        if self.fail_upload:
            raise ImmichError("POST /assets -> 500")
        data = open(path, "rb").read()
        self.uploads.append((data, name))
        if data in self.dedupe and self.dedupe[data] not in self.gone:
            return self.dedupe[data]
        self.dedupe[data] = f"asset-{len(self.uploads)}"
        return self.dedupe[data]

    def get_asset(self, asset_id):
        if asset_id in self.gone:
            return None
        return {"id": asset_id, "isTrashed": asset_id in self.trashed}

    def restore_assets(self, ids):
        self.restored.extend(ids)
        self.trashed -= set(ids)

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


def test_a_revert_that_gets_a_trashed_asset_back_restores_it(tmp_path):
    store, revisions = build(tmp_path, asset="asset-1", published=0, current=0, steps=1)
    immich = FakeImmich()
    immich.dedupe[b"image 0"] = "asset-1"
    # Photo moved on, asset-1 went to Immich's trash, then the photo reverted to identical bytes.
    store.update_photo("p", lambda x: (setattr(x, "immich_asset_id", "asset-2"),
                                       setattr(x, "published_revision", 1)))
    immich.trashed.add("asset-1")
    publish_pending(immich, store, revisions)
    photo = p(store)
    assert photo.immich_asset_id == "asset-1" and immich.restored == ["asset-1"]
    assert "asset-1" not in photo.stale_asset_ids and photo.stale_asset_ids == ["asset-2"]
    tidy_stale(immich, store)
    assert immich.deleted == [(["asset-2"], False)]


def test_an_asset_that_is_still_queued_stale_is_unqueued_when_it_comes_back(tmp_path):
    store, revisions = build(tmp_path, asset="asset-2", published=1, current=0, steps=2, stale_asset_ids=["asset-1"])
    immich = FakeImmich()
    immich.dedupe[b"image 0"] = "asset-1"
    publish_pending(immich, store, revisions)
    photo = p(store)
    assert photo.immich_asset_id == "asset-1" and "asset-1" not in photo.stale_asset_ids
    tidy_stale(immich, store)
    assert all("asset-1" not in ids for ids, _ in immich.deleted)


def test_when_the_immich_trash_was_emptied_the_item_is_uploaded_again(tmp_path):
    store, revisions = build(tmp_path, asset="asset-2", published=1, current=0, steps=2)
    immich = FakeImmich()
    immich.dedupe[b"image 0"] = "asset-1"
    immich.gone.add("asset-1")                     # emptied: upload makes a new asset
    publish_pending(immich, store, revisions)
    photo = p(store)
    assert photo.immich_asset_id != "asset-1" and photo.immich_asset_id.startswith("asset-")
    assert immich.restored == []


def test_restore_failing_for_a_vanished_asset_falls_back_to_uploading_again(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None, steps=1)
    immich = FakeImmich()
    immich.dedupe[b"image 0"] = "asset-9"
    immich.trashed.add("asset-9")

    def failing_restore(ids):
        immich.gone.add("asset-9")                 # emptied between the check and the restore
        raise ImmichError("POST /trash/restore/assets -> 400")
    immich.restore_assets = failing_restore
    publish_pending(immich, store, revisions)
    assert p(store).immich_asset_id not in (None, "asset-9")


def test_a_failed_restore_of_a_live_asset_is_retried_not_recorded(tmp_path):
    store, revisions = build(tmp_path, asset=None, published=None, steps=1)
    immich = FakeImmich()
    immich.dedupe[b"image 0"] = "asset-9"
    immich.trashed.add("asset-9")
    immich.restore_assets = lambda ids: (_ for _ in ()).throw(ImmichError("POST /trash/restore/assets -> 500"))
    assert publish_pending(immich, store, revisions) == []
    assert p(store).immich_asset_id is None


def test_tidy_never_trashes_an_asset_another_photo_shows(tmp_path):
    store, revisions = build(tmp_path, asset="shared", published=0, stale_asset_ids=["shared", "x"])
    immich = FakeImmich()
    assert tidy_stale(immich, store) == 1
    assert immich.deleted == [(["x"], False)] and p(store).stale_asset_ids == []
