"""A thumbs-up in a shared album is an activity (one per album, photo and
account), not the favorite flag. These are the client's requests for them."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import ImmichClient, Like
from tests.test_immich_client import RoutedSession


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
