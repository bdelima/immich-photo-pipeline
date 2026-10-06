import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.import_cli import resolve_source_album
from app.immich_client import Asset, ImmichError
from app.importer import import_existing
from app.pipeline import Pipeline
from app.state import ImageState, PipelineState, StateStore


class FakeImmich:
    def __init__(self, assets, albums=None, fail_add_for=()):
        self._assets = assets
        self._albums = albums or []
        self.fail_add_for = set(fail_add_for)
        self.added = []  # (album_id, [ids])
        self.created = []
        self.comments = []
        self.deleted = []
        self.removed = []

    def list_album_assets(self, album_id):
        return list(self._assets)

    def list_albums(self):
        return self._albums

    def create_album(self, name):
        self.created.append(name)
        return f"new-{name}"

    def add_assets_to_album(self, album_id, ids):
        if set(ids) & self.fail_add_for:
            raise ImmichError("403 not allowed")
        self.added.append((album_id, list(ids)))

    def remove_assets_from_album(self, album_id, ids):
        self.removed.append((album_id, list(ids)))

    def delete_assets(self, ids, force=True):
        self.deleted.extend(ids)

    def post_comment(self, text, *, album_id, asset_id=None):
        self.comments.append((text, album_id, asset_id))
        return "c1"


def asset(i):
    return Asset(id=i, original_file_name=f"{i}.jpg")


def run(immich, store, target="Everyday", apply=True):
    return import_existing(
        immich, store, source_album_id="src", source_album_name="Screensaver",
        target=target, review_album_id="review-id", apply=apply,
    )


def test_dry_run_changes_nothing(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    immich = FakeImmich([asset("a"), asset("b")])
    report = run(immich, store, apply=False)
    assert report.dry_run and report.imported_ids == ["a", "b"]
    assert immich.added == [] and immich.created == []
    assert not os.path.exists(str(tmp_path / "state.json"))


def test_import_into_managed_album_adds_and_tracks(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    immich = FakeImmich([asset("a"), asset("b")])
    report = run(immich, store)
    assert report.imported_ids == ["a", "b"]
    assert immich.created == ["Everyday"]
    assert immich.added == [("new-Everyday", ["a"]), ("new-Everyday", ["b"])]
    state = store.load()
    assert state.watched_albums == {"Everyday": "new-Everyday"}
    img = state.images["a"]
    assert (img.current_asset_id, img.source_asset_ids, img.home, img.imported) == ("a", ["a"], "Everyday", True)
    # nothing is ever removed from the source or deleted
    assert immich.removed == [] and immich.deleted == []


def test_import_into_existing_album_reuses_it(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    immich = FakeImmich([asset("a")], albums=[{"id": "have-it", "albumName": "Everyday"}])
    run(immich, store)
    assert immich.created == []
    assert immich.added == [("have-it", ["a"])]


def test_import_into_review_adds_to_review(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    immich = FakeImmich([asset("a")])
    run(immich, store, target="review")
    assert immich.added == [("review-id", ["a"])]
    assert store.load().images["a"].home == "review"


def test_already_tracked_assets_are_skipped_and_rerun_is_idempotent(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = PipelineState()
    state.images["x"] = ImageState(source_asset_ids=["orig"], current_asset_id="a", home="review")
    store.save(state)
    immich = FakeImmich([asset("a"), asset("b")])
    first = run(immich, store)
    assert first.already_tracked == ["a"] and first.imported_ids == ["b"]
    second = run(immich, store)
    assert second.imported_ids == [] and sorted(second.already_tracked) == ["a", "b"]


def test_a_photo_that_cannot_be_added_is_skipped_and_reported(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    immich = FakeImmich([asset("a"), asset("b")], fail_add_for={"a"})
    report = run(immich, store)
    assert report.imported_ids == ["b"]
    assert [aid for aid, _ in report.failed] == ["a"]
    assert ("new-Everyday", ["a"]) not in immich.added
    assert "a" not in store.load().images


def test_resolve_source_album_by_name_id_and_errors():
    albums = [{"id": "1", "albumName": "Screensaver"}, {"id": "2", "albumName": "Dup"}, {"id": "3", "albumName": "Dup"}]
    assert resolve_source_album(albums, "Screensaver") == ("1", "Screensaver")
    assert resolve_source_album(albums, "3") == ("3", "Dup")
    with pytest.raises(ValueError):
        resolve_source_album(albums, "Nope")
    with pytest.raises(ValueError):
        resolve_source_album(albums, "Dup")


def test_state_round_trips_imported_flag_and_loads_old_files(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"images": {"a": {"source_asset_ids": ["a"], "current_asset_id": "a", "home": "review", '
                    '"acted_comment_ids": [], "awaiting_clarification": false, "claude_session_id": null}}}')
    store = StateStore(str(path))
    state = store.load()
    assert state.images["a"].imported is False  # old state files predate the field
    state.images["a"].imported = True
    store.save(state)
    assert store.load().images["a"].imported is True


def test_exclusive_lock_can_be_taken_and_released_repeatedly(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    with store.exclusive():
        pass
    with store.exclusive():
        pass


def test_reprocess_refuses_imported_photo_without_touching_it(tmp_path):
    immich = FakeImmich([])

    class NoRecipe:
        def run_single(self, *a, **k):
            raise AssertionError("must not run the recipe on an imported photo")

        run_collage = run_single

    pipeline = Pipeline(config=None, immich=immich, recipe=NoRecipe(), store=None)
    state = PipelineState(images={"a": ImageState(source_asset_ids=["a"], current_asset_id="a", home="Everyday", imported=True)})
    pipeline._reprocess(state, "a", "a", "make it brighter", target_album="album-1")
    assert immich.deleted == []  # the good copy survives
    assert len(immich.comments) == 1 and "no original" in immich.comments[0][0]
    assert state.images["a"].current_asset_id == "a"
