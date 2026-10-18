import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from flask import Flask

from app import household
from app.immich_client import ImmichClient, ImmichError
from app.webui.household_routes import register_household_routes

ID1 = "11111111-1111-1111-1111-111111111111"
ID2 = "22222222-2222-2222-2222-222222222222"
ID3 = "33333333-3333-3333-3333-333333333333"
ME, ANNA, BEN = "u-me", "u-anna", "u-ben"
BUCKET = "2026-10-01T00:00:00.000Z"


class Resp:
    def __init__(self, ok=True, status_code=200, content=b"", headers=None, text="", chunks=()):
        self.ok, self.status_code, self.content, self.headers, self.text = ok, status_code, content, headers or {}, text
        self._chunks, self.closed = chunks, False

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class Session:
    def __init__(self, resp=None):
        self.headers, self.resp, self.calls = {}, resp, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


def client(handler):
    c = ImmichClient("http://immich", "k", session=Session())
    c._request = lambda method, path, **kw: handler(method, path, kw.get("params") or {})
    return c


# ---- client ----

def test_columnar_timeline_bucket():
    body = {"id": [ID1, ID2], "ownerId": [ANNA, BEN], "ratio": [1.5, 0.75], "isImage": [True, False],
            "isTrashed": [False, False], "visibility": ["timeline", "timeline"], "duration": [None, "0:01:05.000"],
            "fileCreatedAt": ["2026-10-02T01:30:00.000Z"] * 2, "localOffsetHours": [-5, -5]}
    c = client(lambda m, p, q: body)
    rows = c.timeline_bucket(BUCKET)
    assert [r["id"] for r in rows] == [ID1, ID2]
    assert [r["isVideo"] for r in rows] == [False, True]
    assert rows[1]["duration"] == "0:01:05.000" and rows[0]["ownerId"] == ANNA


def test_columnar_ignores_mismatched_column():
    body = {"id": [ID1, ID2], "ratio": [1.0], "isImage": [True, True]}
    rows = client(lambda m, p, q: body).timeline_bucket(BUCKET)
    assert len(rows) == 2 and rows[0]["ratio"] is None


def test_legacy_list_timeline_bucket():
    body = [{"id": ID1, "ownerId": ANNA, "type": "VIDEO", "isArchived": True, "isTrashed": False,
             "fileCreatedAt": "2026-10-02T01:30:00.000Z", "exifInfo": {"exifImageWidth": 400, "exifImageHeight": 200}}]
    row = client(lambda m, p, q: body).timeline_bucket(BUCKET)[0]
    assert row["isVideo"] and row["visibility"] == "archive" and row["ratio"] == 2.0


def test_timeline_retries_with_size_on_400():
    seen = []

    def handler(method, path, params):
        seen.append(dict(params))
        if "size" not in params:
            raise ImmichError("GET /timeline/buckets -> 400: size must be provided")
        return [{"timeBucket": BUCKET, "count": 3}]

    out = client(handler).timeline_buckets()
    assert out[0]["count"] == 3 and seen[1]["size"] == "MONTH" and seen[0]["withPartners"] == "true"


def test_timeline_other_errors_propagate():
    def handler(method, path, params):
        raise ImmichError("GET /timeline/buckets -> 500: boom")

    with pytest.raises(ImmichError):
        client(handler).timeline_buckets()


def test_fetch_image_and_error():
    c = ImmichClient("http://immich", "k", session=Session(Resp(content=b"jpg", headers={"content-type": "image/webp"})))
    assert c.fetch_image(ID1, "preview") == (b"jpg", "image/webp")
    url, kw = c._session.calls[0]
    assert url.endswith(f"/api/assets/{ID1}/thumbnail") and kw["params"] == {"size": "preview"}
    bad = ImmichClient("http://immich", "k", session=Session(Resp(ok=False, status_code=404, text="no")))
    with pytest.raises(ImmichError):
        bad.fetch_image(ID1)


def test_open_video_passes_range_and_streams():
    c = ImmichClient("http://immich", "k", session=Session(Resp(status_code=206)))
    c.open_video(ID1, "bytes=0-9")
    url, kw = c._session.calls[0]
    assert url.endswith(f"/api/assets/{ID1}/video/playback") and kw["stream"] is True
    assert kw["headers"]["Range"] == "bytes=0-9"


# ---- household logic ----

def row(id, owner=ANNA, **kw):
    r = {"id": id, "ownerId": owner, "ratio": 1.5, "isVideo": False, "isTrashed": False, "visibility": "timeline",
         "duration": None, "fileCreatedAt": "2026-10-02T01:30:00.000Z", "localOffsetHours": -5}
    r.update(kw)
    return r


class FakeImmich:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail = list(rows), fail

    def timeline_buckets(self):
        if self.fail:
            raise ImmichError("GET /timeline/buckets -> 500: down")
        return [{"timeBucket": "2026-09-01T00:00:00.000Z", "count": 2}, {"timeBucket": BUCKET, "count": 5}]

    def timeline_bucket(self, b):
        return self.rows

    def get_my_user_id(self):
        return ME

    def get_user_name(self, uid):
        return {ANNA: "Anna", BEN: "Ben"}.get(uid, "")

    def fetch_image(self, asset_id, size):
        if self.fail:
            raise ImmichError("GET thumbnail -> 404: gone")
        return b"IMG-" + size.encode(), "image/jpeg"

    def open_video(self, asset_id, range_header=None):
        self.range = range_header
        return Resp(status_code=206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-2/9", "X-Secret": "no"},
                    chunks=[b"ab", b"c"])

    def get_asset(self, asset_id):
        return {"id": asset_id, "ownerId": ANNA, "originalFileName": "IMG_1.jpg", "type": "IMAGE"}


def test_months_newest_first():
    out = household.months(FakeImmich())
    assert [m["month"] for m in out] == ["2026-10", "2026-09"] and out[0]["count"] == 5


def test_month_items_filters_and_local_day():
    rows = [row(ID1), row(ID2, owner=ME), row(ID3, isTrashed=True), row("x" * 36, visibility="archive"),
            row("", owner=ANNA), row("44444444-4444-4444-4444-444444444444", isVideo=True, duration="0:00:07.500",
                                     fileCreatedAt="2026-10-02T03:00:00.000Z")]
    items = household.month_items(FakeImmich(rows), BUCKET, ME)
    assert [i["id"] for i in items] == ["44444444-4444-4444-4444-444444444444", ID1]
    assert items[1]["day"] == "2026-10-01" and items[1]["time"] == "20:30"   # 01:30Z at -5h is the evening before
    assert items[0]["video"] and items[0]["seconds"] in (7, 8)


def test_month_items_default_ratio():
    items = household.month_items(FakeImmich([row(ID1, ratio=None)]), BUCKET, ME)
    assert items[0]["ratio"] == household.DEFAULT_RATIO


def test_valid_ids_and_buckets():
    assert household.valid_asset_id(ID1) and not household.valid_asset_id("../etc/passwd")
    assert household.valid_bucket(BUCKET) and household.valid_bucket("2026-10-01") and not household.valid_bucket("x")


# ---- routes ----

def make_client(fake):
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "..", "app", "webui", "templates"),
                static_folder=os.path.join(os.path.dirname(__file__), "..", "app", "webui", "static"))
    register_household_routes(app, fake)
    return app.test_client()


def test_page_renders():
    r = make_client(FakeImmich()).get("/photos")
    assert r.status_code == 200 and b"photos.js" in r.data


def test_months_route_and_error():
    assert make_client(FakeImmich()).get("/api/household/months").get_json()["months"][0]["month"] == "2026-10"
    r = make_client(FakeImmich(fail=True)).get("/api/household/months")
    assert r.status_code == 502 and "down" in r.get_json()["error"]


def test_month_route():
    c = make_client(FakeImmich([row(ID1), row(ID2, owner=ME), row(ID3, owner=BEN)]))
    items = c.get("/api/household/month", query_string={"bucket": BUCKET}).get_json()["items"]
    assert sorted(i["owner"] for i in items) == ["Anna", "Ben"]
    assert c.get("/api/household/month", query_string={"bucket": "nope"}).status_code == 400


def test_image_routes():
    c = make_client(FakeImmich())
    r = c.get(f"/api/household/{ID1}/thumb")
    assert r.status_code == 200 and r.data == b"IMG-thumbnail" and r.mimetype == "image/jpeg"
    assert "max-age" in r.headers["Cache-Control"]
    assert c.get(f"/api/household/{ID1}/preview").data == b"IMG-preview"
    assert c.get("/api/household/not-an-id/thumb").status_code == 404
    assert make_client(FakeImmich(fail=True)).get(f"/api/household/{ID1}/thumb").status_code == 502


def test_video_route_streams_with_range():
    fake = FakeImmich()
    r = make_client(fake).get(f"/api/household/{ID1}/video", headers={"Range": "bytes=0-2"})
    assert r.status_code == 206 and r.data == b"abc" and fake.range == "bytes=0-2"
    assert r.headers["Content-Range"] == "bytes 0-2/9" and "X-Secret" not in r.headers
    assert make_client(fake).get("/api/household/zz/video").status_code == 404


def test_asset_info_route():
    r = make_client(FakeImmich()).get(f"/api/household/{ID1}").get_json()
    assert r["file_name"] == "IMG_1.jpg" and r["video"] is False and r["meta"]["owner_name"] == "Anna"


# ---- navigation ----

def test_photos_link_is_added_to_every_sidebar():
    root = os.path.join(os.path.dirname(__file__), "..", "app", "webui")
    js = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert 'a.href = "/photos"' in js and "nav .links" in js          # added to any page with the links
    for name in ("index.html", "rules.html", "photos.html"):
        html = open(os.path.join(root, "templates", name), encoding="utf-8").read()
        assert 'class="links"' in html and "app.js" in html            # so each of them gets it
    assert 'href="/photos"' in open(os.path.join(root, "templates", "photos.html"), encoding="utf-8").read()
