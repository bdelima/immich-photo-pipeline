import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.library import (
    REVIEW, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY, Library, LibraryStore, Photo, Revision,
)
from app.revisions import RevisionStore
from app.thumbs import ThumbCache
from app.webui.server import create_app


def make_image(path, size=(800, 600), color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path)
    return str(path)


def build(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    revisions = RevisionStore(str(tmp_path / "revisions"))
    thumbs = ThumbCache(str(tmp_path / "thumbs"), size=100)

    def add(pid, home, status=STATUS_READY, with_image=True, steps=1, **kw):
        photo = Photo(id=pid, home=home, status=status, **kw)
        if with_image:
            for n in range(steps):
                rel, sha = revisions.save_revision(pid, n, make_image(tmp_path / f"{pid}{n}.jpg"))
                photo.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=rel, sha256=sha,
                                                instruction=f"step {n}" if n else ""))
            photo.current = steps - 1
        def fn(lib):
            lib.photos[pid] = photo
            if home not in (REVIEW, "inbox"):
                lib.albums.setdefault(home, "album-id")
        store.update(fn)

    add("r1", REVIEW, steps=3)
    add("a1", "Sunsets", live=True)
    add("q1", "inbox", status=STATUS_PROCESSING, with_image=False)
    add("f1", "inbox", status=STATUS_FAILED, with_image=False, error="boom")
    add("t1", REVIEW, trashed=True, trashed_from=REVIEW)
    add("v1", REVIEW, media_type="video")
    app = create_app(None, None, store, None, revisions=revisions, thumbs=thumbs)
    return app.test_client(), store, revisions


def ids(resp):
    return {p["id"] for p in resp.get_json()["photos"]}


def test_tree_counts(tmp_path):
    client, *_ = build(tmp_path)
    t = client.get("/api/tree").get_json()
    assert t["review"] == 2 and t["live"] == 1 and t["queue"] == 2 and t["trash"] == 1
    assert t["albums"] == [{"name": "Sunsets", "view": "album:sunsets", "count": 1}]


def test_an_album_a_photo_was_just_moved_to_is_listed_before_immich_has_it(tmp_path):
    from app import actions
    client, store, _ = build(tmp_path)
    actions.move(store, ["r1"], "Beach")                    # no Immich album yet
    t = client.get("/api/tree").get_json()
    assert {"name": "Beach", "view": "album:beach", "count": 1} in t["albums"]
    assert ids(client.get("/api/photos?view=album:beach")) == {"r1"}
    assert t["review"] == 1


def test_views(tmp_path):
    client, *_ = build(tmp_path)
    assert ids(client.get("/api/photos?view=review")) == {"r1", "v1"}
    assert ids(client.get("/api/photos?view=live")) == {"a1"}
    assert ids(client.get("/api/photos?view=queue")) == {"q1", "f1"}
    assert ids(client.get("/api/photos?view=trash")) == {"t1"}
    assert ids(client.get("/api/photos?view=album:sunsets")) == {"a1"}
    assert ids(client.get("/api/photos?view=album:Sunsets")) == {"a1"}       # an old link by name
    assert client.get("/api/photos?view=album:Nope").status_code == 400
    assert client.get("/api/photos?view=bogus").status_code == 400


def test_detail_shows_history(tmp_path):
    client, *_ = build(tmp_path)
    d = client.get("/api/photos/r1").get_json()
    assert [h["step"] for h in d["history"]] == [0, 1, 2]
    assert d["history"][2]["current"] and d["history"][2]["instruction"] == "step 2"
    assert all(h["reverts_to_step"] is None for h in d["history"])
    assert client.get("/api/photos/nope").status_code == 404
    assert client.get("/api/photos/f1").get_json()["error"] == "boom"


def test_detail_shows_a_revert_as_a_step(tmp_path):
    from app.revert import revert_to
    client, store, revisions = build(tmp_path)
    assert revert_to(store, revisions, "r1", 0) is not None
    d = client.get("/api/photos/r1").get_json()
    assert [h["step"] for h in d["history"]] == [0, 1, 2, 3]
    assert d["history"][3]["reverts_to_step"] == 0 and d["history"][3]["current"]
    assert client.get("/media/r1/full").status_code == 200


def test_thumbnail_is_small_and_cached(tmp_path):
    client, *_ = build(tmp_path)
    resp = client.get("/media/r1/thumb")
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"
    from io import BytesIO
    assert max(Image.open(BytesIO(resp.data)).size) <= 100
    resp.close()
    assert any(f.endswith(".jpg") for _, _, fs in os.walk(tmp_path / "thumbs") for f in fs)
    assert client.get("/media/r1/thumb").status_code == 200


def test_media_404s(tmp_path):
    client, *_ = build(tmp_path)
    assert client.get("/media/q1/thumb").status_code == 404   # no image yet
    assert client.get("/media/q1/full").status_code == 404
    assert client.get("/media/v1/thumb").status_code == 404   # videos get no thumbnail
    assert client.get("/media/nope/full").status_code == 404
    assert client.get("/media/r1/rev/99").status_code == 404


def test_full_and_revision_files(tmp_path):
    client, *_ = build(tmp_path)
    assert client.get("/media/r1/full").status_code == 200
    resp = client.get("/media/r1/rev/0")
    assert resp.status_code == 200 and "immutable" in resp.headers["Cache-Control"]


def test_cannot_escape_the_store(tmp_path):
    client, store, _ = build(tmp_path)
    secret = tmp_path / "secret.jpg"
    make_image(secret)
    def fn(lib):
        lib.photos["r1"].revisions[-1].file = "../secret.jpg"
    store.update(fn)
    assert client.get("/media/r1/full").status_code == 404
    assert client.get("/media/r1/thumb").status_code == 404


def test_missing_file_is_404_not_500(tmp_path):
    client, store, revisions = build(tmp_path)
    os.unlink(revisions.path(store.load().photos["r1"].current_revision().file))
    assert client.get("/media/r1/full").status_code == 404
    assert client.get("/media/r1/thumb").status_code == 404


def test_page_renders(tmp_path):
    client, *_ = build(tmp_path)
    page = client.get("/").data
    assert b"/static/pipeline.css" in page and b"/static/app.js" in page
    # The sidebar's code is shared with the Recipe rules page, so it lives in app.js.
    assert b"/api/tree" in client.get("/static/app.js").data


def test_every_page_uses_the_one_stylesheet(tmp_path):
    client, *_ = build(tmp_path)
    assert client.get("/static/pipeline.css").status_code == 200
    assert b"/static/pipeline.css" in client.get("/").data


def test_the_original_is_served_and_listed_in_the_detail(tmp_path):
    from app.library import Source
    client, store, revisions = build(tmp_path)
    rel = revisions.save_source("r1", 0, make_image(tmp_path / "orig.jpg", color=(10, 200, 10)), "orig.jpg")
    store.update(lambda lib: lib.photos["r1"].sources.append(Source(asset_id="x", file=rel, name="orig.jpg")))
    d = client.get("/api/photos/r1").get_json()
    assert d["sources"] == [{"index": 0, "name": "orig.jpg", "has_copy": True, "media_type": "image", "meta": {}}]
    full = client.get("/media/r1/source/0")
    assert full.status_code == 200 and "immutable" in full.headers["Cache-Control"]
    thumb = client.get("/media/r1/source/0/thumb")
    assert thumb.status_code == 200 and thumb.mimetype == "image/jpeg"
    thumb.close(); full.close()


def test_the_detail_carries_the_details_of_each_original(tmp_path):
    from app.library import Source
    client, store, _ = build(tmp_path)
    meta = {"taken": "2024-03-02 14:05", "taken_kind": "captured", "place": "Costa Rica", "owner_name": "Bob"}
    store.update(lambda lib: lib.photos["r1"].sources.append(Source(asset_id="x", meta=meta)))
    assert client.get("/api/photos/r1").get_json()["sources"][0]["meta"] == meta


def test_original_routes_404_without_a_stored_copy(tmp_path):
    from app.library import Source
    client, store, _ = build(tmp_path)
    store.update(lambda lib: lib.photos["r1"].sources.append(Source(asset_id="x")))      # no copy yet
    assert client.get("/media/r1/source/0").status_code == 404
    assert client.get("/media/r1/source/0/thumb").status_code == 404
    assert client.get("/media/r1/source/5").status_code == 404
    assert client.get("/media/nope/source/0").status_code == 404


def test_a_video_original_has_no_thumbnail(tmp_path):
    from app.library import Source
    client, store, revisions = build(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really a video")
    rel = revisions.save_source("v1", 0, str(clip), "clip.mp4")
    store.update(lambda lib: lib.photos["v1"].sources.append(Source(asset_id="v", file=rel, name="clip.mp4")))
    assert client.get("/api/photos/v1").get_json()["sources"][0]["media_type"] == "video"
    assert client.get("/media/v1/source/0").status_code == 200
    assert client.get("/media/v1/source/0/thumb").status_code == 404


def test_the_icons_and_the_pages_that_link_them_are_served(tmp_path):
    client, *_ = build(tmp_path)
    assert client.get("/static/icon.svg").status_code == 200
    html = client.get("/").data
    assert b"/static/icon.svg" in html and html.count(b'data:image/png;base64,') == 2
    assert b'rel="apple-touch-icon"' in html
    rules_template = os.path.join(os.path.dirname(__file__), "..", "app", "webui", "templates", "rules.html")
    assert '{% include "_icons.html" %}' in open(rules_template).read()


def test_the_detail_offers_a_watermark_template_and_the_route_queues_it_for_claude(tmp_path):
    from app.library import Source
    client, store, _ = build(tmp_path)
    meta = {"taken": "2024-03-02 14:05", "place": "Costa Rica", "owner_name": "Bob"}
    store.update(lambda lib: lib.photos["r1"].sources.append(Source(asset_id="x", meta=meta)))
    first = client.get("/api/photos/r1").get_json()["watermark"]
    assert (first["title"], first["subtitle"], first["third"], first["remembered"]) == ("Costa Rica", "Bob · March 2024", "", False)
    assert [(x["key"], x["text"]) for x in first["pieces"]] == [("title", ""), ("location", "Costa Rica"), ("author", "Bob"), ("date", "March 2024")]
    assert first["parts"] == {"place": "Costa Rica", "owner": "Bob", "date": "March 2024", "coordinates": None}
    pieces = [{"key": "title", "label": "Title", "text": "Wings at Dusk", "t": True},
              {"key": "location", "label": "Location", "text": "Costa Rica", "u": True},
              {"key": "author", "label": "Author", "text": "Bob", "v": True},
              {"key": "date", "label": "Date", "text": "March 2024", "v": True}]
    ok = client.post("/api/photos/r1/watermark", json={"title": "Wings at Dusk", "subtitle": "Costa Rica", "third": "Bob · March 2024", "pieces": pieces})
    assert ok.status_code == 200 and ok.get_json() == {"id": 1}
    p = store.load().photos["r1"]
    # queued for Claude as a chat request, the form's state kept with it; no step yet
    assert [(m.role, m.text, m.state) for m in p.chat] == [("user", 'Watermark: "Wings at Dusk" / "Costa Rica" / "Bob · March 2024"', "queued")]
    assert p.chat[0].watermark["title"] == "Wings at Dusk" and p.chat[0].watermark["pieces"][0]["t"] == 1 and p.current == 2
    assert client.post("/api/photos/r1/watermark", json={"title": "", "subtitle": ""}).status_code == 400
    assert client.post("/api/photos/r1/watermark", json={"title": "x", "pieces": "bad"}).status_code == 400
    # Once Claude has drawn it, the step keeps the state, and the next time the form opens it
    # starts from what was asked for, with the same ticks.
    from app.library import Revision
    def done(photo):
        photo.revisions.append(Revision(n=3, parent=2, file=photo.revisions[2].file, instruction=photo.chat[0].text, watermark=photo.chat[0].watermark))
        photo.current = 3
        photo.chat[0].state = "done"
    store.update_photo("r1", done)
    again = client.get("/api/photos/r1").get_json()["watermark"]
    assert (again["title"], again["subtitle"], again["third"], again["remembered"]) == ("Wings at Dusk", "Costa Rica", "Bob · March 2024", True)
    assert [(x["key"], x["t"], x["u"], x["v"]) for x in again["pieces"]] == [
        ("title", True, False, False), ("location", False, True, False), ("author", False, False, True), ("date", False, False, True)]
    assert again["parts"]["place"] == "Costa Rica"
    assert client.post("/api/photos/nope/watermark", json={"title": "x"}).status_code == 404
