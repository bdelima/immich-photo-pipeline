"""The client marks the comments it posts, so they can be told apart from a
person's (the Immich activity payload has no usable "own" flag)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import PIPELINE_COMMENT_PREFIX, ImmichClient
from tests.test_immich_client import RoutedSession


def _client_with(activities):
    session = RoutedSession({"/activities": activities})
    return ImmichClient("http://immich", "key", session=session), session


def test_own_comments_are_recognized_by_the_marker_not_the_author():
    # Immich's activity payload has no "isOwner", and a person may comment
    # from the very account the pipeline's API key belongs to: that
    # comment must still count as a reviewer instruction.
    client, _ = _client_with([
        {"id": "c1", "comment": "tilt it down 3 degrees", "user": {"id": "pipeline-account"}},
        {"id": "c2", "comment": PIPELINE_COMMENT_PREFIX + "Applied: tilt it", "user": {"id": "pipeline-account"}},
        {"id": "c3", "comment": "too pink", "user": {"id": "wife"}},
    ])
    comments = client.list_comments(album_id="alb")
    assert [(c.id, c.is_own) for c in comments] == [("c1", False), ("c2", True), ("c3", False)]


def test_posted_comments_carry_the_marker_once():
    client, session = _client_with({"id": "new"})
    client.post_comment("Applied: darker", album_id="alb", asset_id="a")
    client.post_comment(PIPELINE_COMMENT_PREFIX + "already marked", album_id="alb", asset_id="a")
    texts = [call[2]["json"]["comment"] for call in session.calls]
    assert texts == [PIPELINE_COMMENT_PREFIX + "Applied: darker", PIPELINE_COMMENT_PREFIX + "already marked"]
