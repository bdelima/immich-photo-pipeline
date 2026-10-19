import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset
from app.intake import is_video


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
    def __init__(self, wallpaper=(), collage=(), fail=(), imports=()):
        self.queues = {"wp": list(wallpaper), "co": list(collage), "im": list(imports)}
        self.fail = set(fail)

    def list_album_assets(self, album_id):
        if album_id in self.fail:
            raise ImmichError("GET /search/metadata -> 500")
        return list(self.queues[album_id])


def named(asset_id, name=None, orientation=None):
    return Asset(id=asset_id, original_file_name=name or f"{asset_id}.jpg", exif_orientation=orientation, owner_id="u1")


def scanner(tmp_path, **kw):
    store = LibraryStore(str(tmp_path / "lib.json"))
    return Intake(QueuesImmich(**kw), store, "wp", "co", "im"), store


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


def test_photos_in_collage_maker_wait_and_nothing_is_grouped_for_them(tmp_path):
    intake, store = scanner(tmp_path, collage=[named(c) for c in "abcdefg"])
    assert intake.scan() == [] and store.load().photos == {}


def test_a_video_in_collage_maker_is_recorded_as_failed_and_the_photos_beside_it_keep_waiting(tmp_path):
    intake, store = scanner(tmp_path, collage=[named("v", "clip.mov"), named("a"), named("b")])
    intake.scan()
    lib = store.load()
    assert lib.photos["v"].status == "failed" and "collage" in lib.photos["v"].error
    assert set(lib.photos) == {"v"}


def test_a_queue_that_cannot_be_listed_is_skipped_for_now(tmp_path):
    intake, store = scanner(tmp_path, wallpaper=[named("a")], collage=[named("v", "clip.mov")], fail={"wp"})
    assert intake.scan() == ["v"]
    intake.immich.fail.clear()
    assert intake.scan() == ["a"]


def test_import_only_originals_become_imported_photos_that_need_no_recipe(tmp_path):
    intake, store = scanner(tmp_path, imports=[named("i1"), named("i2", "clip.mov")])
    assert intake.scan() == ["i1", "i2"]
    photo, video = store.load().photos["i1"], store.load().photos["i2"]
    assert (photo.kind, photo.media_type, photo.status, photo.queue, photo.home, photo.imported) == (
        "single", "image", "processing", "import", "inbox", True)
    assert video.media_type == "video" and video.imported
    assert intake.scan() == []                                 # never picked up twice


def test_without_an_import_album_nothing_is_scanned_for_it(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    intake = Intake(QueuesImmich(imports=[named("i1")]), store, "wp", "co")
    assert intake.scan() == []
