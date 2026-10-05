"""A thumbs-up in a shared album is an activity (one per album, photo and
account), not the favorite flag. The pipeline gives it again, as the account
that gave it, when a photo moves or is revised; it never sets the favorite
flag."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, Comment, ImmichClient, ImmichError, Like
from app.pipeline import Pipeline
from app.recipe_runner import CommentIntent
from app.state import ImageState, PipelineState
from tests.test_immich_client import RoutedSession
from tests.test_rules import FakeImmich, FakeRecipe, asset, comment, handle, make, state_with


def test_list_likes_asks_for_like_activities_and_says_who_made_them():
    session = RoutedSession({"/activities": [
        {"id": "l1", "type": "like", "user": {"id": "u1", "name": "Bob"}},
        {"id": "l2", "type": "like", "user": {"id": "u2", "name": "Sam"}},
    ]})
    client = ImmichClient("http://immich", "key", session=session)
    assert client.list_likes(album_id="alb", asset_id="a") == [Like("l1", "u1", "Bob"), Like("l2", "u2", "Sam")]
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "http://immich/api/activities")
    assert kwargs["params"] == {"type": "like", "albumId": "alb", "assetId": "a"}


def test_post_like_gives_a_thumbs_up_on_that_photo_in_that_album():
    session = RoutedSession({"/activities": {"id": "l9"}})
    client = ImmichClient("http://immich", "key", session=session)
    assert client.post_like(album_id="alb", asset_id="a") == "l9"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "http://immich/api/activities")
    assert kwargs["json"] == {"albumId": "alb", "assetId": "a", "type": "like"}


class Account(FakeImmich):
    """One Immich account's client. The likes are shared between accounts,
    as they are in Immich: whoever gives one, everyone sees it."""

    def __init__(self, user_id, name, likes, **kw):
        super().__init__(**kw)
        self.user_id, self.name, self.likes = user_id, name, likes
        self.lookup_fails = False
        self.post_fails = False
        self.liked_as = []  # (user_id, album_id, asset_id), in order

    def get_my_user_id(self):
        return self.user_id

    def list_likes(self, *, album_id, asset_id):
        if self.lookup_fails:
            raise ImmichError("GET /activities -> 500")
        return list(self.likes.get((album_id, asset_id), []))

    def post_like(self, *, album_id, asset_id):
        if self.post_fails:
            raise ImmichError("POST /activities -> 400")
        self.liked_as.append((self.user_id, album_id, asset_id))
        like = Like(f"like-{len(self.liked_as)}-{self.user_id}", self.user_id, self.name)
        self.likes.setdefault((album_id, asset_id), []).append(like)
        return like.id

    def create_album(self, name):
        return f"album-{name}"

    def remove_assets_from_album(self, album_id, ids):
        pass


def household(tmp_path, verdict=None, result=None):
    """The pipeline's own account (Bob) plus one extra account (Sam), with
    one set of likes between them."""
    likes = {}
    bob = Account("u-bob", "Bob", likes, assets=[asset("a1")])
    sam = Account("u-sam", "Sam", likes)
    pipeline, _, recipe, rules = make(tmp_path, verdict=verdict, result=result, immich=bob)
    pipeline.extra_clients = [sam]
    return pipeline, bob, sam, likes, recipe


# ---- a thumbs-up in Review is the question "which album?" ---------------------


def review_pipeline(likes):
    immich = Account("u-bob", "Bob", {("review-album", "a1"): likes}, assets=[asset("a1")])
    pipeline = Pipeline(config=SimpleNamespace(review_album_id="review-album"), immich=immich,
                        recipe=FakeRecipe(), store=None, extra_clients=[Account("u-sam", "Sam", {})])
    img = ImageState(source_asset_ids=["src"], current_asset_id="a1", home="review")
    return pipeline, immich, PipelineState(images={"L": img}), img


def test_a_thumbs_up_asks_which_album_exactly_once():
    pipeline, immich, state, img = review_pipeline([Like("like-1", "u-sam", "Sam")])
    for _ in range(4):
        pipeline._flow2_review(state)
    assert sum("Which album" in t for t, _, _ in immich.posted) == 1
    assert img.awaiting_clarification and img.acted_like_ids == ["like-1"]


def test_no_thumbs_up_no_question():
    pipeline, immich, state, img = review_pipeline([])
    pipeline._flow2_review(state)
    assert immich.posted == [] and not img.awaiting_clarification


def test_a_like_that_cannot_be_looked_up_is_not_fatal():
    pipeline, immich, state, img = review_pipeline([Like("like-1", "u-sam", "Sam")])
    immich.lookup_fails = True
    pipeline._flow2_review(state)
    assert immich.posted == []


def test_a_fresh_thumbs_up_after_returning_to_review_asks_again():
    pipeline, immich, state, img = review_pipeline([Like("like-1", "u-sam", "Sam")])
    pipeline._flow2_review(state)
    pipeline._promote_to_album(state, "L", "a1", "Everyday")
    img.home = "review"  # pulled back to Review later
    pipeline._flow2_review(state)  # same old like: no new question
    assert sum("Which album" in t for t, _, _ in immich.posted) == 1
    immich.likes[("review-album", "a1")].append(Like("like-2", "u-sam", "Sam"))  # unliked, liked again
    pipeline._flow2_review(state)
    assert sum("Which album" in t for t, _, _ in immich.posted) == 2


# ---- the thumbs-up follows the photo, as the account that gave it -------------


def move_to_holiday(pipeline, bob):
    state, img = state_with()
    handle(pipeline, state, img, "Holiday")
    return state, img


def test_a_like_by_sam_is_given_again_in_the_new_album_as_sam(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    likes[("review-album", "a1")] = [Like("l1", "u-sam", "Sam")]
    state, img = move_to_holiday(pipeline, bob)
    assert img.home == "Holiday"
    assert sam.liked_as == [("u-sam", "album-Holiday", "a1")]  # one like, made through Sam's own key
    assert bob.liked_as == []
    assert [t for t, _, _ in bob.posted if "couldn't carry" in t] == []


def test_a_like_by_bob_is_given_again_as_bob_and_both_likes_are_kept(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    likes[("review-album", "a1")] = [Like("l1", "u-bob", "Bob"), Like("l2", "u-sam", "Sam"), Like("l3", "u-sam", "Sam")]
    move_to_holiday(pipeline, bob)
    assert bob.liked_as == [("u-bob", "album-Holiday", "a1")]
    assert sam.liked_as == [("u-sam", "album-Holiday", "a1")]  # once, though Sam's like was listed twice


def test_a_like_from_an_unknown_account_is_ignored_and_never_given_as_someone_else(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    likes[("review-album", "a1")] = [Like("l1", "u-kid", "Kid")]
    state, img = move_to_holiday(pipeline, bob)
    assert bob.liked_as == [] and sam.liked_as == []
    assert img.home == "Holiday"  # the move still happens
    assert not any("thumbs-up" in t for t, _, _ in bob.posted)  # nothing to report: it was ignored


def test_a_failed_like_is_reported_and_does_not_stop_the_move(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    likes[("review-album", "a1")] = [Like("l1", "u-sam", "Sam")]
    sam.post_fails = True
    state, img = move_to_holiday(pipeline, bob)
    assert img.home == "Holiday"
    assert any("couldn't carry over the thumbs-up from Sam" in t for t, _, _ in bob.posted)


def test_a_comment_move_without_any_like_carries_nothing(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    state, img = move_to_holiday(pipeline, bob)
    assert bob.liked_as == [] and img.home == "Holiday"


def test_the_likes_are_read_before_the_move_and_the_pipeline_never_sets_the_favorite_flag(tmp_path):
    # the fake has no set_favorite at all, so any call to it would raise
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    likes[("review-album", "a1")] = [Like("l1", "u-sam", "Sam")]
    assert not hasattr(bob, "set_favorite")
    move_to_holiday(pipeline, bob)


def test_a_revision_in_a_managed_album_gives_the_likes_again_on_the_new_photo(tmp_path):
    pipeline, bob, sam, likes, recipe = household(tmp_path, CommentIntent("revise"))
    likes[("alb-h", "a1")] = [Like("l1", "u-sam", "Sam"), Like("l2", "u-bob", "Bob")]
    state, img = state_with(home="Holiday")
    pipeline._handle_fresh_comment(state, "L", img, asset(), comment("darker"), album_id="alb-h", in_review=False)
    assert img.current_asset_id == "new-asset"
    assert bob.liked_as == [("u-bob", "alb-h", "new-asset")]
    assert sam.liked_as == [("u-sam", "alb-h", "new-asset")]
    assert img.like_in_album is False  # seen again next cycle, on the new photo


def test_a_revision_in_review_does_not_touch_likes(tmp_path):
    pipeline, bob, sam, likes, recipe = household(tmp_path, CommentIntent("revise"))
    likes[("review-album", "a1")] = [Like("l1", "u-sam", "Sam")]
    state, img = state_with()
    handle(pipeline, state, img, "darker")
    assert bob.liked_as == []


# ---- unliking in a managed album sends the photo back to Review ---------------


def managed(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("none"))
    pipeline.cfg = SimpleNamespace(review_album_id="review-album")
    state, img = state_with(home="Holiday")
    state.watched_albums["Holiday"] = "alb-h"
    return pipeline, bob, likes, state, img


def test_a_like_that_is_seen_and_then_removed_sends_the_photo_back_to_review(tmp_path):
    pipeline, bob, likes, state, img = managed(tmp_path)
    likes[("alb-h", "a1")] = [Like("l1", "u-sam", "Sam")]
    pipeline._flow3_managed(state)
    assert img.home == "Holiday" and img.like_in_album is True
    likes[("alb-h", "a1")] = []  # unliked
    pipeline._flow3_managed(state)
    assert img.home == "review" and img.like_in_album is False
    assert [(a, i) for _, a, i in bob.posted] == [("review-album", "a1")]
    assert "Moved back to Review" in bob.posted[0][0] and "Holiday" in bob.posted[0][0]


def test_a_photo_that_never_had_a_like_there_is_not_pulled_back(tmp_path):
    # imported photos, or ones whose like could not be carried over
    pipeline, bob, likes, state, img = managed(tmp_path)
    for _ in range(3):
        pipeline._flow3_managed(state)
    assert img.home == "Holiday" and bob.posted == []


def test_a_like_that_cannot_be_looked_up_never_pulls_a_photo_back(tmp_path):
    pipeline, bob, likes, state, img = managed(tmp_path)
    img.like_in_album = True
    bob.lookup_fails = True
    pipeline._flow3_managed(state)
    assert img.home == "Holiday" and img.like_in_album is True


def test_the_whole_round_trip_like_move_unlike(tmp_path):
    pipeline, bob, sam, likes, _ = household(tmp_path, CommentIntent("album", album="Holiday"))
    pipeline.cfg = SimpleNamespace(review_album_id="review-album")
    likes[("review-album", "a1")] = [Like("l1", "u-sam", "Sam")]
    state, img = move_to_holiday(pipeline, bob)
    pipeline._flow3_managed(state)  # sees the like that was carried over
    assert img.home == "Holiday" and img.like_in_album is True
    likes[("album-Holiday", "a1")] = []  # Sam unlikes it
    pipeline._flow3_managed(state)
    assert img.home == "review"


# ---- accounts the pipeline doesn't know cannot drive it ------------------------


def stranger_comment():
    return Comment(id="c-x", text="put this in Holiday", user_id="u-stranger", is_own=False)


def test_a_thumbs_up_from_an_unknown_account_asks_nothing():
    pipeline, immich, state, img = review_pipeline([Like("like-1", "u-stranger", "Stranger")])
    pipeline._flow2_review(state)
    assert immich.posted == [] and not img.awaiting_clarification and img.acted_like_ids == []


def test_a_known_like_still_counts_when_an_unknown_one_is_beside_it():
    pipeline, immich, state, img = review_pipeline(
        [Like("like-1", "u-stranger", "Stranger"), Like("like-2", "u-sam", "Sam")])
    pipeline._flow2_review(state)
    assert img.acted_like_ids == ["like-2"] and img.awaiting_album


def test_a_comment_from_an_unknown_account_is_ignored_in_review():
    pipeline, immich, state, img = review_pipeline([])
    immich._comments = {"a1": [stranger_comment()]}
    pipeline._flow2_review(state)
    assert immich.posted == [] and immich.deleted == [] and img.acted_comment_ids == []
    assert pipeline.recipe.single_calls == []


def test_a_comment_from_an_unknown_account_is_ignored_in_a_managed_album(tmp_path):
    pipeline, bob, likes, state, img = managed(tmp_path)
    bob._comments = {"a1": [stranger_comment()]}
    pipeline.recipe = FakeRecipe(CommentIntent("delete"))  # would delete the photo if it were read
    pipeline._flow3_managed(state)
    assert bob.deleted == [] and bob.posted == [] and "c-x" not in img.acted_comment_ids


def test_a_comment_from_a_known_account_is_still_handled(tmp_path):
    pipeline, bob, likes, state, img = managed(tmp_path)
    bob._comments = {"a1": [Comment(id="c-sam", text="delete this", user_id="u-sam", is_own=False)]}
    pipeline.recipe = FakeRecipe(CommentIntent("delete"))
    pipeline._flow3_managed(state)
    assert bob.deleted == ["a1"]


def test_an_unknown_accounts_like_in_a_managed_album_is_not_seen_and_cannot_hold_a_photo(tmp_path):
    pipeline, bob, likes, state, img = managed(tmp_path)
    likes[("alb-h", "a1")] = [Like("l1", "u-stranger", "Stranger")]
    pipeline._flow3_managed(state)
    assert img.like_in_album is False  # a stranger's like is not a like
    img.like_in_album = True  # a household like was seen earlier, and is gone now
    pipeline._flow3_managed(state)
    assert img.home == "review"  # the stranger's remaining like does not keep it there


def test_when_accounts_cannot_be_resolved_nobody_is_trusted_and_it_recovers(tmp_path):
    pipeline, immich, state, img = review_pipeline([Like("like-1", "u-bob", "Bob")])
    immich.get_my_user_id = lambda: (_ for _ in ()).throw(ImmichError("GET /users/me -> 500"))
    pipeline._flow2_review(state)
    assert immich.posted == [] and not img.awaiting_clarification  # fail closed, not open
    del immich.get_my_user_id  # Immich is back
    pipeline._flow2_review(state)
    assert img.awaiting_album  # keys are retried, and Bob is known again
