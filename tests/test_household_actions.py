"""What a person can do from the Photos view: the in-pipeline mark, search, send, purge, trash, download."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask

from app import household
from app.immich_client import ImmichClient, ImmichError
from app.library import LibraryStore, Photo, Source
from app.revisions import RevisionStore
from app.webui.household_routes import register_household_routes

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
D = "dddddddd-dddd-dddd-dddd-dddddddddddd"
V = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
OWN, ANNA, BEN = "u-pipeline", "u-anna", "u-ben"
BUCKET = "2026-10-01T00:00:00.000Z"


class Cfg:
    wallpaper_album_id, collage_album_id, import_album_id = "alb-wall", "alb-coll", "alb-imp"


class Up:
    def __init__(self, chunks, headers=None):
        self._chunks, self.headers, self.closed = chunks, headers or {}, False

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class Fake:
    """The pipeline's own account, or one household member's."""
    def __init__(self, me, assets=None, rows=None, results=None, fail=False):
        self.me, self.assets, self.rows, self.results, self.fail = me, assets or {}, rows or [], results or {}, fail
        self.added, self.deleted, self.searched, self.archive_ids = [], [], [], None
        self.add_results = None

    def get_my_user_id(self):
        return self.me

    def get_user_name(self, uid):
        return {ANNA: "Anna", BEN: "Ben"}.get(uid, "")

    def get_asset(self, asset_id):
        return self.assets.get(asset_id)

    def timeline_bucket(self, bucket):
        return self.rows

    def add_assets_to_album(self, album, ids):
        self.added.append((album, list(ids)))
        return self.add_results if self.add_results is not None else [{"id": i, "success": True} for i in ids]

    def delete_assets(self, ids, force=True):
        if self.fail:
            raise ImmichError("DELETE /assets -> 500: down")
        self.deleted.append((list(ids), force))

    def search_assets(self, kind, body):
        self.searched.append((kind, body))
        return self.results

    def suggestions(self, kind, **params):
        return ["Costa Rica"] if kind == "country" else []

    def list_people(self):
        return [{"id": A, "name": "Anna"}]

    def open_original(self, asset_id):
        return Up([b"or", b"ig"], {"Content-Type": "image/jpeg", "Content-Length": "4"})

    def open_archive(self, ids):
        self.archive_ids = list(ids)
        return Up([b"PK"], {"Content-Type": "application/zip"})


def asset(aid, owner=ANNA, kind="IMAGE", name="IMG.jpg"):
    return {"id": aid, "ownerId": owner, "type": kind, "originalFileName": name}


def row(aid, owner=ANNA, **kw):
    base = {"id": aid, "ownerId": owner, "ratio": 1.5, "isVideo": False, "isTrashed": False, "visibility": "timeline",
            "duration": None, "fileCreatedAt": "2026-10-02T01:30:00.000Z", "localOffsetHours": -5}
    base.update(kw)
    return base


def library(tmp_path, **photos):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(photos))
    return store


def photo(pid, *sources, trashed=False):
    return Photo(id=pid, home="review", sources=[Source(asset_id=s) for s in sources], trashed=trashed)


def make(tmp_path, fake, store=None, extra=(), sent=None):
    app = Flask(__name__)
    store = store or library(tmp_path)
    register_household_routes(
        app, fake, None, store=store, revisions=RevisionStore(str(tmp_path / "revs")), cfg=Cfg(), extra_immich=extra,
        on_change=(lambda: sent.append(1)) if sent is not None else None)
    return app.test_client(), store


# ---- the in-pipeline mark ----

def test_pipeline_states_follow_the_library(tmp_path):
    store = library(tmp_path, p1=photo("p1", A), p2=photo("p2", B, trashed=True),
                    p3=photo("p3", C, trashed=True), p4=photo("p4", C))
    states = household.pipeline_states(store.load())
    assert states == {A: "in", B: "trash", C: "in"}          # a live photo wins over a trashed one
    store.update(lambda lib: lib.photos.pop("p2"))           # deleted for good: forgotten
    assert B not in household.pipeline_states(store.load())


def test_month_hides_photos_in_the_pipeline_unless_asked(tmp_path):
    store = library(tmp_path, p1=photo("p1", A), p2=photo("p2", B, trashed=True))
    fake = Fake(OWN, rows=[row(A), row(B), row(C)])
    c, _ = make(tmp_path, fake, store)
    ids = lambda r: {i["id"]: i["pipeline"] for i in r.get_json()["items"]}
    assert ids(c.get("/api/household/month", query_string={"bucket": BUCKET})) == {C: ""}
    assert ids(c.get("/api/household/month", query_string={"bucket": BUCKET, "sent": "1"})) == {A: "in", B: "trash", C: ""}


# ---- search ----

def test_search_body_maps_the_search_types_and_filters():
    kind, body = household.search_body("sunset", "context", {"from": "2025-12-01", "to": "bad", "country": " Costa Rica ",
                                                             "type": "video", "favorites": True, "lens": "35mm"}, 2)
    assert kind == "smart" and body["query"] == "sunset" and body["page"] == 2
    assert body["takenAfter"].startswith("2025-12-01") and "takenBefore" not in body
    assert body["country"] == "Costa Rica" and body["type"] == "VIDEO" and body["isFavorite"] is True and body["lensModel"] == "35mm"
    assert household.search_body("IMG_4", "file_name", {}, 1) == ("metadata", {"page": 1, "size": 200, "withExif": True, "originalFileName": "IMG_4"})
    assert household.search_body("beach", "ocr", {}, 1)[1]["ocr"] == "beach"
    assert household.search_body("", "context", {"city": "Ojochal"}, 1)[0] == "metadata"      # only filters: nothing to match by meaning
    try:
        household.search_body("x", "nope", {}, 1)
        assert False
    except ValueError:
        pass


def test_search_route_leaves_out_what_the_timeline_leaves_out(tmp_path):
    store = library(tmp_path, p1=photo("p1", B))
    items = [
        {**asset(A), "localDateTime": "2026-10-02T20:30:00.000Z", "exifInfo": {"exifImageWidth": 400, "exifImageHeight": 200}},
        asset(B), {**asset(C, owner=BEN, kind="VIDEO"), "duration": 65000, "width": 100, "height": 200},
        asset(D, owner=OWN), {**asset("ffffffff-ffff-ffff-ffff-ffffffffffff"), "isTrashed": True}]
    fake = Fake(OWN, results={"items": items, "next": 3})
    c, _ = make(tmp_path, fake, store)
    out = c.get("/api/household/search", query_string={"q": "x", "mode": "file_name", "page": "2"}).get_json()
    assert [i["id"] for i in out["items"]] == [A, C] and out["next"] == 3 or [i["id"] for i in out["items"]] == [C, A]
    by = {i["id"]: i for i in out["items"]}
    assert by[A]["ratio"] == 2.0 and by[A]["day"] == "2026-10-02" and by[A]["time"] == "20:30" and by[A]["owner"] == "Anna"
    assert by[C]["video"] and by[C]["seconds"] == 65 and by[C]["ratio"] == 0.5
    assert fake.searched[0][1]["page"] == 2
    shown = c.get("/api/household/search", query_string={"q": "x", "sent": "1"}).get_json()["items"]
    assert B in {i["id"] for i in shown} and {i["id"]: i["pipeline"] for i in shown}[B] == "in"
    only = c.get("/api/household/search", query_string={"q": "x", "owner": BEN}).get_json()["items"]
    assert [i["id"] for i in only] == [C]
    assert c.get("/api/household/search", query_string={"mode": "nope"}).status_code == 400
    assert c.get("/api/household/search", query_string={"page": "x"}).status_code == 400


def test_search_error_is_a_502(tmp_path):
    class Down(Fake):
        def search_assets(self, kind, body):
            raise ImmichError("POST /search/smart -> 500: down")
    assert make(tmp_path, Down(OWN))[0].get("/api/household/search", query_string={"q": "x"}).status_code == 502


def test_suggest_and_filters_routes(tmp_path):
    fake = Fake(OWN)
    c, _ = make(tmp_path, fake, extra=[Fake(ANNA), Fake(BEN)])
    assert c.get("/api/household/suggest", query_string={"type": "country"}).get_json() == {"values": ["Costa Rica"]}
    assert c.get("/api/household/suggest", query_string={"type": "../x"}).status_code == 400
    out = c.get("/api/household/filters").get_json()
    assert [o["name"] for o in out["owners"]] == ["Anna", "Ben"] and out["people"] == [{"id": A, "name": "Anna"}]


# ---- send ----

def test_send_adds_to_the_queue_with_the_owners_key_and_skips_what_is_in_the_pipeline(tmp_path):
    store = library(tmp_path, p1=photo("p1", B), p2=photo("p2", C, trashed=True))
    anna = Fake(ANNA)
    fake = Fake(OWN, assets={A: asset(A), B: asset(B), C: asset(C), V: asset(V, kind="VIDEO"),
                             D: asset(D, owner=BEN), "9" * 32 + "0000": asset("9" * 32 + "0000", owner=OWN)})
    sent = []
    c, _ = make(tmp_path, fake, store, extra=[anna], sent=sent)
    out = c.post("/api/household/send", json={"ids": [A, B, C, D], "dest": "wallpaper"}).get_json()
    assert out["done"] == [A] and anna.added == [("alb-wall", [A])] and fake.added == []
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons[B] == "already in the pipeline" and reasons[C] == "in the pipeline's trash" and "no key for Ben" in reasons[D]
    assert sent == [1]
    out = c.post("/api/household/send", json={"ids": [V, A], "dest": "collage"}).get_json()
    assert out["done"] == [A] and {s["id"]: s["reason"] for s in out["skipped"]}[V] == "a video can't go in a collage"
    assert c.post("/api/household/send", json={"ids": [V], "dest": "import"}).get_json()["done"] == [V]


def test_send_reports_a_duplicate_and_a_refusal(tmp_path):
    anna = Fake(ANNA)
    fake = Fake(OWN, assets={A: asset(A)})
    c, _ = make(tmp_path, fake, extra=[anna])
    anna.add_results = [{"id": A, "success": False, "error": "duplicate"}]
    assert c.post("/api/household/send", json={"ids": [A], "dest": "import"}).get_json()["skipped"][0]["reason"] == "already in that queue"
    anna.add_results = [{"id": A, "success": False, "error": "no_permission"}]
    assert "no_permission" in c.post("/api/household/send", json={"ids": [A], "dest": "import"}).get_json()["skipped"][0]["reason"]


def test_send_refuses_a_wrong_request(tmp_path):
    c, _ = make(tmp_path, Fake(OWN))
    for payload in ({"ids": [A]}, {"ids": [A], "dest": "live"}, {"ids": [], "dest": "import"}, {"ids": ["../x"], "dest": "import"},
                    {"dest": "import"}, {"ids": [A] * 1 + [B] * 0, "dest": 5}):
        assert c.post("/api/household/send", json=payload).status_code == 400
    assert c.post("/api/household/send", json={"ids": [A] + [("%032x" % n) for n in range(201)], "dest": "import"}).status_code == 400


def test_send_with_purge_deletes_the_processed_photo_only_after_the_photo_was_sent(tmp_path):
    store = library(tmp_path, p1=photo("p1", C, trashed=True))
    anna = Fake(ANNA)
    sent = []
    c, _ = make(tmp_path, Fake(OWN, assets={C: asset(C)}), store, extra=[anna], sent=sent)
    out = c.post("/api/household/send", json={"ids": [C], "dest": "wallpaper", "purge": True}).get_json()
    assert out["done"] == [C] and out["purged"] == ["p1"] and anna.added == [("alb-wall", [C])]
    assert "p1" not in store.load().photos and household.pipeline_states(store.load()) == {}
    assert sent                                                    # the page and the poll cycle are told


def test_send_without_the_purge_flag_never_purges(tmp_path):
    for flag in (None, False, "yes", 1):
        store = library(tmp_path, p1=photo("p1", C, trashed=True))
        anna = Fake(ANNA)
        c, _ = make(tmp_path, Fake(OWN, assets={C: asset(C)}), store, extra=[anna])
        payload = {"ids": [C], "dest": "wallpaper"}
        if flag is not None:
            payload["purge"] = flag
        out = c.post("/api/household/send", json=payload).get_json()
        assert out["done"] == [] and out["purged"] == [] and anna.added == []
        assert out["skipped"][0]["reason"] == "in the pipeline's trash" and "p1" in store.load().photos


def test_a_send_that_does_not_happen_never_costs_a_processed_photo(tmp_path):
    # Immich refuses, the owner has no key, and a video bound for a collage: the processed photo stays in every case.
    refused = Fake(ANNA)
    refused.add_results = [{"id": C, "success": False, "error": "no_permission"}]
    for n, (aid, assets, dest, anna) in enumerate([
            (C, {C: asset(C)}, "wallpaper", refused),
            (D, {D: asset(D, owner=BEN)}, "wallpaper", Fake(ANNA)),
            (V, {V: asset(V, kind="VIDEO")}, "collage", Fake(ANNA))]):
        here = tmp_path / f"case{n}"
        here.mkdir()
        store = library(here, p1=photo("p1", aid, trashed=True))
        c, _ = make(here, Fake(OWN, assets=assets), store, extra=[anna])
        out = c.post("/api/household/send", json={"ids": [aid], "dest": dest, "purge": True}).get_json()
        assert out["done"] == [] and out["purged"] == [] and len(out["skipped"]) == 1
        assert "p1" in store.load().photos and household.pipeline_states(store.load()) == {aid: "trash"}


def test_send_with_purge_removes_a_whole_collage_and_leaves_live_photos_alone(tmp_path):
    store = library(tmp_path, p1=photo("p1", C, A, trashed=True), p2=photo("p2", B))
    anna = Fake(ANNA)
    c, _ = make(tmp_path, Fake(OWN, assets={C: asset(C), B: asset(B)}), store, extra=[anna])
    out = c.post("/api/household/send", json={"ids": [C, B], "dest": "import", "purge": True}).get_json()
    assert out["done"] == [C] and out["purged"] == ["p1"]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {B: "already in the pipeline"}
    assert set(store.load().photos) == {"p2"}                      # the live photo was not touched; the collage went whole


def test_send_with_purge_treats_an_already_queued_photo_as_sent(tmp_path):
    store = library(tmp_path, p1=photo("p1", C, trashed=True))
    anna = Fake(ANNA)
    anna.add_results = [{"id": C, "success": False, "error": "duplicate"}]
    c, _ = make(tmp_path, Fake(OWN, assets={C: asset(C)}), store, extra=[anna])
    out = c.post("/api/household/send", json={"ids": [C], "dest": "import", "purge": True}).get_json()
    assert out["done"] == [C] and out["purged"] == ["p1"] and "p1" not in store.load().photos


# ---- purge ----

def test_purge_deletes_only_trashed_photos_made_from_the_originals_so_they_can_be_sent_again(tmp_path):
    store = library(tmp_path, p1=photo("p1", A, trashed=True), p2=photo("p2", B), p3=photo("p3", C, D, trashed=True))
    sent = []
    c, _ = make(tmp_path, Fake(OWN), store, sent=sent)
    out = c.post("/api/household/purge", json={"ids": [A, B, C]}).get_json()
    assert sorted(out["purged"]) == ["p1", "p3"]                  # the collage goes as a whole
    lib = store.load()
    assert set(lib.photos) == {"p2"} and household.pipeline_states(lib) == {B: "in"}
    assert sent == [1]
    assert c.post("/api/household/purge", json={"ids": [B]}).get_json()["purged"] == []     # live: left alone
    assert c.post("/api/household/purge", json={"ids": ["x"]}).status_code == 400


# ---- trash ----

def test_trash_moves_photos_to_the_owners_immich_trash(tmp_path):
    anna = Fake(ANNA)
    fake = Fake(OWN, assets={A: asset(A), B: asset(B, owner=OWN), C: asset(C, owner=BEN)})
    c, _ = make(tmp_path, fake, extra=[anna])
    out = c.post("/api/household/trash", json={"ids": [A, B, C]}).get_json()
    assert out["done"] == [A] and anna.deleted == [([A], False)] and fake.deleted == []
    assert {s["id"] for s in out["skipped"]} == {B, C}
    anna.fail = True
    out = c.post("/api/household/trash", json={"ids": [A]}).get_json()
    assert out["done"] == [] and "down" in out["skipped"][0]["reason"]
    assert c.post("/api/household/trash", json={"ids": []}).status_code == 400


# ---- download ----

def test_download_one_and_many(tmp_path):
    fake = Fake(OWN, assets={A: asset(A, name="IMG 1.jpg"), B: asset(B), C: asset(C, owner=OWN)})
    c, _ = make(tmp_path, fake)
    r = c.get(f"/api/household/{A}/download")
    assert r.status_code == 200 and r.data == b"orig" and r.mimetype == "image/jpeg"
    assert r.headers["Content-Disposition"].startswith("attachment") and "IMG" in r.headers["Content-Disposition"]
    assert c.get(f"/api/household/{C}/download").status_code == 404           # the pipeline's own
    assert c.get("/api/household/nope/download").status_code == 404
    r = c.post("/api/household/download", data={"ids": f"{A},{B}"})
    assert r.status_code == 200 and r.data == b"PK" and fake.archive_ids == [A, B] and "photos.zip" in r.headers["Content-Disposition"]
    assert c.post("/api/household/download", data={"ids": f"{A},{C}"}).status_code == 400
    assert c.post("/api/household/download", data={"ids": "x"}).status_code == 400
    assert c.post("/api/household/download", data={}).status_code == 400


def test_client_search_and_download_calls():
    calls = []

    class Session:
        headers = {}

        def post(self, url, **kw):
            calls.append(("POST", url, kw))
            return R()

        def get(self, url, **kw):
            calls.append(("GET", url, kw))
            return R()

    class R:
        ok, status_code, content, headers, text = True, 200, b"", {}, ""

        def close(self):
            pass

    c = ImmichClient("http://immich", "k", session=Session())
    c._request = lambda method, path, **kw: calls.append((method, path, kw)) or {"assets": {"items": [{"id": A}, 5], "nextPage": "2"}}
    assert c.search_assets("smart", {"query": "x"}) == {"items": [{"id": A}], "next": 2}
    assert calls[-1][:2] == ("POST", "/search/smart")
    try:
        c.search_assets("bogus", {})
        assert False
    except ValueError:
        pass
    c.open_archive([A, B])
    assert calls[-1][1] == "http://immich/api/download/archive" and calls[-1][2]["json"] == {"assetIds": [A, B]}
    c.open_original(A)
    assert calls[-1][1] == f"http://immich/api/assets/{A}/original"
