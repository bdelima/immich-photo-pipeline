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
    assert t["albums"] == [{"name": "Sunsets", "view": "album:Sunsets", "count": 1}]


def test_views(tmp_path):
    client, *_ = build(tmp_path)
    assert ids(client.get("/api/photos?view=review")) == {"r1", "v1"}
    assert ids(client.get("/api/photos?view=live")) == {"a1"}
    assert ids(client.get("/api/photos?view=queue")) == {"q1", "f1"}
    assert ids(client.get("/api/photos?view=trash")) == {"t1"}
    assert ids(client.get("/api/photos?view=album:Sunsets")) == {"a1"}
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
    assert b"/api/tree" in client.get("/").data
