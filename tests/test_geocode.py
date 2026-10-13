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


def test_the_home_country_is_the_containers_own_zone_then_its_locale():
    if not locale._tables()["zones"]:
        return
    assert locale.container_country({"TZ": "America/Costa_Rica"}) == "Costa Rica"
    assert locale.container_country({"TZ": ":Europe/Paris"}) == "France"
    assert locale.container_country({"LANG": "en_US.UTF-8"}, zone="UTC") == "United States"
    assert locale.container_country({"LC_ALL": "fr_FR", "LANG": "en_US.UTF-8"}, zone="Etc/UTC") == "France"
    assert locale.container_country({"LANG": "C.UTF-8"}, zone="UTC") == ""
    assert locale.container_country({}, zone="") == ""
    assert locale.timezone_name({"TZ": "Asia/Tokyo"}) == "Asia/Tokyo"
    assert locale.country_for_region("ZZ") == "" and locale.country_for_region(None) == ""


def test_the_zone_comes_from_the_localtime_link_when_nothing_else_says(tmp_path, monkeypatch):
    monkeypatch.setattr(locale, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()), raising=False)
    assert locale.timezone_name({}, readlink=lambda p: "/usr/share/zoneinfo/Europe/Paris") == "Europe/Paris"
    assert locale.timezone_name({}, readlink=lambda p: "/etc/elsewhere") == ""
    assert locale.timezone_name({}, readlink=lambda p: (_ for _ in ()).throw(OSError())) == ""


def _client(tmp_path, recipe=None):
    from app.library import LibraryStore, Photo, Source
    from app.revisions import RevisionStore
    from app.rules import RulesStore
    from app.thumbs import ThumbCache
    from app.webui.server import create_app
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(
        a=Photo(id="a", sources=[Source(asset_id="x", meta={"latitude": 9.5, "longitude": -84.6})]), b=Photo(id="b"),
        c=Photo(id="c", sources=[Source(asset_id="y", meta={"place": "9.4300, -84.1600"})])))
    return create_app(None, None, store, None, rules=RulesStore(str(tmp_path / "r.json")),
                      revisions=RevisionStore(str(tmp_path / "rev")), thumbs=ThumbCache(str(tmp_path / "th")),
                      recipe=recipe).test_client()


class FakeLocator:
    def __init__(self, answer='  Quepos, "Puntarenas", Costa Rica ', error=None):
        self.answer, self.error, self.calls = answer, error, []

    def locate(self, lat, lon):
        self.calls.append((lat, lon))
        if self.error:
            raise RuntimeError(self.error)
        return self.answer


def test_the_locate_route_asks_the_chosen_source_and_nothing_else(tmp_path, monkeypatch):
    fresh()
    recipe = FakeLocator()
    client = _client(tmp_path, recipe)
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: Resp({"address": {"town": "Quepos", "country": "Costa Rica"}}))
    osm = client.post("/api/photos/a/locate", json={"source": "osm"})
    assert osm.get_json() == {"place": "Quepos, Costa Rica"} and recipe.calls == []
    claude = client.post("/api/photos/a/locate", json={"source": "claude"})
    assert claude.get_json() == {"place": "Quepos, 'Puntarenas', Costa Rica"} and recipe.calls == [(9.5, -84.6)]


def test_the_locate_route_refuses_what_it_cannot_do(tmp_path, monkeypatch):
    fresh()
    client = _client(tmp_path, FakeLocator(error="took too long"))
    post = lambda pid, body: client.post(f"/api/photos/{pid}/locate", json=body)
    assert post("a", {"source": "bing"}).status_code == 400 and post("a", {}).status_code == 400
    assert post("nope", {"source": "osm"}).status_code == 404
    assert post("b", {"source": "osm"}).status_code == 400            # no coordinates
    bad = post("a", {"source": "claude"})
    assert bad.status_code == 502 and "took too long" in bad.get_json()["error"]
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert post("a", {"source": "osm"}).status_code == 502
    (tmp_path / "bare").mkdir()
    assert _client(tmp_path / "bare").post("/api/photos/a/locate", json={"source": "claude"}).status_code == 503


def test_a_place_that_is_only_the_coordinates_counts_as_no_place_but_keeps_them(tmp_path):
    client = _client(tmp_path)
    got = client.get("/api/photos/c").get_json()["watermark"]
    assert got["parts"]["place"] == "" and got["parts"]["coordinates"] == [9.43, -84.16] and got["title"] == ""


def test_the_home_country_route_uses_the_container_not_the_browser(tmp_path, monkeypatch):
    client = _client(tmp_path)
    monkeypatch.setattr(locale, "container_country", lambda *a, **k: "Costa Rica")
    assert client.get("/api/home-country?tz=Europe/Paris").get_json() == {"country": "Costa Rica"}
