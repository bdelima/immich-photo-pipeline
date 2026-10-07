import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import ImmichError
from app.import_legacy import import_legacy, main, read_legacy_state
from app.library import REVIEW, LibraryStore
from app.revisions import RevisionStore


class FakeImmich:
    """Assets are bytes by id; this account is "pipeline"."""

    def __init__(self, assets, albums=None, unreadable=()):
        self.assets = dict(assets)
        self.albums = albums or {}              # id -> album dict
        self.unreadable = set(unreadable)
        self.uploads = []                       # (name, bytes)
        self.created = []
        self._n = 0

    def get_my_user_id(self):
        return "pipeline"

    def get_album(self, album_id):
        if album_id not in self.albums:
            raise ImmichError("GET /albums -> 404")
        return self.albums[album_id]

    def create_album(self, name):
        self.created.append(name)
        return f"new-{name}"

    def download_asset_original(self, asset_id, dest_dir):
        if asset_id in self.unreadable or asset_id not in self.assets:
            raise ImmichError(f"GET /assets/{asset_id}/original -> 404")
        path = os.path.join(dest_dir, f"{asset_id}.jpg")
        with open(path, "wb") as fh:
            fh.write(self.assets[asset_id])
        return path

    def upload_asset(self, path, name):
        self._n += 1
        self.uploads.append((name, open(path, "rb").read()))
        return f"uploaded-{self._n}"


def legacy_state():
    return {
        "watched_albums": {"Everyday": "alb-mine", "Holiday": "alb-theirs"},
        "live_source_album": "Everyday",
        "images": {
            # made by the old pipeline, in Review
            "r1": {"source_asset_ids": ["s-r1"], "current_asset_id": "cur-r1", "home": "review",
                   "revision_notes": ["darker mat"]},
            # in the album that is live
            "e1": {"source_asset_ids": ["s-e1"], "current_asset_id": "cur-e1", "home": "Everyday"},
            # a collage, in another album
            "h1": {"source_asset_ids": ["s-h1a", "s-h1b"], "current_asset_id": "cur-h1", "home": "Holiday",
                   "revision_notes": ["swap the first two", "recenter the left one"]},
            # already finished when the old pipeline first saw it
            "i1": {"source_asset_ids": ["cur-i1"], "current_asset_id": "cur-i1", "home": "Everyday", "imported": True},
            # the old pipeline was still waiting on these
            "w1": {"source_asset_ids": ["s-w1"], "current_asset_id": None, "home": "collage_maker_wait"},
            "q1": {"source_asset_ids": ["s-q1"], "current_asset_id": None, "home": "awaiting_clarification"},
        },
    }


def assets():
    return {k: k.encode() for k in (
        "s-r1", "cur-r1", "s-e1", "cur-e1", "s-h1a", "s-h1b", "cur-h1", "cur-i1")}


def albums():
    return {"alb-mine": {"ownerId": "pipeline"}, "alb-theirs": {"ownerId": "admin"}}


def setup(tmp_path, **kw):
    immich = FakeImmich(assets(), albums(), **kw)
    store = LibraryStore(str(tmp_path / "library.json"))
    revisions = RevisionStore(str(tmp_path / "revisions"))
    return immich, store, revisions


def test_a_dry_run_changes_nothing(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = import_legacy(immich, store, revisions, legacy_state(), apply=False)
    assert report.dry_run and sorted(report.imported) == ["e1", "h1", "i1", "r1"]
    assert sorted(report.skipped_unfinished) == ["q1", "w1"]
    assert immich.uploads == [] and immich.created == []
    assert not os.path.exists(store.path) and not os.path.exists(revisions.root)


def test_processed_photos_come_across_with_their_state(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    lib = store.load()
    assert sorted(lib.photos) == ["e1", "h1", "i1", "r1"] and not report.failed

    r1 = lib.photos["r1"]
    assert (r1.home, r1.kind, r1.status, r1.live, r1.imported) == (REVIEW, "single", "ready", False, False)
    assert r1.legacy_notes == ["darker mat"] and r1.current == 0
    rev0 = r1.current_revision()
    assert rev0.origin == "legacy" and rev0.parent is None and rev0.instruction is None
    assert open(revisions.path(rev0.file), "rb").read() == b"cur-r1"
    assert rev0.sha256 == hashlib.sha256(b"cur-r1").hexdigest()
    assert r1.immich_asset_id.startswith("uploaded-") and r1.published_revision == 0

    h1 = lib.photos["h1"]
    assert h1.kind == "collage" and h1.home == "Holiday" and h1.legacy_notes == ["swap the first two", "recenter the left one"]


def test_originals_are_copied_in_so_the_photo_can_be_revised(tmp_path):
    immich, store, revisions = setup(tmp_path)
    import_legacy(immich, store, revisions, legacy_state(), apply=True)
    h1 = store.load().photos["h1"]
    assert [s.asset_id for s in h1.sources] == ["s-h1a", "s-h1b"]
    assert [open(revisions.path(s.file), "rb").read() for s in h1.sources] == [b"s-h1a", b"s-h1b"]


def test_an_already_finished_photo_has_no_original_and_is_marked_so(tmp_path):
    immich, store, revisions = setup(tmp_path)
    import_legacy(immich, store, revisions, legacy_state(), apply=True)
    i1 = store.load().photos["i1"]
    assert i1.imported is True and all(s.file == "" for s in i1.sources)
    assert i1.revisions[0].file


def test_the_photo_in_the_live_source_album_is_promoted(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    lib = store.load()
    assert sorted(p.id for p in lib.live_photos()) == ["e1", "i1"]
    assert sorted(report.live) == ["e1", "i1"]
    assert lib.photos["h1"].live is False and lib.photos["r1"].live is False


def test_an_album_this_account_owns_is_reused_and_another_accounts_is_replaced(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    assert report.albums == {"Everyday": "reused", "Holiday": "created"}
    assert immich.created == ["Holiday"]
    assert store.load().albums == {"Everyday": "alb-mine", "Holiday": "new-Holiday"}


def test_a_dry_run_reports_albums_without_creating_them(tmp_path):
    immich, store, revisions = setup(tmp_path)
    report = import_legacy(immich, store, revisions, legacy_state(), apply=False)
    assert report.albums == {"Everyday": "reused", "Holiday": "created"} and immich.created == []


def test_running_it_twice_imports_nothing_new(tmp_path):
    immich, store, revisions = setup(tmp_path)
    import_legacy(immich, store, revisions, legacy_state(), apply=True)
    uploads = len(immich.uploads)
    again = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    assert again.imported == [] and sorted(again.already_there) == ["e1", "h1", "i1", "r1"]
    assert len(immich.uploads) == uploads and immich.created == ["Holiday"]


def test_a_missing_original_is_reported_and_the_photo_still_comes_across(tmp_path):
    immich, store, revisions = setup(tmp_path, unreadable={"s-r1"})
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    assert report.missing_sources == [("r1", "s-r1")] and "r1" in report.imported
    r1 = store.load().photos["r1"]
    assert r1.sources[0].asset_id == "s-r1" and r1.sources[0].file == ""


def test_a_photo_whose_image_cannot_be_read_is_skipped_cleanly(tmp_path):
    immich, store, revisions = setup(tmp_path, unreadable={"cur-e1"})
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    assert [pid for pid, _ in report.failed] == ["e1"]
    assert sorted(report.imported) == ["h1", "i1", "r1"]
    assert "e1" not in store.load().photos
    assert not os.path.exists(os.path.join(revisions.root, "e1"))      # nothing half-written is left
    retry = FakeImmich(assets(), albums())
    import_legacy(retry, store, revisions, legacy_state(), apply=True)
    assert "e1" in store.load().photos                                  # and a re-run picks it up


def test_a_failed_upload_leaves_no_partial_photo(tmp_path):
    immich, store, revisions = setup(tmp_path)

    def broken(path, name):
        raise ImmichError("POST /assets -> 500")

    immich.upload_asset = broken
    report = import_legacy(immich, store, revisions, legacy_state(), apply=True)
    assert len(report.failed) == 4 and store.load().photos == {}
    assert os.listdir(revisions.root) == []


def test_read_legacy_state_and_the_command_line(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(legacy_state()))
    assert read_legacy_state(str(path))["live_source_album"] == "Everyday"

    monkeypatch.setenv("IMMICH_URL", "http://immich")
    monkeypatch.setenv("IMMICH_API_KEY", "key")
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("LIBRARY_PATH", str(tmp_path / "library.json"))
    monkeypatch.setenv("REVISIONS_PATH", str(tmp_path / "revisions"))
    fake = FakeImmich(assets(), albums())
    monkeypatch.setattr("app.import_legacy.ImmichClient", lambda url, key: fake)
    assert main(["--legacy", str(path)]) == 0
    out = capsys.readouterr().out
    assert "would import 4 photo(s)" in out and "dry run only" in out
    assert main(["--legacy", str(tmp_path / "missing.json")]) == 2
    assert main(["--legacy", str(path), "--apply"]) == 0
    assert "imported 4 photo(s)" in capsys.readouterr().out
