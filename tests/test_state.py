import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.state import ImageState, PipelineState, StateStore


def test_round_trip(tmp_path):
    path = str(tmp_path / "state.json")
    store = StateStore(path)

    state = PipelineState()
    state.images["abc"] = ImageState(
        source_asset_ids=["abc"],
        current_asset_id="def",
        home="review",
        acted_comment_ids=["c1"],
    )
    state.watched_albums["Holiday"] = "album-123"
    state.live_source_album = "Holiday"

    store.save(state)
    loaded = store.load()

    assert loaded.images["abc"].current_asset_id == "def"
    assert loaded.images["abc"].acted_comment_ids == ["c1"]
    assert loaded.watched_albums == {"Holiday": "album-123"}
    assert loaded.live_source_album == "Holiday"


def test_load_missing_file_returns_empty_state(tmp_path):
    store = StateStore(str(tmp_path / "does-not-exist.json"))
    state = store.load()
    assert state.images == {}
    assert state.watched_albums == {}
    assert state.live_source_album is None


def test_save_is_atomic_no_partial_file_left_behind(tmp_path):
    path = str(tmp_path / "state.json")
    store = StateStore(path)
    store.save(PipelineState())
    leftovers = [f for f in os.listdir(tmp_path) if f != "state.json"]
    assert leftovers == []
