import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import actions
from app.actions import ActionError
from app.cycle import Cycle
from app.immich_client import ImmichError
from app.library import DEFAULT_TRASH_DAYS, LibraryStore, Photo, Revision
from app.publish import purge_assets
from app.revisions import RevisionStore
from app.thumbs import ThumbCache
from app.webui.server import create_app

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class FakeImmich:
    def __init__(self):
        self.deleted = []
        self.fail = False

    def delete_assets(self, ids, force=True):
        if self.fail:
            raise ImmichError("DELETE /assets -> 500")
        self.deleted.append((list(ids), force))


def build(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "revs"))

    def photo(pid, **kw):
        p = Photo(id=pid, home="review", **kw)
        src = tmp_path / f"{pid}.jpg"
        src.write_bytes(pid.encode())
        rel, sha = revisions.save_revision(pid, 0, str(src))
        p.revisions.append(Revision(n=0, parent=None, file=rel, sha256=sha))
        p.current = 0
        return p

    store.update(lambda lib: lib.photos.update(
        # trashed: its Immich copy is already in Immich's trash (forgotten) except one still queued
        t1=photo("t1", trashed=True, trashed_at=ago(40), stale_asset_ids=["old-1"]),
        t2=photo("t2", trashed=True, trashed_at=ago(5), stale_asset_ids=["old-2", "old-2b"]),
        t3=photo("t3", trashed=True, trashed_at=None),
        live=photo("live", immich_asset_id="im-live", published_revision=0),
    ))
    return store, revisions


# ---- deleting ---------------------------------------------------------------

def test_only_trashed_photos_can_be_deleted_and_nothing_of_them_is_left(tmp_path):
    store, revisions = build(tmp_path)
    out = actions.delete(store, revisions, ["t1", "live", "nope"])
    assert out.done == ["t1"]
    assert {s["id"]: s["reason"] for s in out.skipped} == {"live": "it isn't in the trash", "nope": "no such photo"}
    library = store.load()
    assert "t1" not in library.photos and "live" in library.photos
    assert not os.path.exists(os.path.join(revisions.root, "t1")) and os.path.isdir(os.path.join(revisions.root, "live"))


def test_the_immich_copies_are_queued_for_deletion_including_ones_still_waiting_for_the_trash(tmp_path):
    store, revisions = build(tmp_path)
    actions.delete(store, revisions, ["t1", "t2"])
    assert store.load().purge_asset_ids == ["old-1", "old-2", "old-2b"]


def test_deleting_needs_ids(tmp_path):
    store, revisions = build(tmp_path)
    with pytest.raises(ActionError):
        actions.delete(store, revisions, [])


# ---- purging Immich ---------------------------------------------------------

def test_purge_deletes_from_immich_for_good_and_forgets_them(tmp_path):
    store, revisions = build(tmp_path)
    actions.delete(store, revisions, ["t1"])
    immich = FakeImmich()
    assert purge_assets(immich, store) == 1
    assert immich.deleted == [(["old-1"], True)]
    assert store.load().purge_asset_ids == []
    assert purge_assets(immich, store) == 0 and len(immich.deleted) == 1


def test_purge_keeps_the_list_when_immich_cannot_be_reached(tmp_path):
    store, revisions = build(tmp_path)
    actions.delete(store, revisions, ["t1"])
    immich = FakeImmich()
    immich.fail = True
    assert purge_assets(immich, store) == 0
    assert store.load().purge_asset_ids == ["old-1"]
    immich.fail = False
    assert purge_assets(immich, store) == 1


def test_purge_never_deletes_an_asset_another_photo_still_shows(tmp_path):
    store, revisions = build(tmp_path)
    store.update(lambda lib: lib.photos["t1"].stale_asset_ids.append("im-live"))
    actions.delete(store, revisions, ["t1"])
    immich = FakeImmich()
    purge_assets(immich, store)
    assert immich.deleted == [(["old-1"], True)]
    assert store.load().purge_asset_ids == []


def test_purge_also_clears_the_asset_from_other_photos_waiting_to_trash_it(tmp_path):
    store, revisions = build(tmp_path)
    store.update(lambda lib: lib.photos["t2"].stale_asset_ids.append("old-1"))   # shared identical bytes
    actions.delete(store, revisions, ["t1"])
    purge_assets(FakeImmich(), store)
    assert "old-1" not in store.load().photos["t2"].stale_asset_ids


# ---- retention --------------------------------------------------------------

def test_the_default_is_30_days_and_an_old_library_file_loads_with_it(tmp_path):
    store, _ = build(tmp_path)
    assert store.load().trash_days == DEFAULT_TRASH_DAYS == 30
    import json
    raw = json.load(open(store.path))
    raw.pop("trash_days"); raw.pop("purge_asset_ids")
    json.dump(raw, open(store.path, "w"))
    assert store.load().trash_days == 30 and store.load().purge_asset_ids == []


def test_set_trash_days_accepts_zero_to_365_and_rejects_the_rest(tmp_path):
    store, _ = build(tmp_path)
    assert actions.set_trash_days(store, 0) == 0 and store.load().trash_days == 0
    assert actions.set_trash_days(store, 365) == 365
    for bad in (-1, 366, "30", 1.5, None, True):
        with pytest.raises(ActionError):
            actions.set_trash_days(store, bad)
    assert store.load().trash_days == 365


def test_expired_photos_are_those_in_the_trash_for_the_period_and_never_when_off(tmp_path):
    store, revisions = build(tmp_path)
    assert actions.expired_ids(store.load(), NOW) == ["t1"]          # t3 has no date: never
    actions.set_trash_days(store, 5)
    assert actions.expired_ids(store.load(), NOW) == ["t1", "t2"]     # exactly 5 days counts
    actions.set_trash_days(store, 0)
    assert actions.expired_ids(store.load(), NOW) == []


def test_delete_expired_removes_them_and_queues_their_immich_copies(tmp_path):
    store, revisions = build(tmp_path)
    assert actions.delete_expired(store, revisions, NOW) == ["t1"]
    library = store.load()
    assert set(library.photos) == {"t2", "t3", "live"} and library.purge_asset_ids == ["old-1"]


def test_the_poll_cycle_deletes_old_trash_and_then_purges_immich_in_the_same_pass(tmp_path):
    store, revisions = build(tmp_path)
    immich = FakeImmich()
    immich.get = lambda *a, **k: None
    cycle = Cycle(immich, store, None, None, live_album_id="", revisions=revisions)
    cycle.run_once()          # every other step fails on the bare fake and is logged; these two still run
    assert "t1" not in store.load().photos
    # the copy still waiting for Immich's trash is deleted for good, not moved there first
    assert immich.deleted[0] == (["old-1"], True) and (["old-1"], False) not in immich.deleted
    assert store.load().purge_asset_ids == []


# ---- the web routes ---------------------------------------------------------

def client_for(tmp_path):
    store, revisions = build(tmp_path)
    changes = []
    app = create_app(None, None, store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / 'thumbs')), on_change=lambda: changes.append(1))
    return app.test_client(), store, revisions, changes


def test_delete_route(tmp_path):
    client, store, revisions, changes = client_for(tmp_path)
    out = client.post("/api/photos/delete", json={"ids": ["t1", "live"]}).get_json()
    assert out["done"] == ["t1"] and out["skipped"][0]["id"] == "live" and changes
    assert client.post("/api/photos/delete", json={"ids": []}).status_code == 400


def test_trash_settings_routes_and_a_shorter_limit_deletes_at_once(tmp_path):
    client, store, revisions, changes = client_for(tmp_path)
    assert client.get("/api/trash-settings").get_json() == {"trash_days": 30}
    old = datetime.now(timezone.utc) - timedelta(days=9)
    store.update(lambda lib: setattr(lib.photos["t2"], "trashed_at", old.isoformat(timespec="seconds")))
    out = client.post("/api/trash-settings", json={"trash_days": 7}).get_json()
    assert out == {"trash_days": 7, "deleted": 2}
    assert set(store.load().photos) == {"t3", "live"} and store.load().trash_days == 7
    assert client.post("/api/trash-settings", json={"trash_days": 0}).get_json() == {"trash_days": 0, "deleted": 0}
    assert client.post("/api/trash-settings", json={"trash_days": 999}).status_code == 400
    assert client.post("/api/trash-settings", json={}).status_code == 400


def test_the_photo_list_carries_when_each_was_trashed_and_the_retention_period(tmp_path):
    client, store, revisions, changes = client_for(tmp_path)
    body = client.get("/api/photos?view=trash").get_json()
    assert body["trash_days"] == 30
    assert {p["id"]: p["trashed_at"] for p in body["photos"]}["t3"] is None


# ---- the page ---------------------------------------------------------------

def _read(*parts):
    return open(os.path.join(os.path.dirname(__file__), "..", *parts), encoding="utf-8").read()


def test_the_trash_page_has_delete_beside_restore_a_confirmation_and_the_retention_button():
    html = _read("app", "webui", "templates", "index.html")
    assert 'onclick="restoreSel()"' in html and 'id="delSel"' in html and "deleteSel" in html
    assert 'id="vRestore"' in html and 'id="vDelete"' in html
    assert 'id="retBtn"' in html and "/api/trash-settings" in html and "/api/photos/delete" in html
    assert "deleted for good (including from Immich's trash)" in html and "the contributor's original" in html
    assert 'cdNo").focus()' in html            # Cancel is the default button
