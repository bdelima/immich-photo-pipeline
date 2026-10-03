import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, ImmichError
from app.pipeline import Pipeline
from app.state import ImageState, PipelineState


class FakeClient:
    """Stands in for an ImmichClient configured with one account's API
    key: succeeds at removal only if that account happens to be the one
    that added the asset -- exactly the real restriction this is testing
    the fallback behavior against."""

    def __init__(self, succeeds=False):
        self.succeeds = succeeds
        self.calls = []

    def remove_assets_from_album(self, album_id, asset_ids):
        self.calls.append((album_id, tuple(asset_ids)))
        if not self.succeeds:
            raise ImmichError("not allowed: asset not added by this account")


class RecordingClient(FakeClient):
    def __init__(self, succeeds=False):
        super().__init__(succeeds)
        self.comments = []

    def post_comment(self, text, *, album_id, asset_id=None):
        self.comments.append((text, album_id, asset_id))
        return "comment-1"


def make_pipeline(primary, extras):
    return Pipeline(config=None, immich=primary, recipe=None, store=None, extra_clients=extras)


def test_try_remove_from_album_succeeds_on_primary():
    primary = FakeClient(succeeds=True)
    pipeline = make_pipeline(primary, [])
    assert pipeline._try_remove_from_album("album-1", "asset-1") is True
    assert primary.calls == [("album-1", ("asset-1",))]


def test_try_remove_from_album_falls_through_to_extra_client():
    primary = FakeClient(succeeds=False)
    extra = FakeClient(succeeds=True)
    pipeline = make_pipeline(primary, [extra])
    assert pipeline._try_remove_from_album("album-1", "asset-1") is True
    assert primary.calls == [("album-1", ("asset-1",))]
    assert extra.calls == [("album-1", ("asset-1",))]


def test_try_remove_from_album_fails_when_no_client_can():
    primary = FakeClient(succeeds=False)
    extra = FakeClient(succeeds=False)
    pipeline = make_pipeline(primary, [extra])
    assert pipeline._try_remove_from_album("album-1", "asset-1") is False


def test_clear_from_entry_queue_comments_when_all_clients_fail():
    primary = RecordingClient(succeeds=False)
    pipeline = make_pipeline(primary, [])
    asset = Asset(id="a1", original_file_name="a1.jpg", is_favorite=False, owner_id="someone-else")
    pipeline._clear_from_entry_queue("album-1", [asset])
    assert len(primary.comments) == 1
    text, album_id, asset_id = primary.comments[0]
    assert album_id == "album-1"
    assert asset_id == "a1"
    assert "safe to delete" in text


def test_clear_from_entry_queue_does_not_comment_when_removal_succeeds():
    primary = RecordingClient(succeeds=True)
    pipeline = make_pipeline(primary, [])
    asset = Asset(id="a1", original_file_name="a1.jpg", is_favorite=False, owner_id="me")
    pipeline._clear_from_entry_queue("album-1", [asset])
    assert primary.comments == []


class DeletingClient(RecordingClient):
    """Unlike FakeClient, this stands in for the primary account deleting
    its OWN upload -- the Review/managed-album case, where there's no
    ownership restriction to simulate, just a delete_assets call that can
    succeed or be made to fail."""

    def __init__(self, delete_succeeds=True):
        super().__init__(succeeds=True)
        self.delete_succeeds = delete_succeeds
        self.deleted_ids = []

    def delete_assets(self, asset_ids, force=True):
        self.deleted_ids.extend(asset_ids)
        if not self.delete_succeeds:
            raise ImmichError("delete failed")


def test_delete_reviewed_asset_removes_asset_and_drops_state_on_success():
    client = DeletingClient(delete_succeeds=True)
    pipeline = make_pipeline(client, [])
    state = PipelineState(images={"lineage-1": ImageState(source_asset_ids=["src-1"], current_asset_id="a1")})

    pipeline._delete_reviewed_asset(state, "lineage-1", "a1", album_id="review-album")

    assert client.deleted_ids == ["a1"]
    assert "lineage-1" not in state.images
    assert client.comments == []


def test_delete_reviewed_asset_comments_and_keeps_state_on_failure():
    client = DeletingClient(delete_succeeds=False)
    pipeline = make_pipeline(client, [])
    state = PipelineState(images={"lineage-1": ImageState(source_asset_ids=["src-1"], current_asset_id="a1")})

    pipeline._delete_reviewed_asset(state, "lineage-1", "a1", album_id="review-album")

    assert "lineage-1" in state.images  # left for retry, not silently dropped
    assert len(client.comments) == 1
    text, album_id, asset_id = client.comments[0]
    assert "couldn't delete" in text.lower()
    assert album_id == "review-album"
    assert asset_id == "a1"
