import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.library import LibraryStore, Photo, Revision
from app.revisions import RevisionStore
from app.rules import RulesStore
from app.thumbs import ThumbCache
from app.webui.server import create_app


def build(tmp_path, **photo_fields):
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "revs"))
    rules = RulesStore(str(tmp_path / "rules.json"))
    jobs = []
    photo = Photo(id="a", home="review", status="ready", **photo_fields)
    src = tmp_path / "a.jpg"
    Image.new("RGB", (40, 30)).save(src)
    rel, sha = revisions.save_revision("a", 0, str(src))
    photo.revisions.append(Revision(n=0, parent=None, file=rel, sha256=sha))
    photo.current = 0
    store.update(lambda lib: lib.photos.update({"a": photo}))
    app = create_app(None, None, store, None, rules=rules, revisions=revisions,
                     thumbs=ThumbCache(str(tmp_path / "th")), on_job=lambda: jobs.append(1))
    return app.test_client(), store, rules, jobs


def test_posting_a_message_queues_it_and_wakes_the_worker(tmp_path):
    client, store, _, jobs = build(tmp_path)
    r = client.post("/api/photos/a/chat", json={"text": "make it warmer"})
    assert r.status_code == 200 and r.get_json() == {"id": 1} and jobs == [1]
    assert [(m.text, m.state) for m in store.load().photos["a"].chat] == [("make it warmer", "queued")]


def test_a_bad_message_is_a_400_and_does_not_wake_the_worker(tmp_path):
    client, _, _, jobs = build(tmp_path)
    for body in ({"text": ""}, {"text": 5}, {}, None, {"text": "x" * 2000}):
        r = client.post("/api/photos/a/chat", json=body)
        assert r.status_code == 400 and r.get_json()["error"]
    assert jobs == []


def test_a_photo_that_cannot_be_modified_gives_its_reason(tmp_path):
    client, _, _, jobs = build(tmp_path, imported=True)
    r = client.post("/api/photos/a/chat", json={"text": "warmer"})
    assert r.status_code == 400 and "already finished" in r.get_json()["error"] and jobs == []


def test_an_unknown_photo_is_a_404(tmp_path):
    client, *_ = build(tmp_path)
    assert client.post("/api/photos/zzz/chat", json={"text": "hi"}).status_code == 404


def test_the_detail_carries_the_conversation_and_what_blocks_it(tmp_path):
    client, store, rules, _ = build(tmp_path)
    client.post("/api/photos/a/chat", json={"text": "warmer"})
    d = client.get("/api/photos/a").get_json()
    assert d["busy"] is True and d["chat_blocked"] is None and d["proposal"] is None
    assert [(m["role"], m["text"], m["state"]) for m in d["chat"]] == [("user", "warmer", "queued")]
    store.update_photo("a", lambda p: setattr(p, "trashed", True))
    assert "trash" in client.get("/api/photos/a").get_json()["chat_blocked"]


def test_a_pending_proposal_is_offered_with_its_id(tmp_path):
    client, _, rules, _ = build(tmp_path)
    rules.add("thin borders", "single", status="proposed", origin="proposed", lineage_id="a", source_asset_id="a")
    d = client.get("/api/photos/a").get_json()
    assert d["proposal"] == {"id": "r1", "text": "thin borders", "scope": "single"}
    assert client.post("/api/rules/r1/activate").status_code == 200
    assert client.get("/api/photos/a").get_json()["proposal"] is None


def test_a_busy_photo_cannot_be_reverted(tmp_path):
    client, store, _, _ = build(tmp_path)
    store.update_photo("a", lambda p: p.revisions.append(Revision(n=1, parent=0, file=p.revisions[0].file)) or setattr(p, "current", 1))
    client.post("/api/photos/a/chat", json={"text": "warmer"})
    r = client.post("/api/photos/a/revert", json={"step": 0})
    assert r.status_code == 400 and "being revised" in r.get_json()["error"]
    assert client.get("/api/photos?view=review").get_json()["photos"][0]["busy"] is True
