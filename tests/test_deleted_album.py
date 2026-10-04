"""An album deleted in Immich must not break every poll cycle (seen live:
POST /search/metadata -> 400 for a managed album that no longer existed)."""
import contextlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, ImmichError
from app.pipeline import Pipeline
from app.state import ImageState, PipelineState


class AlbumImmich:
    def __init__(self, gone=(), broken=()):
        self.gone = set(gone)      # deleted: listing fails and get_album says not found
        self.broken = set(broken)  # listing fails but the album exists
        self.added = []
        self.assets = {"review": [], "alb-ok": [Asset(id="b1", original_file_name="b.jpg", is_favorite=True)]}

    def list_album_assets(self, album_id):
        if album_id in self.gone or album_id in self.broken:
            raise ImmichError("POST /search/metadata -> 400: ")
        return list(self.assets.get(album_id, []))

    def get_album(self, album_id):
        if album_id in self.gone:
            raise ImmichError("GET /albums/x -> 400: Not found or no album.read access")
        return {"id": album_id}

    def list_comments(self, *, album_id, asset_id=None):
        return []

    def add_assets_to_album(self, album_id, ids):
        self.added.append((album_id, list(ids)))
        for i in ids:
            self.assets.setdefault(album_id, []).append(Asset(id=i, original_file_name=f"{i}.jpg", is_favorite=True))


class MemoryStore:
    def __init__(self, state):
        self.state = state
        self.saved = 0

    @contextlib.contextmanager
    def exclusive(self):
        yield

    def load(self):
        return self.state

    def save(self, state):
        self.saved += 1


def setup(immich):
    cfg = SimpleNamespace(review_album_id="review", collage_album_id="c", wallpaper_album_id="w")
    state = PipelineState(
        images={
            "A": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="Stray", awaiting_clarification=True, awaiting_album=True),
            "B": ImageState(source_asset_ids=["t"], current_asset_id="b1", home="Holiday"),
        },
        watched_albums={"Stray": "alb-gone", "Holiday": "alb-ok"},
    )
    store = MemoryStore(state)
    return Pipeline(config=cfg, immich=immich, recipe=None, store=store), state, store


def test_a_deleted_album_is_dropped_and_its_photos_return_to_review():
    immich = AlbumImmich(gone=["alb-gone"])
    pipeline, state, _ = setup(immich)
    pipeline._flow3_managed(state)
    assert "Stray" not in state.watched_albums and state.watched_albums == {"Holiday": "alb-ok"}
    a = state.images["A"]
    assert a.home == "review" and not a.awaiting_clarification and not a.awaiting_album
    assert ("review", ["a1"]) in immich.added
    assert state.images["B"].home == "Holiday"  # the healthy album is unaffected


def test_a_failed_listing_of_an_existing_album_is_not_mistaken_for_deletion():
    immich = AlbumImmich(broken=["alb-gone"])
    pipeline, state, _ = setup(immich)
    pipeline._flow3_managed(state)  # logged per item, never raised
    assert state.watched_albums == {"Stray": "alb-gone", "Holiday": "alb-ok"}
    assert state.images["A"].home == "Stray" and immich.added == []


def test_reaping_is_skipped_when_an_album_cannot_be_listed():
    immich = AlbumImmich(broken=["alb-gone"])
    pipeline, state, _ = setup(immich)
    pipeline._reap_deleted(state)
    assert set(state.images) == {"A", "B"}


def test_a_whole_cycle_survives_a_deleted_album_and_saves_state():
    immich = AlbumImmich(gone=["alb-gone"])
    pipeline, state, store = setup(immich)
    pipeline._flow1_wallpaper = pipeline._flow1_collage = lambda s: None
    pipeline._flow2_review = lambda s: None
    pipeline.run_once()
    assert store.saved == 1 and state.images["A"].home == "review"


def test_state_is_saved_even_when_a_step_fails():
    pipeline, state, store = setup(AlbumImmich())

    def boom(s):
        raise ImmichError("down")

    pipeline._flow1_wallpaper = boom
    with pytest.raises(ImmichError):
        pipeline.run_once()
    assert store.saved == 1
