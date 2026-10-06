import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.cycle import Cycle
from app.immich_client import Asset, ImmichError
from app.intake import Intake
from app.library import Library, LibraryStore, Photo


class FakeImmich:
    """Albums by id; this account is "pipeline"."""

    def __init__(self, queues=None, albums=None, existing=(), fail_share=False):
        self.contents = {k: set(v) for k, v in (queues or {}).items()}
        self.contents.update({k: set(v) for k, v in (albums or {}).items()})
        self.existing = list(existing)
        self.created = []
        self.shared = []
        self.fail_share = fail_share

    def list_album_assets(self, album_id):
        return [Asset(id=i, original_file_name=f"{i}.jpg") for i in sorted(self.contents.get(album_id, ()))]

    def get_my_user_id(self):
        return "pipeline"

    def list_albums(self):
        return list(self.existing)

    def create_album(self, name):
        self.created.append(name)
        self.contents[f"new-{name}"] = set()
        return f"new-{name}"

    def add_assets_to_album(self, album_id, ids):
        self.contents.setdefault(album_id, set()).update(ids)

    def remove_assets_from_album(self, album_id, ids):
        self.contents.setdefault(album_id, set()).difference_update(ids)

    def get_album(self, album_id):
        return {"ownerId": "pipeline", "albumUsers": [], "isActivityEnabled": True}

    def add_album_users(self, album_id, user_ids, role="editor"):
        if self.fail_share:
            raise ImmichError("403")
        self.shared.append((album_id, list(user_ids), role))


class FakeWorker:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1


def build(tmp_path, immich, share=("wife",), revisions=None):
    store = LibraryStore(str(tmp_path / "lib.json"))
    worker = FakeWorker()
    intake = Intake(immich, store, "wp", "co")
    cycle = Cycle(immich, store, intake, worker, live_album_id="LIVE", share_user_ids=list(share), revisions=revisions)
    return cycle, store, worker


def test_a_new_original_is_recorded_and_the_worker_woken(tmp_path):
    immich = FakeImmich(queues={"wp": {"a"}})
    cycle, store, worker = build(tmp_path, immich)
    cycle.run_once()
    assert store.load().photos["a"].status == "processing" and worker.woken == 1
    cycle.run_once()
    assert worker.woken == 1


def test_albums_for_every_home_are_created_shared_as_viewers_and_filled(tmp_path):
    immich = FakeImmich()
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(
        a=Photo(id="a", home="Holiday", immich_asset_id="im-a"),
        r=Photo(id="r", home="review", immich_asset_id="im-r"),
    ))
    cycle.run_once()
    assert immich.created == ["Holiday"] and store.load().albums == {"Holiday": "new-Holiday"}
    assert immich.shared == [("new-Holiday", ["wife"], "viewer")]
    assert immich.contents["new-Holiday"] == {"im-a"}      # Review is state only: no album for it
    cycle.run_once()
    assert immich.shared == [("new-Holiday", ["wife"], "viewer")]    # checked once, not every cycle


def test_live_follows_the_promoted_flags(tmp_path):
    immich = FakeImmich(albums={"LIVE": {"old"}})
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(
        a=Photo(id="a", home="Holiday", live=True, immich_asset_id="im-a"),
        b=Photo(id="b", home="Holiday", live=False, immich_asset_id="im-b"),
    ))
    cycle.run_once()
    assert immich.contents["LIVE"] == {"im-a"}


def test_an_empty_library_does_not_empty_live(tmp_path):
    immich = FakeImmich(albums={"LIVE": {"keep", "these"}})
    cycle, _, _ = build(tmp_path, immich)
    cycle.run_once()
    assert immich.contents["LIVE"] == {"keep", "these"}


def test_an_album_that_could_not_be_shared_is_tried_again_next_cycle(tmp_path):
    immich = FakeImmich(fail_share=True)
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Holiday", immich_asset_id="im-a")))
    cycle.run_once()
    immich.fail_share = False
    cycle.run_once()
    assert immich.shared == [("new-Holiday", ["wife"], "viewer")]


def test_a_step_that_fails_does_not_stop_the_others(tmp_path):
    immich = FakeImmich(albums={"LIVE": set()})

    def boom(album_id):
        raise ImmichError("GET /search/metadata -> 500")

    immich.list_album_assets = boom
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Holiday", immich_asset_id="im-a")))
    cycle.run_once()    # intake and the album syncs all fail to list; nothing raises
    assert store.load().albums == {"Holiday": "new-Holiday"}


def test_nothing_is_shared_when_there_is_nobody_to_share_with(tmp_path):
    immich = FakeImmich()
    cycle, store, _ = build(tmp_path, immich, share=())
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Holiday", immich_asset_id="im-a")))
    cycle.run_once()
    assert immich.shared == []


# ---- publishing, emptying and converting --------------------------------------


class PublishingImmich(FakeImmich):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.deleted = []
        self.role_changes = []
        self.users = []

    def upload_asset(self, path, name):
        return f"up-{len(self.contents)}-{name}"

    def delete_assets(self, ids, force=True):
        self.deleted.append((list(ids), force))

    def get_album(self, album_id):
        return {"ownerId": "pipeline", "isActivityEnabled": True,
                "albumUsers": [{"user": {"id": u}, "role": "editor"} for u in self.users]}

    def update_album_user_role(self, album_id, user_id, role):
        self.role_changes.append((album_id, user_id, role))


def stocked(tmp_path, immich):
    from app.library import Revision
    from app.revisions import RevisionStore

    revisions = RevisionStore(str(tmp_path / "revs"))
    cycle, store, _ = build(tmp_path, immich, revisions=revisions)
    src = tmp_path / "x.jpg"
    src.write_bytes(b"x")
    rel, sha = revisions.save_revision("a", 0, str(src))
    store.update(lambda lib: lib.photos.update(a=Photo(
        id="a", home="Holiday", live=True, revisions=[Revision(n=0, parent=None, file=rel, sha256=sha)], current=0,
    )))
    return cycle, store


def test_a_photo_with_no_asset_is_published_then_lands_in_its_album_and_live(tmp_path):
    immich = PublishingImmich()
    cycle, store = stocked(tmp_path, immich)
    cycle.run_once()
    asset = store.load().photos["a"].immich_asset_id
    assert asset and immich.contents["new-Holiday"] == {asset} and immich.contents["LIVE"] == {asset}


def test_trashing_the_last_live_photo_empties_live_and_binds_the_copy(tmp_path):
    from app import actions

    immich = PublishingImmich()
    cycle, store = stocked(tmp_path, immich)
    cycle.run_once()
    asset = store.load().photos["a"].immich_asset_id
    actions.trash(store, ["a"])
    cycle.run_once()
    assert immich.contents["LIVE"] == set() and immich.contents["new-Holiday"] == set()
    assert immich.deleted == [([asset], False)] and store.load().photos["a"].stale_asset_ids == []


def test_an_editor_shared_output_album_is_converted_to_viewer(tmp_path):
    immich = PublishingImmich()
    immich.users = ["wife"]
    cycle, store = stocked(tmp_path, immich)
    cycle.run_once()
    assert immich.role_changes == [("new-Holiday", "wife", "viewer")]
    cycle.run_once()
    assert len(immich.role_changes) == 1


def test_without_a_revision_store_the_cycle_still_runs(tmp_path):
    immich = PublishingImmich()
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Holiday", immich_asset_id="im-a")))
    cycle.run_once()
    assert immich.contents["new-Holiday"] == {"im-a"}
