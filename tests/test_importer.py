import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, ImmichError
from app.import_cli import resolve_source_album
from app.importer import import_existing
from app.library import REVIEW, LibraryStore
from app.revisions import RevisionStore


class FakeImmich:
    """Albums hold assets; this account is "pipeline" and owns what it creates."""

    def __init__(self, source_assets, unreadable=(), albums=None):
        self.source = [Asset(id=a, original_file_name=f"{a}.jpg", owner_id="someone") for a in source_assets]
        self.unreadable = set(unreadable)
        self.albums = list(albums or [])
        self.created = []
        self.uploads = []

    def list_album_assets(self, album_id):
        return list(self.source)

    def get_my_user_id(self):
        return "pipeline"

    def list_albums(self):
        return list(self.albums)

    def create_album(self, name):
        self.created.append(name)
        return f"new-{name}"

    def download_asset_original(self, asset_id, dest_dir):
        if asset_id in self.unreadable:
            raise ImmichError(f"GET /assets/{asset_id}/original -> 404")
        path = os.path.join(dest_dir, f"{asset_id}.jpg")
        with open(path, "wb") as fh:
            fh.write(asset_id.encode())
        return path

    def upload_asset(self, path, name):
        self.uploads.append(name)
        return f"up-{name}"


def setup(tmp_path, assets=("a", "b"), **kw):
    return (FakeImmich(assets, **kw), LibraryStore(str(tmp_path / "lib.json")), RevisionStore(str(tmp_path / "rev")))


def run(immich, store, revisions, target="Everyday", apply=True):
    return import_existing(immich, store, revisions, source_album_id="src", source_album_name="Screensaver",
                           target=target, apply=apply)


def test_dry_run_changes_nothing(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = run(immich, store, revisions, apply=False)
    assert report.dry_run and report.imported_ids == ["a", "b"]
    assert immich.created == [] and immich.uploads == []
    assert not os.path.exists(store.path) and not os.path.exists(revisions.root)


def test_import_into_a_managed_album_makes_the_album_and_the_photos(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = run(immich, store, revisions)
    assert report.imported_ids == ["a", "b"] and not report.failed
    lib = store.load()
    assert lib.albums == {"Everyday": "new-Everyday"}
    a = lib.photos["a"]
    assert (a.home, a.imported, a.status, a.current, a.live) == ("Everyday", True, "ready", 0, False)
    assert a.immich_asset_id == "up-a.jpg" and a.revisions[0].origin == "legacy"
    assert open(revisions.path(a.revisions[0].file), "rb").read() == b"a"
    assert [s.asset_id for s in a.sources] == ["a"] and a.sources[0].file == ""


def test_import_into_an_existing_album_this_account_owns_reuses_it(tmp_path):
    immich, store, revisions = setup(tmp_path, albums=[{"id": "alb", "albumName": "Everyday", "ownerId": "pipeline"}])
    run(immich, store, revisions)
    assert immich.created == [] and store.load().albums == {"Everyday": "alb"}


def test_an_album_of_that_name_owned_by_someone_else_is_not_adopted(tmp_path):
    immich, store, revisions = setup(tmp_path, albums=[{"id": "alb", "albumName": "Everyday", "ownerId": "other"}])
    run(immich, store, revisions)
    assert immich.created == ["Everyday"]


def test_import_into_review_needs_no_album(tmp_path):
    immich, store, revisions = setup(tmp_path)
    run(immich, store, revisions, target=REVIEW)
    lib = store.load()
    assert immich.created == [] and lib.albums == {} and lib.photos["a"].home == REVIEW


def test_already_tracked_assets_are_skipped_and_a_rerun_is_idempotent(tmp_path):
    immich, store, revisions = setup(tmp_path)
    run(immich, store, revisions)
    again = run(immich, store, revisions)
    assert again.imported_ids == [] and sorted(again.already_tracked) == ["a", "b"]
    assert len(immich.uploads) == 2


def test_a_photo_that_cannot_be_read_is_skipped_and_reported_without_leftovers(tmp_path):
    immich, store, revisions = setup(tmp_path, unreadable={"a"})
    report = run(immich, store, revisions)
    assert [aid for aid, _ in report.failed] == ["a"] and report.imported_ids == ["b"]
    assert "a" not in store.load().photos
    assert not os.path.exists(os.path.join(revisions.root, "a"))


def test_resolve_source_album_by_name_id_and_errors():
    albums = [{"id": "1", "albumName": "Screensaver"}, {"id": "2", "albumName": "Dup"}, {"id": "3", "albumName": "Dup"}]
    assert resolve_source_album(albums, "Screensaver") == ("1", "Screensaver")
    assert resolve_source_album(albums, "3") == ("3", "Dup")
    with pytest.raises(ValueError):
        resolve_source_album(albums, "Nope")
    with pytest.raises(ValueError):
        resolve_source_album(albums, "Dup")
