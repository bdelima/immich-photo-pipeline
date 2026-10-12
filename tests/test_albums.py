import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.albums import ensure_core_albums, find_album_id_by_name, resolve_or_create_album
from app.config import Config


def make_cfg(**overrides):
    base = dict(
        immich_url="http://immich", immich_api_key="k",
        collage_album_id="", collage_album_name="Collage Maker",
        wallpaper_album_id="", wallpaper_album_name="Wallpaper Maker",
        review_album_id="", review_album_name="Review",
        live_album_id="", live_album_name="Live",
        poll_interval_seconds=15, state_path="/data/state.json",
        recipe_skill_path="/app/photo-mat-recipe", claude_binary="claude",
        secrets_file="/run/secrets/immich_secrets.env",
        claude_auth_check_interval_seconds=300,
        webui_host="0.0.0.0", webui_port=8080,
    )
    base.update(overrides)
    return Config(**base)


class FakeImmich:
    def __init__(self, albums):
        self._albums = albums
        self.created = []
        self._next_id = 100

    def list_albums(self):
        return self._albums

    def create_album(self, name):
        self._next_id += 1
        new_id = f"new-{self._next_id}"
        self.created.append((name, new_id))
        return new_id


def test_find_album_id_by_name_no_match():
    assert find_album_id_by_name([{"id": "1", "albumName": "Other"}], "Review") is None


def test_find_album_id_by_name_single_match():
    albums = [{"id": "1", "albumName": "Review"}, {"id": "2", "albumName": "Other"}]
    assert find_album_id_by_name(albums, "Review") == "1"


def test_find_album_id_by_name_multiple_matches_picks_first():
    albums = [{"id": "1", "albumName": "Review"}, {"id": "2", "albumName": "Review"}]
    assert find_album_id_by_name(albums, "Review") == "1"


def test_resolve_or_create_album_uses_existing():
    fake = FakeImmich([{"id": "1", "albumName": "Review"}])
    assert resolve_or_create_album(fake, fake.list_albums(), "Review") == "1"
    assert fake.created == []


def test_resolve_or_create_album_creates_when_missing():
    fake = FakeImmich([])
    result = resolve_or_create_album(fake, fake.list_albums(), "Review")
    assert result.startswith("new-")
    assert fake.created == [("Review", result)]


def test_ensure_core_albums_prefers_explicit_id_over_name_lookup():
    fake = FakeImmich([{"id": "should-not-be-used", "albumName": "Review"}])
    cfg = make_cfg(review_album_id="explicit-id")
    resolved = ensure_core_albums(fake, cfg)
    assert resolved.review_album_id == "explicit-id"
    # "Review" was pinned explicitly, so it's never looked up or created --
    # the other three weren't pinned, so they still bootstrap as usual.
    assert "Review" not in {name for name, _ in fake.created}


def test_ensure_core_albums_bootstraps_missing_albums_by_name():
    fake = FakeImmich([])
    cfg = make_cfg()
    resolved = ensure_core_albums(fake, cfg)
    assert resolved.collage_album_id.startswith("new-")
    assert resolved.wallpaper_album_id.startswith("new-")
    assert resolved.review_album_id.startswith("new-")
    assert resolved.live_album_id.startswith("new-")
    created_names = {name for name, _ in fake.created}
    assert created_names == {"Collage Maker", "Wallpaper Maker", "Review", "Live"}


def test_ensure_core_albums_reuses_existing_by_name():
    fake = FakeImmich([{"id": "existing-review", "albumName": "Review"}])
    cfg = make_cfg()
    resolved = ensure_core_albums(fake, cfg)
    assert resolved.review_album_id == "existing-review"
    created_names = {name for name, _ in fake.created}
    assert created_names == {"Collage Maker", "Wallpaper Maker", "Live"}


# ---- managed albums (the library's own) ---------------------------------------

from app.albums import ensure_album, ensure_home_albums
from app.library import LibraryStore, Photo


class OwnedImmich(FakeImmich):
    def __init__(self, albums, me="pipeline"):
        super().__init__(albums)
        self.me = me

    def get_my_user_id(self):
        return self.me


def test_ensure_album_reuses_one_this_account_owns_and_remembers_it(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    immich = OwnedImmich([{"id": "1", "albumName": "Holiday", "ownerId": "pipeline"}])
    assert ensure_album(immich, store, "Holiday") == "1"
    assert immich.created == [] and store.load().albums == {"Holiday": "1"}
    immich._albums = []
    assert ensure_album(immich, store, "Holiday") == "1"       # known, so no lookup


def test_ensure_album_does_not_adopt_an_album_owned_by_someone_else(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    immich = OwnedImmich([{"id": "1", "albumName": "Holiday", "ownerId": "other"}])
    assert ensure_album(immich, store, "Holiday") == "new-101"
    assert [n for n, _ in immich.created] == ["Holiday"]


def test_home_albums_are_made_for_homes_without_one_except_review_and_inbox(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(
        a=Photo(id="a", home="Holiday"), b=Photo(id="b", home="review"), c=Photo(id="c", home="inbox"),
        d=Photo(id="d", home="Everyday"),
    ))
    immich = OwnedImmich([{"id": "9", "albumName": "Everyday", "ownerId": "pipeline"}])
    assert ensure_home_albums(immich, store) == ["Everyday", "Holiday"]
    assert sorted(store.load().albums) == ["Everyday", "Holiday"]
    assert ensure_home_albums(immich, store) == []
