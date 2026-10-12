import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset
from app.intake import is_portrait, is_video, plan_collage_maker


def make_asset(asset_id, orientation=None):
    return Asset(id=asset_id, original_file_name=f"{asset_id}.jpg", exif_orientation=orientation)


def test_is_portrait_detects_rotated_exif():
    assert is_portrait(make_asset("a", orientation="6"))
    assert is_portrait(make_asset("a", orientation="8"))
    assert not is_portrait(make_asset("a", orientation="1"))
    assert not is_portrait(make_asset("a", orientation=None))


def test_plan_collage_maker_waits_on_singleton():
    plan = plan_collage_maker([make_asset("a")])
    assert plan.action == "wait"
    assert plan.asset_ids == ["a"]


def test_plan_collage_maker_groups_two():
    plan = plan_collage_maker([make_asset("a"), make_asset("b")])
    assert plan.action == "group"
    assert plan.asset_ids == ["a", "b"]


def test_plan_collage_maker_caps_at_three():
    assets = [make_asset(x) for x in ["a", "b", "c", "d"]]
    plan = plan_collage_maker(assets)
    assert plan.action == "group"
    assert plan.asset_ids == ["a", "b", "c"]


def test_plan_collage_maker_empty_waits_with_nothing():
    plan = plan_collage_maker([])
    assert plan.action == "wait"
    assert plan.asset_ids == []


def test_is_video_goes_by_the_file_extension():
    assert is_video(Asset(id="a", original_file_name="clip.MOV"))
    assert is_video(Asset(id="a", original_file_name="x.mp4"))
    assert not is_video(Asset(id="a", original_file_name="x.jpg"))
    assert not is_video(Asset(id="a", original_file_name=""))


# ---- the scan ----------------------------------------------------------------

from app.immich_client import ImmichError
from app.intake import Intake
from app.library import Library, LibraryStore, Photo, Source


class QueuesImmich:
    def __init__(self, wallpaper=(), collage=(), fail=()):
        self.queues = {"wp": list(wallpaper), "co": list(collage)}
        self.fail = set(fail)

    def list_album_assets(self, album_id):
        if album_id in self.fail:
            raise ImmichError("GET /search/metadata -> 500")
        return list(self.queues[album_id])


def named(asset_id, name=None, orientation=None):
    return Asset(id=asset_id, original_file_name=name or f"{asset_id}.jpg", exif_orientation=orientation, owner_id="u1")


def scanner(tmp_path, **kw):
    store = LibraryStore(str(tmp_path / "lib.json"))
    return Intake(QueuesImmich(**kw), store, "wp", "co"), store


def test_each_new_wallpaper_original_becomes_a_queued_photo(tmp_path):
    intake, store = scanner(tmp_path, wallpaper=[named("a"), named("b")])
    assert intake.scan() == ["a", "b"]
    photo = store.load().photos["a"]
    assert (photo.kind, photo.media_type, photo.status, photo.queue, photo.home) == (
        "single", "image", "processing", "wallpaper", "inbox")
    assert photo.sources[0].asset_id == "a" and photo.sources[0].owner_id == "u1" and photo.sources[0].file == ""


def test_a_wallpaper_video_is_a_video_photo(tmp_path):
    intake, store = scanner(tmp_path, wallpaper=[named("v", "trip.mp4")])
    intake.scan()
    photo = store.load().photos["v"]
    assert photo.media_type == "video" and photo.status == "processing"


def test_an_original_already_in_the_library_is_not_picked_up_again(tmp_path):
    intake, store = scanner(tmp_path, wallpaper=[named("a")])
    assert intake.scan() == ["a"]
    assert intake.scan() == []
    # even when the photo was given a different id (a collage is named after its first source)
    store.update(lambda lib: lib.photos.update(c=Photo(id="c", sources=[Source(asset_id="x")])))
    intake.immich.queues["wp"].append(named("x"))
    assert intake.scan() == []


def test_two_portraits_in_collage_maker_become_one_collage(tmp_path):
    intake, store = scanner(tmp_path, collage=[named("a"), named("b")])
    assert intake.scan() == ["a"]
    photo = store.load().photos["a"]
    assert photo.kind == "collage" and [s.asset_id for s in photo.sources] == ["a", "b"]
    assert photo.status == "processing" and photo.queue == "collage"


def test_a_lone_portrait_waits_and_records_nothing(tmp_path):
    intake, store = scanner(tmp_path, collage=[named("a")])
    assert intake.scan() == [] and store.load().photos == {}
    intake.immich.queues["co"].append(named("b"))
    assert intake.scan() == ["a"]


def test_seven_portraits_make_two_collages_and_leave_one_waiting(tmp_path):
    intake, store = scanner(tmp_path, collage=[named(c) for c in "abcdefg"])
    assert intake.scan() == ["a", "d"]
    assert [s.asset_id for s in store.load().photos["d"].sources] == ["d", "e", "f"]
    assert "g" not in {s.asset_id for p in store.load().photos.values() for s in p.sources}


def test_a_video_in_collage_maker_is_recorded_as_failed_and_does_not_join_a_collage(tmp_path):
    intake, store = scanner(tmp_path, collage=[named("v", "clip.mov"), named("a"), named("b")])
    intake.scan()
    lib = store.load()
    assert lib.photos["v"].status == "failed" and "collage" in lib.photos["v"].error
    assert [s.asset_id for s in lib.photos["a"].sources] == ["a", "b"]


def test_a_queue_that_cannot_be_listed_is_skipped_for_now(tmp_path):
    intake, store = scanner(tmp_path, wallpaper=[named("a")], collage=[named("b"), named("c")], fail={"wp"})
    assert intake.scan() == ["b"]
    intake.immich.fail.clear()
    assert intake.scan() == ["a"]
