import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from app import geocode, locale


class Resp:
    def __init__(self, body, bad=False):
        self.body, self.bad = body, bad

    def raise_for_status(self):
        if self.bad:
            raise requests.HTTPError("503")

    def json(self):
        return self.body


def fresh():
    geocode._cache.clear()
    geocode._last_request = 0.0


def test_the_address_becomes_city_state_country_without_repeats_or_gaps():
    assert geocode.place_from_address({"town": "Jaco", "state": "Puntarenas", "country": "Costa Rica"}) == "Jaco, Puntarenas, Costa Rica"
    assert geocode.place_from_address({"city": "Singapore", "state": "Singapore", "country": "Singapore"}) == "Singapore"
    assert geocode.place_from_address({"village": "Oia", "country": "Greece"}) == "Oia, Greece"
    assert geocode.place_from_address({"ocean": "Pacific"}) == ""


def test_reverse_asks_once_per_place_names_itself_and_waits_between_requests():
    fresh()
    calls, naps = [], []

    def get(url, **kw):
        calls.append(kw)
        return Resp({"address": {"city": "Tamarindo", "country": "Costa Rica"}})

    clock = iter([100.0, 100.0, 100.2, 100.2]).__next__
    assert geocode.reverse(10.299, -85.84, get, naps.append, clock) == "Tamarindo, Costa Rica"
    assert geocode.reverse(10.2991, -85.8399, get, naps.append, clock) == "Tamarindo, Costa Rica"   # cached
    assert len(calls) == 1 and "immich-photo-pipeline" in calls[0]["headers"]["User-Agent"]
    assert calls[0]["params"]["lat"] == 10.299 and calls[0]["timeout"] == geocode.TIMEOUT_SECONDS
    geocode.reverse(48.85, 2.35, get, naps.append, clock)
    assert len(calls) == 2 and naps and 0 < naps[0] <= 1.0


def test_reverse_gives_nothing_for_bad_input_or_a_failing_service_and_does_not_cache_failures():
    fresh()
    boom = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down"))
    assert geocode.reverse("x", 1, boom) == "" and geocode.reverse(91, 0, boom) == "" and geocode.reverse(True, 0, boom) == ""
    assert geocode.reverse(1.0, 1.0, boom, lambda s: None) == ""
    assert geocode.reverse(1.0, 1.0, lambda *a, **k: Resp({}, bad=True), lambda s: None) == ""
    assert geocode.reverse(1.0, 1.0, lambda *a, **k: Resp({"address": "?"}), lambda s: None) == ""
    ok = lambda *a, **k: Resp({"address": {"city": "Oslo", "country": "Norway"}})
    assert geocode.reverse(1.0, 1.0, ok, lambda s: None) == "Oslo, Norway"


def test_the_home_country_comes_from_the_time_zone_when_the_tables_exist():
    if not locale._tables()["zones"]:
        return
    assert locale.country_for_timezone("America/Costa_Rica") == "Costa Rica"
    assert locale.country_for_timezone("Europe/Paris") == "France"
    assert locale.country_for_timezone("Nowhere/Land") == ""
    assert locale.country_for_timezone("../../etc/passwd") == "" and locale.country_for_timezone(None) == ""


def test_the_place_and_home_country_routes(tmp_path, monkeypatch):
    from app.library import LibraryStore, Photo, Source
    from app.revisions import RevisionStore
    from app.rules import RulesStore
    from app.thumbs import ThumbCache
    from app.webui.server import create_app
    fresh()
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(
        a=Photo(id="a", sources=[Source(asset_id="x", meta={"latitude": 9.5, "longitude": -84.6})]), b=Photo(id="b")))
    client = create_app(None, None, store, None, rules=RulesStore(str(tmp_path / "r.json")),
                        revisions=RevisionStore(str(tmp_path / "rev")), thumbs=ThumbCache(str(tmp_path / "th"))).test_client()
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: Resp({"address": {"town": "Quepos", "country": "Costa Rica"}}))
    assert client.get("/api/photos/a/place").get_json() == {"place": "Quepos, Costa Rica"}
    assert client.get("/api/photos/b/place").get_json() == {"place": ""}
    assert client.get("/api/photos/nope/place").status_code == 404
    assert "country" in client.get("/api/home-country?tz=Europe/Paris").get_json()
    assert client.get("/api/home-country").get_json() == {"country": ""}
