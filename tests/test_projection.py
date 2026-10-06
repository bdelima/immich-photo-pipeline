import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, ImmichError
from app.library import Library, Photo
from app.projection import (
    desired_album_ids, desired_live_ids, reconcile_album, sync_albums, sync_live,
)


class FakeImmich:
    def __init__(self, albums=None, failing=()):
        self.albums = {k: set(v) for k, v in (albums or {}).items()}
        self.failing = set(failing)
        self.calls = []

    def list_album_assets(self, album_id):
        if album_id in self.failing:
            raise ImmichError("GET /search/metadata -> 500")
        return [Asset(id=i, original_file_name=f"{i}.jpg") for i in sorted(self.albums.get(album_id, set()))]

    def add_assets_to_album(self, album_id, ids):
        if ids:
            self.calls.append(("add", album_id, list(ids)))
            self.albums.setdefault(album_id, set()).update(ids)

    def remove_assets_from_album(self, album_id, ids):
        if ids:
            self.calls.append(("remove", album_id, list(ids)))
            self.albums.setdefault(album_id, set()).difference_update(ids)


def library():
    return Library(
        photos={
            "a": Photo(id="a", home="Holiday", live=True, immich_asset_id="im-a"),
            "b": Photo(id="b", home="Holiday", live=False, immich_asset_id="im-b"),
            "c": Photo(id="c", home="Everyday", live=True, immich_asset_id="im-c"),
            "d": Photo(id="d", home="Holiday", live=True, trashed=True, immich_asset_id=None),
            "e": Photo(id="e", home="review", live=True, immich_asset_id="im-e"),
            "f": Photo(id="f", home="Holiday", live=True, immich_asset_id=None),   # no result yet
        },
        albums={"Holiday": "alb-h", "Everyday": "alb-e"},
    )


def test_desired_sets_come_from_the_library_only():
    lib = library()
    assert desired_live_ids(lib) == {"im-a", "im-c", "im-e"}        # promoted, not trashed, published
    assert desired_album_ids(lib, "Holiday") == {"im-a", "im-b"}
    assert desired_album_ids(lib, "Nowhere") == set()


def test_reconcile_adds_the_missing_and_removes_the_extra():
    immich = FakeImmich({"live": {"im-a", "stale"}})
    result = reconcile_album(immich, "live", {"im-a", "im-c"})
    assert result.added == ["im-c"] and result.removed == ["stale"] and result.changed
    assert immich.albums["live"] == {"im-a", "im-c"}


def test_reconcile_adds_before_it_removes():
    immich = FakeImmich({"live": {"stale"}})
    reconcile_album(immich, "live", {"new"})
    assert [c[0] for c in immich.calls] == ["add", "remove"]


def test_a_failed_add_removes_nothing():
    class Broken(FakeImmich):
        def add_assets_to_album(self, album_id, ids):
            raise ImmichError("PUT /albums -> 500")

    immich = Broken({"live": {"keep-me"}})
    try:
        reconcile_album(immich, "live", {"new"})
    except ImmichError:
        pass
    assert immich.albums["live"] == {"keep-me"}


def test_an_album_already_right_makes_no_calls():
    immich = FakeImmich({"live": {"x"}})
    result = reconcile_album(immich, "live", {"x"})
    assert not result.changed and immich.calls == []


def test_an_album_is_not_emptied_by_accident():
    immich = FakeImmich({"live": {"x", "y"}})
    result = reconcile_album(immich, "live", set())
    assert result.held_back and result.removed == [] and immich.albums["live"] == {"x", "y"}


def test_an_album_is_emptied_when_that_is_asked_for():
    immich = FakeImmich({"live": {"x", "y"}})
    result = reconcile_album(immich, "live", set(), allow_empty=True)
    assert not result.held_back and sorted(result.removed) == ["x", "y"] and immich.albums["live"] == set()


def test_an_empty_album_stays_empty_without_complaint():
    immich = FakeImmich({"live": set()})
    result = reconcile_album(immich, "live", set())
    assert not result.held_back and not result.changed


def test_sync_live_follows_the_promoted_flags():
    immich = FakeImmich({"LIVE": {"old"}})
    sync_live(immich, library(), "LIVE")
    assert immich.albums["LIVE"] == {"im-a", "im-c", "im-e"}


def test_a_promoted_photo_that_is_trashed_leaves_live():
    lib = library()
    immich = FakeImmich({"LIVE": {"im-a", "im-c", "im-e"}})
    lib.photos["a"].trashed = True
    lib.photos["a"].immich_asset_id = None
    sync_live(immich, lib, "LIVE")
    assert immich.albums["LIVE"] == {"im-c", "im-e"}


def test_sync_albums_does_every_album_and_survives_one_failing():
    lib = library()
    immich = FakeImmich({"alb-h": {"stale"}, "alb-e": set()}, failing={"alb-h"})
    results = sync_albums(immich, lib)
    assert "Holiday" not in results and results["Everyday"].added == ["im-c"]
    assert immich.albums["alb-e"] == {"im-c"} and immich.albums["alb-h"] == {"stale"}


def test_sync_albums_does_not_wipe_every_album_for_a_blank_library():
    immich = FakeImmich({"alb-h": {"x"}, "alb-e": {"y"}})
    blank = Library(albums={"Holiday": "alb-h", "Everyday": "alb-e"})
    results = sync_albums(immich, blank)
    assert all(r.held_back for r in results.values())
    assert immich.albums == {"alb-h": {"x"}, "alb-e": {"y"}}


def test_the_last_promoted_photo_can_be_taken_out_of_live():
    lib = library()
    for photo in lib.photos.values():
        photo.live = False
    immich = FakeImmich({"LIVE": {"im-a"}})
    sync_live(immich, lib, "LIVE")
    assert immich.albums["LIVE"] == set()


def test_live_is_not_emptied_while_a_promoted_photo_still_awaits_its_upload():
    lib = library()
    for photo in lib.photos.values():
        photo.live = photo.id == "f"          # promoted, but no asset yet
    immich = FakeImmich({"LIVE": {"im-a"}})
    sync_live(immich, lib, "LIVE")
    assert immich.albums["LIVE"] == {"im-a"}


def test_the_last_photo_can_be_taken_out_of_an_album():
    lib = library()
    lib.photos["c"].home = "Holiday"
    immich = FakeImmich({"alb-h": set(), "alb-e": {"im-c"}})
    sync_albums(immich, lib)
    assert immich.albums["alb-e"] == set() and "im-c" in immich.albums["alb-h"]
