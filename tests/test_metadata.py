import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import metadata
from app.immich_client import ImmichError
from app.library import LibraryStore, Photo, Source


def asset(**kw):
    base = {
        "id": "a", "ownerId": "u1", "owner": {"name": "Bob"},
        "localDateTime": "2024-03-02T14:05:09.000Z", "fileCreatedAt": "2024-03-03T01:00:00.000Z",
        "exifInfo": {"dateTimeOriginal": "2024-03-02T20:05:09.000Z", "city": "Playa Hermosa",
                     "state": "Guanacaste", "country": "Costa Rica", "latitude": 10.5, "longitude": -85.7},
    }
    base.update(kw)
    return base


def test_a_captured_photo_gives_its_local_time_place_and_owner():
    assert metadata.meta_from_asset(asset()) == {
        "taken": "2024-03-02 14:05", "taken_kind": "captured", "place": "Playa Hermosa, Guanacaste, Costa Rica",
        "latitude": 10.5, "longitude": -85.7, "owner_name": "Bob"}


def test_without_a_capture_date_the_file_date_is_used_and_labelled_as_such():
    item = asset(exifInfo={})
    meta = metadata.meta_from_asset(item)
    assert meta["taken"] == "2024-03-03 01:00" and meta["taken_kind"] == "file"
    assert "place" not in meta and "latitude" not in meta


def test_repeated_place_names_are_not_repeated_and_junk_dates_are_dropped():
    item = asset(exifInfo={"city": "Monaco", "country": "Monaco"}, fileCreatedAt="not a date")
    meta = metadata.meta_from_asset(item)
    assert meta["place"] == "Monaco" and "taken" not in meta


def test_coordinates_need_both_values():
    assert "latitude" not in metadata.meta_from_asset(asset(exifInfo={"dateTimeOriginal": "x", "latitude": 1.0}))


class Account:
    def __init__(self, assets=None, error=None, names=None):
        self.assets, self.error, self.names = assets or {}, error, names or {}

    def get_asset(self, asset_id):
        if self.error:
            raise self.error
        return self.assets.get(asset_id)

    def get_user_name(self, user_id):
        return self.names.get(user_id, "")


def test_the_next_account_is_asked_when_the_first_cannot_see_the_asset():
    meta = metadata.fetch([Account(), Account({"a": asset()})], "a")
    assert meta["owner_name"] == "Bob" and meta["taken"] == "2024-03-02 14:05"


def test_the_owner_name_is_looked_up_when_the_asset_does_not_carry_it():
    meta = metadata.fetch([Account({"a": asset(owner=None)}, names={"u1": "Alex"})], "a")
    assert meta["owner_name"] == "Alex"


def test_an_asset_nobody_can_see_is_unavailable_but_an_unreachable_immich_is_not_settled():
    assert metadata.fetch([Account(), Account()], "a") == {"unavailable": True}
    assert metadata.fetch([Account(error=ImmichError("GET /assets/a -> 500"))], "a") is None
    assert metadata.fetch([], "a") is None


def stocked(tmp_path, **sources):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(
        p=Photo(id="p", sources=[Source(asset_id=a, meta=m) for a, m in sources.items()])))
    return store


def test_backfill_fills_missing_details_and_leaves_known_ones_alone(tmp_path):
    store = stocked(tmp_path, a={}, b={"taken": "2020-01-01 10:00"}, c={})
    clients = [Account({"a": asset(), "b": asset()})]
    assert metadata.backfill(clients, store) == 2
    metas = [s.meta for s in store.load().photos["p"].sources]
    assert metas[0]["owner_name"] == "Bob" and metas[1] == {"taken": "2020-01-01 10:00"}
    assert metas[2] == {"unavailable": True}
    assert metadata.backfill(clients, store) == 0            # nothing left to ask about


def test_backfill_retries_later_when_immich_cannot_be_reached_and_respects_its_limit(tmp_path):
    store = stocked(tmp_path, a={}, b={}, c={})
    assert metadata.backfill([Account(error=ImmichError("down"))], store, limit=2) == 2
    assert all(not s.meta for s in store.load().photos["p"].sources)
    assert metadata.backfill([Account({"a": asset(), "b": asset(), "c": asset()})], store, limit=2) == 2
    assert [bool(s.meta) for s in store.load().photos["p"].sources] == [True, True, False]
