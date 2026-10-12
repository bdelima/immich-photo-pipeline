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


def build(tmp_path, immich, share=("wife",), revisions=None, **cycle_args):
    store = LibraryStore(str(tmp_path / "lib.json"))
    worker = FakeWorker()
    intake = Intake(immich, store, "wp", "co")
    cycle = Cycle(
        immich, store, intake, worker, live_album_id="LIVE", share_user_ids=list(share), revisions=revisions,
        **cycle_args,
    )
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
    both = [("new-Holiday", ["wife"], "viewer"), ("LIVE", ["wife"], "viewer")]
    assert immich.shared == both
    assert immich.contents["new-Holiday"] == {"im-a"}      # Review has its own album: see the Review tests
    cycle.run_once()
    assert immich.shared == both    # checked once, not every cycle


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
    assert immich.shared == [("new-Holiday", ["wife"], "viewer"), ("LIVE", ["wife"], "viewer")]


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

    def get_asset(self, asset_id):
        return {"id": asset_id, "isTrashed": False}

    def restore_assets(self, ids):
        pass

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
    assert immich.role_changes == [("new-Holiday", "wife", "viewer"), ("LIVE", "wife", "viewer")]
    cycle.run_once()
    assert len(immich.role_changes) == 2


def test_without_a_revision_store_the_cycle_still_runs(tmp_path):
    immich = PublishingImmich()
    cycle, store, _ = build(tmp_path, immich)
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Holiday", immich_asset_id="im-a")))
    cycle.run_once()
    assert immich.contents["new-Holiday"] == {"im-a"}


# ---- Live, Review and the album notes -----------------------------------------


def test_live_is_shared_as_viewers_even_with_an_empty_library(tmp_path):
    immich = FakeImmich()
    cycle, _, _ = build(tmp_path, immich)
    cycle.run_once()
    assert immich.shared == [("LIVE", ["wife"], "viewer")]


def test_live_is_not_shared_when_it_has_no_id_yet(tmp_path):
    immich = FakeImmich()
    store = LibraryStore(str(tmp_path / "lib.json"))
    cycle = Cycle(immich, store, Intake(immich, store, "wp", "co"), FakeWorker(), live_album_id="", share_user_ids=["wife"])
    cycle.run_once()
    assert immich.shared == []


def test_review_holds_the_photos_waiting_in_review_and_is_shared_as_viewers(tmp_path):
    immich = FakeImmich(albums={"REVIEW": {"stale"}})
    cycle, store, _ = build(tmp_path, immich, review_album_id="REVIEW")
    store.update(lambda lib: lib.photos.update(
        r=Photo(id="r", home="review", immich_asset_id="im-r"),
        a=Photo(id="a", home="Holiday", immich_asset_id="im-a"),
    ))
    cycle.run_once()
    assert immich.contents["REVIEW"] == {"im-r"}
    assert ("REVIEW", ["wife"], "viewer") in immich.shared


def test_an_empty_library_does_not_empty_review(tmp_path):
    immich = FakeImmich(albums={"REVIEW": {"keep"}})
    cycle, _, _ = build(tmp_path, immich, review_album_id="REVIEW")
    cycle.run_once()
    assert immich.contents["REVIEW"] == {"keep"}


class NotedImmich(FakeImmich):
    """Keeps a description per album and lists them the way Immich does."""

    def __init__(self, descriptions, **kw):
        super().__init__(**kw)
        self.descriptions = dict(descriptions)
        self.writes = []

    def list_albums(self):
        return [{"id": k, "albumName": k, "description": v} for k, v in self.descriptions.items()]

    def create_album(self, name):
        album_id = super().create_album(name)
        self.descriptions[album_id] = ""
        return album_id

    def update_album_description(self, album_id, description):
        self.writes.append(album_id)
        self.descriptions[album_id] = description


def noted(tmp_path, **kw):
    immich = NotedImmich({"LIVE": "", "REVIEW": "my own words"})
    cycle, store, _ = build(tmp_path, immich, review_album_id="REVIEW", **kw)
    store.update(lambda lib: lib.photos.update(a=Photo(id="a", home="Costa Rica", immich_asset_id="im-a")))
    return immich, cycle


def test_each_managed_album_gets_a_note_that_links_back_to_its_view(tmp_path):
    immich, cycle = noted(tmp_path, pipeline_url="https://pipe.example/")
    cycle.run_once()
    d = immich.descriptions
    assert d["new-Costa Rica"] == (
        "This is a view-only album managed by the Immich Pipeline. To make changes to this album, "
        "go to https://pipe.example/#album:Costa%20Rica")
    assert d["REVIEW"] == (
        "This is a view-only album managed by the Immich Pipeline that holds items waiting for review. "
        "To review items, go to https://pipe.example/#review")
    assert d["LIVE"] == (
        "This is a view-only album managed by the Immich Pipeline that determines wallpapers on connected "
        "display devices. To add or remove wallpapers, go to https://pipe.example/#live")


def test_notes_are_kept_current_but_only_written_when_they_differ(tmp_path):
    immich, cycle = noted(tmp_path, pipeline_url="https://pipe.example")
    cycle.run_once()
    first = len(immich.writes)
    assert first == 3
    cycle.run_once()
    assert len(immich.writes) == first
    immich.descriptions["LIVE"] = "edited by hand"
    cycle.run_once()
    assert immich.writes[first:] == ["LIVE"] and immich.descriptions["LIVE"].endswith("https://pipe.example/#live")


def test_no_notes_are_written_without_a_pipeline_url(tmp_path):
    immich, cycle = noted(tmp_path)
    cycle.run_once()
    assert immich.writes == [] and immich.descriptions["REVIEW"] == "my own words"
