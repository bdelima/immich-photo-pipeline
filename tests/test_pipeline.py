import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, Comment
from app.pipeline import is_approval_reply, is_portrait, new_comments, plan_collage_maker


def make_asset(asset_id, orientation=None, favorite=False):
    return Asset(id=asset_id, original_file_name=f"{asset_id}.jpg",
                 is_favorite=favorite, exif_orientation=orientation)


def test_is_portrait_detects_rotated_exif():
    assert is_portrait(make_asset("a", orientation="6"))
    assert is_portrait(make_asset("a", orientation="8"))
    assert not is_portrait(make_asset("a", orientation="1"))
    assert not is_portrait(make_asset("a", orientation=None))


def test_plan_collage_maker_waits_on_singleton():
    plan = plan_collage_maker([make_asset("a")])
    assert plan.action == "wait"
    assert plan.asset_ids == ["a"]


def test_plan_collage_maker_groups_two():
    plan = plan_collage_maker([make_asset("a"), make_asset("b")])
    assert plan.action == "group"
    assert plan.asset_ids == ["a", "b"]


def test_plan_collage_maker_caps_at_three():
    assets = [make_asset(x) for x in ["a", "b", "c", "d"]]
    plan = plan_collage_maker(assets)
    assert plan.action == "group"
    assert plan.asset_ids == ["a", "b", "c"]


def test_plan_collage_maker_empty_waits_with_nothing():
    plan = plan_collage_maker([])
    assert plan.action == "wait"
    assert plan.asset_ids == []


def test_new_comments_excludes_acted_on_and_own_posts():
    comments = [
        Comment(id="1", text="too pink", user_id="u1", is_own=False),
        Comment(id="2", text="already handled", user_id="u1", is_own=False),
        Comment(id="3", text="applied: darker mat", user_id="pipeline", is_own=True),
    ]
    fresh = new_comments(comments, acted_on={"2"})
    assert [c.id for c in fresh] == ["1"]


def test_is_approval_reply_rejects_blank():
    assert is_approval_reply("  ") is None
    assert is_approval_reply("Holiday") == "Holiday"
