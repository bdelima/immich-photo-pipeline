import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.config import Config
from app.library import LibraryStore, Photo, Revision
from app.revisions import RevisionStore
from app.thumbs import ThumbCache
from app.webui.server import create_app


def build(tmp_path, cfg=None):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "revs"))
    pings = []
    photos = {}
    for pid, home, status in (("a", "review", "ready"), ("b", "Holiday", "ready"), ("c", "inbox", "processing")):
        p = Photo(id=pid, home=home, status=status, immich_asset_id=f"im-{pid}", published_revision=1)
        for n in range(2):
            src = tmp_path / f"{pid}{n}.jpg"
            Image.new("RGB", (40, 30), (n * 100, 10, 10)).save(src)
            rel, sha = revisions.save_revision(pid, n, str(src))
            p.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=rel, sha256=sha))
        p.current = 1
        photos[pid] = p
    store.update(lambda lib: (lib.photos.update(photos), lib.albums.update({"Holiday": "alb-h"})))
    app = create_app(cfg, None, store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / "th")),
                     on_change=lambda: pings.append(1))
    return app.test_client(), store, pings


def post(client, path, **body):
    return client.post(path, json=body)


def test_promote_reports_done_and_skipped_and_nudges_the_cycle(tmp_path):
    client, store, pings = build(tmp_path)
    resp = post(client, "/api/photos/promote", ids=["a", "c"], live=True)
    assert resp.status_code == 200
    assert resp.get_json() == {"done": ["a"], "skipped": [{"id": "c", "reason": "it isn't finished yet"}]}
    assert store.load().photos["a"].live and pings == [1]
    post(client, "/api/photos/promote", ids=["a"], live=False)
    assert not store.load().photos["a"].live


def test_nothing_done_means_no_nudge(tmp_path):
    client, _, pings = build(tmp_path)
    post(client, "/api/photos/promote", ids=["c"])
    assert pings == []


def test_move_to_a_new_album_and_a_bad_name(tmp_path):
    client, store, _ = build(tmp_path)
    assert post(client, "/api/photos/move", ids=["a"], home="Summer").get_json()["done"] == ["a"]
    assert store.load().photos["a"].home == "Summer"
    bad = post(client, "/api/photos/move", ids=["a"], home="Live")
    assert bad.status_code == 400 and "error" in bad.get_json()


def test_the_configured_queue_names_are_reserved(tmp_path):
    cfg = Config(
        immich_url="http://x", immich_api_key="k", collage_album_id="", collage_album_name="Collage Maker",
        wallpaper_album_id="", wallpaper_album_name="Wallpaper Maker", review_album_id="", review_album_name="Review",
        live_album_id="", live_album_name="Screensaver", poll_interval_seconds=1, state_path="", recipe_skill_path="",
        claude_binary="", secrets_file="", claude_auth_check_interval_seconds=1, webui_host="", webui_port=0,
    )
    client, _, _ = build(tmp_path, cfg)
    for name in ("wallpaper maker", "Collage Maker", "screensaver"):
        assert post(client, "/api/photos/move", ids=["a"], home=name).status_code == 400
    assert post(client, "/api/photos/move", ids=["a"], home="Holiday").status_code == 200


def test_trash_then_restore(tmp_path):
    client, store, _ = build(tmp_path)
    assert post(client, "/api/photos/trash", ids=["b", "c"]).get_json() == {
        "done": ["b"], "skipped": [{"id": "c", "reason": "it is being processed"}]}
    assert client.get("/api/photos?view=trash").get_json()["photos"][0]["id"] == "b"
    assert post(client, "/api/photos/restore", ids=["b"]).get_json()["done"] == ["b"]
    assert store.load().photos["b"].home == "Holiday" and not store.load().photos["b"].trashed


def test_bad_requests_are_400(tmp_path):
    client, _, _ = build(tmp_path)
    for path in ("promote", "move", "trash", "restore"):
        assert client.post(f"/api/photos/{path}", json={}).status_code == 400
        assert client.post(f"/api/photos/{path}", data="not json").status_code == 400
    assert post(client, "/api/photos/trash", ids="a").status_code == 400


def test_revert_route(tmp_path):
    client, store, pings = build(tmp_path)
    out = post(client, "/api/photos/a/revert", step=0)
    assert out.status_code == 200 and out.get_json() == {"step": 2}
    assert store.load().photos["a"].current == 2 and pings == [1]
    assert post(client, "/api/photos/a/revert", step=2).get_json() == {"unchanged": True}
    assert pings == [1]
    assert post(client, "/api/photos/a/revert", step=9).status_code == 400
    assert post(client, "/api/photos/nope/revert", step=0).status_code == 400
    assert client.get("/media/a/full").status_code == 200       # the stacked copy can be served


def test_a_failing_nudge_does_not_fail_the_request(tmp_path):
    client, store, _ = build(tmp_path)
    app = create_app(None, None, store, None, revisions=RevisionStore(str(tmp_path / "revs")),
                     thumbs=ThumbCache(str(tmp_path / "th")), on_change=lambda: 1 / 0)
    assert app.test_client().post("/api/photos/promote", json={"ids": ["a"]}).status_code == 200
