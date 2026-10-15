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
        "latitude": 10.5, "longitude": -85.7, "owner_name": "Bob", "details": 2}


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


def test_an_account_that_is_refused_or_fails_does_not_stop_the_next_one_being_asked():
    refused = Account(error=ImmichError("GET /assets/a -> 403: no access"))
    broken = Account(error=ImmichError("GET /assets/a -> 500"))
    for first in (refused, broken):
        meta = metadata.fetch([first, Account({"a": asset()})], "a")
        assert meta["owner_name"] == "Bob"


def test_refused_everywhere_is_unavailable_but_a_failure_anywhere_leaves_it_unsettled():
    refused = Account(error=ImmichError("GET /assets/a -> 403: no access"))
    broken = Account(error=ImmichError("GET /assets/a -> 500"))
    assert metadata.fetch([refused, Account()], "a") == {"unavailable": True}
    assert metadata.fetch([broken, Account()], "a") is None
    assert metadata.fetch([Account(), broken], "a") is None


def stocked(tmp_path, **sources):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(
        p=Photo(id="p", sources=[Source(asset_id=a, meta=m) for a, m in sources.items()])))
    return store


def test_backfill_fills_missing_details_and_leaves_known_ones_alone(tmp_path):
    store = stocked(tmp_path, a={}, b={"taken": "2020-01-01 10:00", "details": 2}, c={})
    clients, tried = [Account({"a": asset(), "b": asset()})], {}
    assert metadata.backfill(clients, store, tried=tried) == 2
    metas = [s.meta for s in store.load().photos["p"].sources]
    assert metas[0]["owner_name"] == "Bob" and metas[1] == {"taken": "2020-01-01 10:00", "details": 2}
    assert metas[2] == {"unavailable": True}
    assert metadata.backfill(clients, store, tried=tried) == 0            # nothing left to ask about


def test_backfill_retries_later_when_immich_cannot_be_reached_without_starving_the_rest(tmp_path):
    store = stocked(tmp_path, a={}, b={}, c={})
    tried = {}
    assert metadata.backfill([Account(error=ImmichError("down"))], store, limit=2, tried=tried) == 2
    assert all(not s.meta for s in store.load().photos["p"].sources)
    # the two that just failed go to the back of the line
    assert metadata.backfill([Account({"a": asset(), "b": asset(), "c": asset()})], store, limit=2, tried=tried) == 2
    assert [bool(s.meta) for s in store.load().photos["p"].sources] == [True, False, True]


def test_picture_size_camera_and_exposure_are_read_when_present():
    item = asset(exifInfo={"dateTimeOriginal": "2024-03-02T20:05:09.000Z", "exifImageWidth": 4032, "exifImageHeight": 3024,
                           "fileSizeInByte": 3250000, "make": "Google", "model": "Pixel 8 Pro", "exposureTime": "1/250",
                           "fNumber": 1.7000000476837158, "iso": 50})
    meta = metadata.meta_from_asset(item)
    assert (meta["width"], meta["height"], meta["size_bytes"], meta["camera"]) == (4032, 3024, 3250000, "Pixel 8 Pro")
    assert (meta["exposure_time"], meta["f_number"], meta["iso"]) == ("1/250", 1.7, 50)


def test_the_make_stands_in_for_a_missing_model_and_a_numeric_exposure_time_is_written_as_a_fraction():
    meta = metadata.meta_from_asset(asset(exifInfo={"make": "Canon", "exposureTime": 0.004}))
    assert meta["camera"] == "Canon" and meta["exposure_time"] == "1/250"
    assert metadata.meta_from_asset(asset(exifInfo={"exposureTime": 2}))["exposure_time"] == "2"
    plain = metadata.meta_from_asset(asset(exifInfo={}))
    for key in ("width", "height", "size_bytes", "camera", "exposure_time", "f_number", "iso"):
        assert key not in plain


def test_backfill_tops_up_details_kept_before_more_was_read_and_keeps_what_was_there(tmp_path):
    older = {"taken": "2020-01-01 10:00", "place": "Old Place", "owner_name": "Bob"}
    store = stocked(tmp_path, a=dict(older), gone=dict(older), done={**older, "details": 2}, lost={"unavailable": True})
    item = asset(exifInfo={"city": "New City", "model": "Pixel 8 Pro"})
    clients, tried = [Account({"a": item, "done": item})], {}
    assert metadata.backfill(clients, store, tried=tried) == 2                # a, and gone (which Immich no longer has)
    metas = {s.asset_id: s.meta for s in store.load().photos["p"].sources}
    assert metas["a"]["camera"] == "Pixel 8 Pro" and metas["a"]["details"] == 2 and metas["a"]["place"] == "New City"
    assert metas["gone"] == {**older, "details": 2}                           # kept, and not asked about again
    assert metas["done"] == {**older, "details": 2} and metas["lost"] == {"unavailable": True}
    assert metadata.backfill(clients, store, tried=tried) == 0
