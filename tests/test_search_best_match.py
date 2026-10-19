import os

from app import household

HERE = os.path.dirname(__file__)
OWN = "own"


def asset(i, day):
    return {"id": i, "ownerId": "anna", "type": "IMAGE", "localDateTime": day + "T10:00:00.000Z", "width": 100, "height": 100}


class FakeImmich:
    """Hands back the same three photos, best match first, whichever search is asked for."""

    def __init__(self):
        self.kinds = []

    def search_assets(self, kind, body):
        self.kinds.append(kind)
        return {"items": [asset("b1", "2020-01-01"), asset("b2", "2026-05-05"), asset("b3", "2023-03-03")], "next": None}


def ids(items):
    return [i["id"] for i in items]


def test_a_search_by_meaning_keeps_immichs_order_best_match_first():
    fake = FakeImmich()
    items, _ = household.search_items(fake, OWN, "boat pictures", "context", {"people": []})
    assert fake.kinds == ["smart"] and ids(items) == ["b1", "b2", "b3"]      # not re-sorted by date


def test_searches_that_are_not_by_meaning_stay_newest_first():
    fake = FakeImmich()
    items, _ = household.search_items(fake, OWN, "IMG", "file_name", {})
    assert ids(items) == ["b2", "b3", "b1"]
    items, _ = household.search_items(fake, OWN, "", "context", {"city": "Ojochal"})      # only filters: no text to match
    assert fake.kinds[-1] == "metadata" and ids(items) == ["b2", "b3", "b1"]


def test_the_page_shows_best_match_results_as_one_run_with_no_months():
    with open(os.path.join(HERE, "..", "app", "webui", "static", "photos.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert 'bestFirst = Q.mode === "context" && !!Q.text;' in js
    assert "if (!bestFirst) allResults.sort(" in js
    assert "flat: true" in js and "if (!s || s.flat) return;" in js
