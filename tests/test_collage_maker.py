"""The Collage Maker place: photos wait until 2 to 12 are chosen; Make collage records one; Remove takes them out."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask

from app import collage
from app.immich_client import ImmichError
from app.library import LibraryStore, Photo, Source
from app.webui.collage_routes import register_collage_routes

OWN = "u-pipeline"


def aid(n):
    return f"{n:08d}-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def asset(n, **kw):
    return {"id": aid(n), "ownerId": OWN, "originalFileName": f"{n}.jpg", "type": "IMAGE",
            "localDateTime": f"2026-10-{n:02d}T10:00:00.000Z", **kw}


class Cfg:
    collage_album_id = "alb-coll"


class Fake:
    def __init__(self, assets, can_remove=True, only=None):
        self.assets, self.can_remove, self.only, self.removed, self.searches = assets, can_remove, only, [], 0

    def search_assets(self, kind, body):
        self.searches += 1
        return {"items": self.assets, "next": None}

    def get_user_name(self, uid):
        return "Pipeline"

    def remove_assets_from_album(self, album, ids):
        if not self.can_remove or (self.only is not None and any(i not in self.only for i in ids)):
            raise ImmichError("DELETE -> 403")
        self.removed.append((album, list(ids)))
        self.assets[:] = [a for a in self.assets if a["id"] not in ids]


def setup(tmp_path, assets, extra=(), can_remove=True):
    store = LibraryStore(str(tmp_path / "lib.json"))
    fake = Fake(assets, can_remove)
    changes = []
    app = Flask(__name__)
    register_collage_routes(app, fake, store, Cfg(), extra_immich=extra, on_change=lambda: changes.append(1))
    return app.test_client(), fake, store, changes


def test_waiting_lists_photos_without_videos_or_ones_the_pipeline_already_knows(tmp_path):
    assets = [asset(1), asset(2, type="VIDEO"), asset(3), asset(4, isTrashed=True), asset(5)]
    c, fake, store, _ = setup(tmp_path, assets)
    store.update(lambda lib: lib.photos.update(x=Photo(id="x", sources=[Source(asset_id=aid(3))])))
    body = c.get("/api/collage/waiting").get_json()
    assert [i["id"] for i in body["items"]] == [aid(5), aid(1)]          # newest first
    assert body["min"] == 2 and body["max"] == 12


def test_the_count_is_reused_for_a_few_seconds_and_dropped_after_a_change(tmp_path):
    c, fake, _, _ = setup(tmp_path, [asset(1), asset(2)])
    assert c.get("/api/collage/count").get_json() == {"count": 2}
    c.get("/api/collage/count")
    assert fake.searches == 1
    c.post("/api/collage/remove", json={"ids": [aid(1)]})
    assert c.get("/api/collage/count").get_json() == {"count": 1}


def test_make_records_one_collage_in_the_order_chosen_and_nothing_else(tmp_path):
    c, _, store, changes = setup(tmp_path, [asset(n) for n in range(1, 6)])
    r = c.post("/api/collage/make", json={"ids": [aid(4), aid(2), aid(3)]})
    assert r.status_code == 200 and r.get_json()["id"] == aid(4)
    photo = store.load().photos[aid(4)]
    assert photo.kind == "collage" and photo.status == "processing" and photo.queue == "collage"
    assert [s.asset_id for s in photo.sources] == [aid(4), aid(2), aid(3)]
    assert list(store.load().photos) == [aid(4)] and changes == [1]
    # the three are now in the pipeline, so they no longer wait
    assert {i["id"] for i in c.get("/api/collage/waiting").get_json()["items"]} == {aid(1), aid(5)}


def test_make_needs_two_to_twelve_waiting_photos(tmp_path):
    c, _, store, _ = setup(tmp_path, [asset(n) for n in range(1, 14)])
    assert c.post("/api/collage/make", json={"ids": [aid(1)]}).status_code == 400
    assert c.post("/api/collage/make", json={"ids": [aid(n) for n in range(1, 14)]}).status_code == 400
    assert c.post("/api/collage/make", json={"ids": [aid(1), aid(99)]}).status_code == 409   # not waiting
    assert c.post("/api/collage/make", json={"ids": ["nope", aid(1)]}).status_code == 400
    assert c.post("/api/collage/make", json={}).status_code == 400
    assert store.load().photos == {}
    assert c.post("/api/collage/make", json={"ids": [aid(n) for n in range(1, 13)]}).status_code == 200   # twelve is fine


def test_collage_module_refuses_a_set_that_is_already_in_the_pipeline(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    chosen = [{"id": aid(1), "name": "a"}, {"id": aid(2), "name": "b"}]
    collage.make(store, chosen)
    try:
        collage.make(store, chosen)
    except ValueError:
        pass
    else:
        raise AssertionError("made twice")


def test_remove_takes_photos_out_of_the_album_only(tmp_path):
    c, fake, store, changes = setup(tmp_path, [asset(1), asset(2), asset(3)])
    r = c.post("/api/collage/remove", json={"ids": [aid(1), aid(2)]}).get_json()
    assert r == {"done": [aid(1), aid(2)], "skipped": []}
    assert fake.removed == [("alb-coll", [aid(1)]), ("alb-coll", [aid(2)])]
    assert store.load().photos == {} and changes == [1]


def test_remove_tries_the_other_accounts_and_reports_what_could_not_go(tmp_path):
    members = Fake([asset(1)], only={aid(1)})
    c, _, _, _ = setup(tmp_path, [asset(1), asset(2)], extra=(members,), can_remove=False)
    r = c.post("/api/collage/remove", json={"ids": [aid(1), aid(2), aid(7)]}).get_json()
    assert r["done"] == [aid(1)]                          # the member who added it can take it out
    assert {s["id"] for s in r["skipped"]} == {aid(2), aid(7)}


def test_immich_down_is_a_502(tmp_path):
    c, fake, _, _ = setup(tmp_path, [])
    fake.search_assets = lambda kind, body: (_ for _ in ()).throw(ImmichError("down"))
    assert c.get("/api/collage/waiting").status_code == 502
    assert c.get("/api/collage/count").status_code == 502
