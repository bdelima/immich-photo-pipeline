"""A thumbs-up in a shared album is an activity, not the favorite flag."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, ImmichClient, ImmichError
from app.pipeline import Pipeline
from app.state import ImageState, PipelineState
from tests.test_immich_client import RoutedSession


def test_list_like_ids_asks_for_like_activities_on_that_asset():
    session = RoutedSession({"/activities": [{"id": "l1", "type": "like"}, {"id": "l2", "type": "like"}]})
    client = ImmichClient("http://immich", "key", session=session)
    assert client.list_like_ids(album_id="alb", asset_id="a") == ["l1", "l2"]
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "http://immich/api/activities")
    assert kwargs["params"] == {"type": "like", "albumId": "alb", "assetId": "a"}


class LikeImmich:
    def __init__(self, likes=(), favorite=False):
        self.likes = list(likes)
        self.favorite = favorite
        self.posted = []
        self.favorites_set = []
        self.moves = []
        self.fail_likes = False

    def list_album_assets(self, album_id):
        return [Asset(id="a1", original_file_name="a.jpg", is_favorite=self.favorite)]

    def list_comments(self, *, album_id, asset_id=None):
        return []

    def list_like_ids(self, *, album_id, asset_id):
        if self.fail_likes:
            raise ImmichError("boom")
        return list(self.likes)

    def post_comment(self, text, *, album_id, asset_id=None):
        self.posted.append(text)
        return f"p{len(self.posted)}"

    def set_favorite(self, asset_id, favorite):
        self.favorites_set.append((asset_id, favorite))

    def create_album(self, name):
        return "album-" + name

    def add_assets_to_album(self, album_id, ids):
        self.moves.append(("add", album_id, list(ids)))

    def remove_assets_from_album(self, album_id, ids):
        self.moves.append(("remove", album_id, list(ids)))


def setup(immich):
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=immich, recipe=None, store=None)
    img = ImageState(source_asset_ids=["src"], current_asset_id="a1", home="review")
    return pipeline, PipelineState(images={"L": img}), img


def test_a_thumbs_up_activity_asks_which_album_exactly_once():
    immich = LikeImmich(likes=["like-1"])
    pipeline, state, img = setup(immich)
    for _ in range(4):
        pipeline._flow2_review(state)
    assert sum("Which album" in t for t in immich.posted) == 1
    assert img.awaiting_clarification and img.acted_like_ids == ["like-1"]


def test_the_favorite_flag_still_asks():
    immich = LikeImmich(favorite=True)
    pipeline, state, img = setup(immich)
    pipeline._flow2_review(state)
    assert sum("Which album" in t for t in immich.posted) == 1


def test_no_like_no_question():
    immich = LikeImmich()
    pipeline, state, img = setup(immich)
    pipeline._flow2_review(state)
    assert immich.posted == [] and not img.awaiting_clarification


def test_a_like_that_cannot_be_looked_up_is_not_fatal():
    immich = LikeImmich(likes=["like-1"])
    immich.fail_likes = True
    pipeline, state, img = setup(immich)
    pipeline._flow2_review(state)
    assert immich.posted == []


def test_a_fresh_like_after_promotion_asks_again():
    immich = LikeImmich(likes=["like-1"])
    pipeline, state, img = setup(immich)
    pipeline._flow2_review(state)
    pipeline._promote_to_album(state, "L", "a1", "Everyday")
    img.home = "review"  # pulled back to Review later
    pipeline._flow2_review(state)  # same old like: no new question
    assert sum("Which album" in t for t in immich.posted) == 1
    immich.likes = ["like-1", "like-2"]  # unliked and liked again
    pipeline._flow2_review(state)
    assert sum("Which album" in t for t in immich.posted) == 2


def test_promotion_sets_the_favorite_flag_so_the_managed_album_keeps_it():
    immich = LikeImmich(likes=["like-1"])
    pipeline, state, img = setup(immich)
    pipeline._promote_to_album(state, "L", "a1", "Everyday")
    assert immich.favorites_set == [("a1", True)]
    assert img.home == "Everyday" and state.watched_albums == {"Everyday": "album-Everyday"}
