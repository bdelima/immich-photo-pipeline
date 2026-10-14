import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import chat, watermark
from test_chat import FakeRecipe, build, add_photo, run
from app.recipe_runner import CommentIntent, RecipeResult
from app.library import LibraryStore, Photo, Source


def photo(**kw):
    return Photo(id="p", status="ready", sources=[Source(asset_id="a", meta={
        "taken": "2024-03-02 14:05", "place": "Playa Hermosa, Costa Rica", "owner_name": "Bob"})], **kw)


def test_the_template_is_seeded_from_the_originals_details():
    assert watermark.template(photo()) == {"title": "Playa Hermosa, Costa Rica", "subtitle": "Bob · March 2024"}


def test_the_template_takes_what_each_original_knows_and_leaves_the_rest_blank():
    p = Photo(id="p", sources=[Source(asset_id="a"), Source(asset_id="b", meta={"owner_name": "Alex"})])
    assert watermark.template(p) == {"title": "", "subtitle": "Alex"}
    assert watermark.template(Photo(id="p")) == {"title": "", "subtitle": ""}


def test_junk_dates_are_left_out():
    assert watermark.month_year("soon") == "" and watermark.month_year("2024-13-01") == ""


def test_the_request_to_claude_carries_both_lines_and_says_it_is_for_this_photo():
    text = watermark.instruction("Costa Rica", "Bob · March 2024")
    assert '"Costa Rica"' in text and '"Bob · March 2024"' in text and "this photo only" in text
    assert "lower right corner" in text and "Arial" in text and "3D" in text


def test_a_single_line_and_quotes_are_handled():
    assert 'the title "He said \'hi\'" on top' in watermark.instruction('He said "hi"', "")
    assert 'rule): "Bob".' in watermark.instruction("", "Bob")


def test_a_third_line_is_allowed_but_never_required():
    text = watermark.instruction("Costa Rica", "Bob", "Wings at Dusk")
    assert 'the title "Costa Rica" on top and "Bob" underneath it and "Wings at Dusk" below that.' in text
    assert 'the title "A" on top and "C" below that.' in watermark.instruction("A", "", "C")
    assert 'rule): "C".' in watermark.instruction("", "", "C")
    assert "below that" not in watermark.instruction("A", "B")
    with pytest.raises(chat.ChatError):
        watermark.instruction("", "", "x" * 121)
    with pytest.raises(chat.ChatError):
        watermark.instruction("A", "", 5)


def test_an_empty_or_overlong_watermark_is_refused():
    with pytest.raises(chat.ChatError):
        watermark.instruction(" ", "")
    with pytest.raises(chat.ChatError):
        watermark.instruction("x" * 121, "")
    with pytest.raises(chat.ChatError):
        watermark.instruction(5, "")


def test_adding_draws_the_watermark_here_as_a_new_step_and_asks_claude_nothing(tmp_path):
    from PIL import Image
    store, revisions = _with_image(tmp_path, size=(400, 300))
    before = store.load().photos["p"]
    step = watermark.apply(store, revisions, "p", {"title": "Costa Rica", "subtitle": "Bob", "third": "Wings at Dusk",
                                                   "lines": [{"font": "Liberation Serif", "bold": True}, {"color": "#ff8c1a"}, {}]})
    p = store.load().photos["p"]
    assert step == 1 and p.current == 1 and len(p.revisions) == 2 and p.chat == []
    rev = p.revisions[1]
    assert rev.instruction == 'Watermark: "Costa Rica" / "Bob" / "Wings at Dusk"' and rev.origin == "processed"
    assert rev.watermark["title"] == "Costa Rica" and rev.watermark["lines"][0] == {"font": "Liberation Serif", "color": "auto", "bold": True, "italic": False}
    assert rev.watermark["lines"][1]["color"] == "#ff8c1a" and p.last_watermark == rev.watermark
    with Image.open(revisions.path(rev.file)) as im:
        assert im.size == (400, 300)
    assert rev.sha256 != before.revisions[0].sha256                       # the image changed
    assert not any(f.startswith(".wm-") for f in os.listdir(revisions.root))    # no scratch left behind


def test_a_photo_that_cannot_be_changed_is_refused(tmp_path):
    store, revisions = _with_image(tmp_path, media_type="video")
    with pytest.raises(chat.ChatError):
        watermark.apply(store, revisions, "p", {"title": "Costa Rica"})
    with pytest.raises(chat.ChatError) as e:
        watermark.apply(store, revisions, "nope", {"title": "x"})
    assert e.value.status == 404
    store, revisions = _with_image(tmp_path / "busy")
    store.update_photo("p", lambda p: setattr(p, "status", "processing"))
    with pytest.raises(chat.ChatError):
        watermark.apply(store, revisions, "p", {"title": "x"})


# ---- identifying what is in the picture ---------------------------------------------------


class FakeIdentifier:
    def __init__(self, answer="Scarlet macaw", error=None):
        self.answer, self.error, self.calls = answer, error, []

    def identify(self, image_path, hint=None, style="subject", avoid=None):
        self.calls.append((image_path, hint))
        self.styles = getattr(self, "styles", []) + [style]
        self.avoids = getattr(self, "avoids", []) + [avoid]
        if self.error:
            raise RuntimeError(self.error)
        return self.answer if isinstance(self.answer, list) else [self.answer]


def _with_image(tmp_path, size=(8, 8), **kw):
    from PIL import Image
    from app.library import Revision
    from app.revisions import RevisionStore
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "rev"))
    img = tmp_path / "i.jpg"
    Image.new("RGB", size, (30, 60, 30)).save(img)
    rel, sha = revisions.save_revision("p", 0, str(img))
    p = photo(**kw)
    p.revisions.append(Revision(n=0, parent=None, file=rel, sha256=sha))
    p.current = 0
    store.update(lambda lib: lib.photos.update(p=p))
    return store, revisions


def test_identify_names_the_current_image_and_cleans_the_answer(tmp_path):
    store, revisions = _with_image(tmp_path)
    recipe = FakeIdentifier('  "Scarlet\n macaw" <b> ')
    assert watermark.identify(store, revisions, recipe, "p", "  the bird ") == ["'Scarlet macaw' b"]
    path, hint = recipe.calls[0]
    assert path == revisions.path(store.load().photos["p"].revisions[0].file) and hint == "the bird"
    assert watermark.identify(store, revisions, recipe, "p") and recipe.calls[1][1] is None
    assert store.load().photos["p"].chat == []            # nothing is queued or changed


def test_identify_can_be_asked_for_a_poetic_title_instead_of_a_name(tmp_path):
    store, revisions = _with_image(tmp_path)
    recipe = FakeIdentifier(["Wings at Dusk", " The Quiet Harbor ", "Wings at Dusk", ""])
    assert watermark.identify(store, revisions, recipe, "p", "the light", "title") == ["Wings at Dusk", "The Quiet Harbor"]
    assert watermark.identify(store, revisions, recipe, "p") and recipe.styles == ["title", "subject"]
    with pytest.raises(chat.ChatError) as e:
        watermark.identify(store, revisions, recipe, "p", None, "haiku")
    assert e.value.status == 400 and len(recipe.calls) == 2


def test_a_retry_passes_the_earlier_answers_so_it_can_differ(tmp_path):
    store, revisions = _with_image(tmp_path)
    recipe = FakeIdentifier()
    watermark.identify(store, revisions, recipe, "p")
    watermark.identify(store, revisions, recipe, "p", None, "subject", [' Old "one" ', "", "Two"])
    watermark.identify(store, revisions, recipe, "p", None, "subject", [str(i) for i in range(30)])
    assert recipe.avoids[0] is None and recipe.avoids[1] == ["Scarlet macaw", "Old 'one'", "Two"]   # what it said before, then the page's
    assert len(recipe.avoids[2]) == watermark.MAX_AVOID and recipe.avoids[2][-1] == "29"
    for bad in ("Two", [1], {"a": 1}):
        with pytest.raises(chat.ChatError) as e:
            watermark.identify(store, revisions, recipe, "p", None, "subject", bad)
        assert e.value.status == 400


def test_the_pieces_the_builder_can_place_come_from_the_originals_details():
    got = watermark.parts(photo())
    assert (got["place"], got["owner"], got["date"]) == ("Playa Hermosa, Costa Rica", "Bob", "March 2024")
    assert watermark.parts(Photo(id="p")) == {"place": "", "owner": "", "date": "", "coordinates": None}
    located = Photo(id="q", sources=[Source(asset_id="a", meta={"latitude": 9.5, "longitude": -84.6})])
    assert watermark.parts(located)["coordinates"] == [9.5, -84.6]


def test_identify_is_refused_when_there_is_nothing_to_look_at(tmp_path):
    store, revisions = _with_image(tmp_path)
    ok = FakeIdentifier()
    for args, status in ((("nope",), 404), (("p", 5), 400), (("p", "x" * 81), 400)):
        with pytest.raises(chat.ChatError) as e:
            watermark.identify(store, revisions, ok, *args)
        assert e.value.status == status
    with pytest.raises(chat.ChatError) as e:
        watermark.identify(store, revisions, None, "p")
    assert e.value.status == 503
    store.update_photo("p", lambda p: setattr(p, "trashed", True))
    with pytest.raises(chat.ChatError):
        watermark.identify(store, revisions, ok, "p")
    assert ok.calls == []
    (tmp_path / "v").mkdir()
    store2, revisions2 = _with_image(tmp_path / "v", media_type="video")
    with pytest.raises(chat.ChatError):
        watermark.identify(store2, revisions2, ok, "p")


def test_a_failed_identification_is_a_502_with_the_reason(tmp_path):
    store, revisions = _with_image(tmp_path)
    with pytest.raises(chat.ChatError) as e:
        watermark.identify(store, revisions, FakeIdentifier(error="Claude took too long to answer"), "p")
    assert e.value.status == 502 and "too long" in str(e.value)


def test_the_identify_route(tmp_path):
    from app.rules import RulesStore
    from app.thumbs import ThumbCache
    from app.webui.server import create_app
    store, revisions = _with_image(tmp_path)
    recipe = FakeIdentifier()
    client = create_app(None, None, store, None, rules=RulesStore(str(tmp_path / "r.json")), revisions=revisions,
                        thumbs=ThumbCache(str(tmp_path / "th")), recipe=recipe).test_client()
    assert client.post("/api/photos/p/identify", json={"hint": "the bird"}).get_json() == {"names": ["Scarlet macaw"]}
    assert recipe.calls[0][1] == "the bird"
    assert client.post("/api/photos/p/identify").status_code == 200
    assert client.post("/api/photos/p/identify", json={"style": "title"}).status_code == 200
    client.post("/api/photos/p/identify", json={"avoid": ["Old"]})
    assert recipe.avoids[-1] == ["Scarlet macaw", "Old"]
    assert client.post("/api/photos/p/identify", json={"avoid": "Old"}).status_code == 400
    assert client.post("/api/photos/p/identify", json={"style": "limerick"}).status_code == 400
    assert client.post("/api/photos/nope/identify", json={}).status_code == 404
    bare = create_app(None, None, store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / "th"))).test_client()
    assert bare.post("/api/photos/p/identify", json={}).status_code == 503


# ---- starting from the last watermark -----------------------------------------------------


def lines_of(pieces):
    """The three lines the pieces make, the way the form's preview does."""
    seps = {"t": " – ", "u": " · ", "v": " · "}
    return [seps[k].join(x["text"] for x in sorted((x for x in pieces if x[k] and x["text"]), key=lambda x: x[k])) for k in ("t", "u", "v")]


def test_the_form_starts_from_the_template_until_a_watermark_has_been_asked_for():
    got = watermark.starting_point(photo())
    assert (got["title"], got["subtitle"], got["third"], got["remembered"]) == ("Playa Hermosa, Costa Rica", "Bob · March 2024", "", False)
    # five fields: title, subject, location, author and date; the first two are empty, the rest come from Immich
    assert [(x["key"], x["text"]) for x in got["pieces"]] == [
        ("title", ""), ("subject", ""), ("location", "Playa Hermosa, Costa Rica"), ("author", "Bob"), ("date", "March 2024")]
    assert lines_of(got["pieces"]) == ["Playa Hermosa, Costa Rica", "Bob · March 2024", ""]
    assert got["lines"] == [{"font": "Liberation Sans", "color": "auto", "bold": False, "italic": False}] * 3


def test_asking_for_a_watermark_remembers_its_lines_pieces_and_ticks_for_next_time(tmp_path):
    store, revisions = _with_image(tmp_path, size=(200, 150))
    pieces = [
        {"key": "title", "label": "Title", "text": ' Wings "at" Dusk ', "t": True, "u": False, "v": False},
        {"key": "location", "label": "Location", "text": "Playa Hermosa", "t": False, "u": True, "v": False},
        {"key": "author", "label": "Author", "text": "Bob", "t": False, "u": False, "v": True},
        {"key": "date", "label": "Date", "text": "March 2024", "t": False, "u": False, "v": True},
        {"key": "", "label": "Title idea", "text": "The Quiet Harbor", "t": False, "u": False, "v": False},
    ]
    watermark.apply(store, revisions, "p", {"title": "Wings 'at' Dusk", "subtitle": "Playa Hermosa", "third": "Bob · March 2024", "pieces": pieces,
                                             "lines": [{"font": "Liberation Mono", "italic": True}]})
    last = store.load().photos["p"].last_watermark
    assert (last["title"], last["subtitle"], last["third"]) == ("Wings 'at' Dusk", "Playa Hermosa", "Bob · March 2024")
    got = watermark.starting_point(store.load().photos["p"])
    assert got["remembered"] is True
    assert [(x["key"], x["label"], x["text"], x["t"], x["u"], x["v"]) for x in got["pieces"]] == [
        ("title", "Title", "Wings 'at' Dusk", True, False, False),
        ("subject", "Subject", "", False, False, False),
        ("location", "Location", "Playa Hermosa", False, True, False),
        ("author", "Author", "Bob", False, False, True),
        ("date", "Date", "March 2024", False, False, True),
        ("", "Title idea", "The Quiet Harbor", False, False, False)]
    assert lines_of(got["pieces"]) == ["Wings 'at' Dusk", "Playa Hermosa", "Bob · March 2024"]
    assert got["lines"][0] == {"font": "Liberation Mono", "color": "auto", "bold": False, "italic": True}
    watermark.apply(store, revisions, "p", {"title": "Second try", "subtitle": "x"})
    assert store.load().photos["p"].last_watermark["title"] == "Second try"


def test_a_watermark_remembered_by_an_older_version_keeps_its_lines_as_pieces():
    p = photo(last_watermark={"title": "Wings at Dusk", "subtitle": "Bob", "third": ""})
    got = watermark.starting_point(p)
    assert got["remembered"] is True and lines_of(got["pieces"]) == ["Wings at Dusk", "Bob", ""]
    assert [x["key"] for x in got["pieces"]][:5] == ["title", "subject", "location", "author", "date"]
    assert not any(x["t"] or x["u"] or x["v"] for x in got["pieces"] if x["key"])      # the five start unticked


def test_pieces_from_the_page_are_cleaned_and_bounded():
    got = watermark.clean_pieces([
        {"key": "title", "label": "x", "text": "a  <b>\"q\"</b>", "t": 1},
        {"key": "title", "label": "dup", "text": "again"},                 # a fixed one only once
        {"key": "bogus", "label": "Subject", "text": "Macaw", "u": True},   # an unknown key is an extra
        {"key": "", "label": "", "text": "no label, no key"},               # dropped
        {"text": 5, "label": 7},
    ] + [{"label": "Subject", "text": str(i)} for i in range(40)])
    assert got[0] == {"key": "title", "label": "x", "text": "a b'q'/b", "t": True, "u": False, "v": False}
    assert got[1]["key"] == "" and got[1]["text"] == "Macaw" and got[1]["u"] == 1
    assert len(got) <= watermark.MAX_PIECES
    assert watermark.clean_pieces(None) is None
    for bad in ("x", [1], {"a": 1}):
        with pytest.raises(chat.ChatError):
            watermark.clean_pieces(bad)


def test_a_refused_watermark_is_not_remembered(tmp_path):
    store, revisions = _with_image(tmp_path)
    with pytest.raises(chat.ChatError):
        watermark.apply(store, revisions, "p", {"title": "", "subtitle": "", "third": ""})
    with pytest.raises(chat.ChatError):
        watermark.apply(store, revisions, "p", {"title": "A", "pieces": "not a list"})
    p = store.load().photos["p"]
    assert p.last_watermark is None and len(p.revisions) == 1


def test_an_older_library_without_the_field_still_loads():
    from app.library import library_from_dict, library_to_dict, Library
    lib = Library(photos={"p": photo()})
    raw = library_to_dict(lib)
    del raw["photos"]["p"]["last_watermark"]
    assert library_from_dict(raw).photos["p"].last_watermark is None


def test_suggestions_are_remembered_on_the_photo_so_a_later_ask_differs(tmp_path):
    store, revisions = _with_image(tmp_path)
    recipe = FakeIdentifier(["One", "Two", "Three"])
    watermark.identify(store, revisions, recipe, "p", None, "title")
    assert store.load().photos["p"].suggestions == {"title": ["One", "Two", "Three"]}
    # asked again later (a fresh page sends no earlier answers): the saved ones are passed along
    recipe.answer = ["Four"]
    watermark.identify(store, revisions, recipe, "p", None, "title")
    assert recipe.avoids[-1] == ["One", "Two", "Three"]
    # the two kinds are kept apart, and only the last 15 are kept
    watermark.identify(store, revisions, FakeIdentifier("Macaw"), "p", None, "subject")
    saved = store.load().photos["p"].suggestions
    assert saved["subject"] == ["Macaw"] and saved["title"] == ["One", "Two", "Three", "Four"]
    many = FakeIdentifier([f"t{i}" for i in range(20)])
    watermark.identify(store, revisions, many, "p", None, "title")
    assert store.load().photos["p"].suggestions["title"] == [f"t{i}" for i in range(5, 20)]
    assert watermark.MAX_AVOID == 15


def test_a_library_without_suggestions_still_loads():
    from app.library import library_from_dict, library_to_dict, Library
    raw = library_to_dict(Library(photos={"p": photo()}))
    del raw["photos"]["p"]["suggestions"]
    assert library_from_dict(raw).photos["p"].suggestions == {}


# ---- remembered per step ---------------------------------------------------------------------


def _stepped():
    """Step 0 plain, step 1 watermarked 'One', step 2 a plain change, step 3 watermarked 'Two'."""
    from app.library import Revision
    p = photo()
    for n, wm in enumerate([None, "One", None, "Two"]):
        p.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=f"f{n}", instruction="x" if n else None,
                                    watermark={"title": wm, "subtitle": "", "third": ""} if wm else None))
    p.current = 3
    return p


def test_the_form_starts_from_the_watermark_of_the_step_showing():
    p = _stepped()
    assert watermark.starting_point(p)["title"] == "Two"
    p.current = 2                                       # a plain step after 'One': nearest earlier watermark
    assert watermark.starting_point(p)["title"] == "One"
    p.current = 0                                       # before any watermark: the template
    got = watermark.starting_point(p)
    assert got["remembered"] is False and got["title"] == "Playa Hermosa, Costa Rica"


def test_a_revert_shows_the_watermark_of_the_step_it_reverts_to():
    p = _stepped()
    p.add_revert(1, "f4")                               # now showing step 4, a copy of step 1
    assert watermark.starting_point(p)["title"] == "One"
    p.add_revert(0, "f5")                               # a copy of the plain first result
    assert watermark.starting_point(p)["remembered"] is False


def test_a_photo_watermarked_before_steps_kept_theirs_uses_the_last_one_asked_for():
    from app.library import Revision
    p = photo(last_watermark={"title": "Old", "subtitle": "", "third": ""})
    p.revisions.append(Revision(n=0, parent=None, file="f0")); p.current = 0
    assert watermark.starting_point(p)["title"] == "Old"


# ---- a watermark request is kept on the step it makes -----------------------------------------


def _wm(title):
    return {"title": title, "subtitle": "", "third": "", "pieces": [
        {"key": "title", "label": "Title", "text": title, "t": True, "u": False, "v": False}]}


def test_a_watermark_request_is_kept_on_the_step_it_makes(tmp_path):
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, steps=("first", "crop"))
    chat.enqueue(store, "a", "add a watermark", watermark=_wm("Dusk"))
    assert run(store, revisions, recipe, rules) is True
    photo = store.load().photos["a"]
    assert photo.current_revision().watermark == _wm("Dusk") and photo.revisions[1].watermark is None
    chat.enqueue(store, "a", "make it warmer")                       # an ordinary step carries none
    run(store, revisions, recipe, rules)
    assert store.load().photos["a"].current_revision().watermark is None


def test_a_watermark_asked_through_a_question_still_reaches_its_step(tmp_path):
    ask = RecipeResult(status="needs_clarification", question="Which corner?", session_id="s1")
    store, revisions, recipe, rules = build(tmp_path, FakeRecipe(results=[ask]))
    add_photo(tmp_path, store, revisions)
    chat.enqueue(store, "a", "add a watermark", watermark=_wm("Dusk"))
    assert run(store, revisions, recipe, rules) is False
    assert store.load().photos["a"].pending_watermark == _wm("Dusk")
    recipe.intent = CommentIntent("answer")
    chat.enqueue(store, "a", "the left one")
    assert run(store, revisions, recipe, rules) is True
    photo = store.load().photos["a"]
    assert photo.current_revision().watermark == _wm("Dusk") and photo.pending_watermark is None


def test_ticks_keep_the_order_they_were_made_in():
    got = watermark.clean_pieces([
        {"key": "author", "label": "Author", "text": "Bob", "u": 2},
        {"key": "date", "label": "Date", "text": "March 2024", "u": 1, "t": True},
        {"key": "", "label": "Subject", "text": "Macaw", "u": 99999, "v": -3},
        {"key": "location", "label": "Location", "text": "CR", "u": "yes", "v": 2.5}])
    assert [(x["t"], x["u"], x["v"]) for x in got] == [(0, 2, 0), (1, 1, 0), (0, watermark.MAX_TICK, 0), (0, 0, 0)]
    p = photo(last_watermark={"title": "", "subtitle": "x", "third": "", "pieces": [
        {"key": "author", "label": "Author", "text": "Bob", "t": 0, "u": 2, "v": 0},
        {"key": "date", "label": "Date", "text": "March 2024", "t": 0, "u": 1, "v": 0}]})
    assert lines_of(watermark.starting_point(p)["pieces"]) == ["", "March 2024 · Bob", ""]
    assert lines_of(watermark.starting_point(photo())["pieces"])[1] == "Bob · March 2024"


# ---- the style of the lines, the preview and the choices under the fields ----------------


def test_line_styles_are_cleaned_and_defaulted():
    got = watermark.clean_lines([{"font": " Creepster ", "color": "#FF8C1A", "bold": 1}, "junk"])
    assert got[0] == {"font": "Creepster", "color": "#ff8c1a", "bold": True, "italic": False}
    assert got[1] == got[2] == {"font": "Liberation Sans", "color": "auto", "bold": False, "italic": False}
    assert watermark.clean_lines([{"color": "purple"}])[0]["color"] == "auto"       # only hex, white, black or auto
    assert watermark.clean_lines([{"color": "White"}])[0]["color"] == "white"
    assert len(watermark.clean_lines(None)) == 3


def test_the_preview_is_a_png_of_the_corner_and_says_which_corner_and_colours(tmp_path):
    store, revisions = _with_image(tmp_path, size=(600, 400))
    png, info = watermark.preview(store, revisions, "p", {"title": "Hello", "lines": [{"color": "#ff0000"}]})
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and info == {"corner": "lower right", "colors": ["#ff0000"]}
    png, info = watermark.preview(store, revisions, "p", {})                       # nothing to write: the plain corner
    assert png[:4] == b"\x89PNG" and info["colors"] == []
    with pytest.raises(chat.ChatError):
        watermark.preview(store, revisions, "p", {"title": "Hi", "lines": [{"font": "No Such Font"}]})


def test_a_font_that_is_not_installed_is_refused_with_no_network(tmp_path, monkeypatch):
    import requests
    from app import wmrender
    store, revisions = _with_image(tmp_path, size=(100, 100))

    def down(*a, **k):
        raise requests.ConnectionError("no network")
    monkeypatch.setattr(wmrender.requests, "get", down)
    with pytest.raises(chat.ChatError) as e:
        watermark.apply(store, revisions, "p", {"title": "Hi", "lines": [{"font": "Creepster"}]})
    assert "Google Fonts" in str(e.value) and len(store.load().photos["p"].revisions) == 1


def test_a_google_font_is_fetched_once_and_kept(tmp_path, monkeypatch):
    from app import wmrender
    real = open("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf", "rb").read()
    calls = []

    class R:
        def __init__(self, status, text="", content=b""):
            self.status_code, self.text, self.content = status, text, content
        def raise_for_status(self):
            pass

    def fake_get(url, params=None, **k):
        calls.append(url)
        if url == wmrender.GOOGLE_CSS:
            if "Nope" in params["family"]:
                return R(400)
            return R(200, "@font-face { src: url(https://fonts.gstatic.com/s/x.ttf) format('truetype'); }")
        return R(200, content=real)
    monkeypatch.setattr(wmrender.requests, "get", fake_get)
    cache = str(tmp_path / "fonts")
    path, real_style = wmrender.font_file("Creepster", False, False, cache)
    assert os.path.isfile(path) and real_style and len(calls) == 2
    wmrender.font_file("Creepster", False, False, cache)
    assert len(calls) == 2                                                          # kept, not fetched again
    with pytest.raises(wmrender.FontError):
        wmrender.font_file("Nope Font", False, False, cache)


def test_the_auto_colour_is_white_on_dark_and_dark_on_light(tmp_path):
    from PIL import Image
    from app import wmrender
    dark, light = tmp_path / "d.jpg", tmp_path / "l.jpg"
    Image.new("RGB", (300, 200), (20, 30, 40)).save(dark)
    Image.new("RGB", (300, 200), (235, 235, 230)).save(light)
    out = tmp_path / "o.jpg"
    assert wmrender.render(str(dark), str(out), [{"text": "Hi"}], None)["colors"] == ["#ffffff"]
    assert wmrender.render(str(light), str(out), [{"text": "Hi"}], None)["colors"] == ["#1a1a1a"]
    assert wmrender.render(str(light), str(out), [{"text": "Hi", "color": "white"}], None)["colors"] == ["#ffffff"]


def test_the_text_moves_to_a_plainer_corner_when_the_lower_right_is_busy(tmp_path):
    from PIL import Image, ImageDraw
    import random
    from app import wmrender
    img = Image.new("RGB", (800, 600), (40, 60, 40))
    d = ImageDraw.Draw(img)
    random.seed(1)
    for _ in range(4000):                                   # noise in the lower right only
        x, y = random.randint(520, 799), random.randint(420, 599)
        d.point((x, y), fill=(random.randint(0, 255),) * 3)
    src = tmp_path / "busy.jpg"
    img.save(src, quality=95)
    assert wmrender.render(str(src), str(tmp_path / "o.jpg"), [{"text": "Hello there"}], None)["corner"] != "lower right"


def test_date_choices_are_written_the_way_people_write_them():
    assert watermark.date_choices("2026-05-12 12:32") == [
        "May 12, 2026", "12 May 2026", "Tuesday, May 12, 2026", "May 2026", "2026"]
    assert watermark.date_choices("") == [] and watermark.date_choices("soon") == []


class FakeImmich:
    def list_user_names(self):
        return ["Bob", "Karen", ""]

    def asset_people(self, asset_id):
        return ["Bob", "Sam"] if asset_id == "a" else []


def test_the_options_gather_suggestions_people_users_dates_and_fonts(tmp_path):
    store, revisions = _with_image(tmp_path, suggestions={"title": ["One", "Two"], "subject": ["Macaw", "Two"],
                                                          "location": ["Saratoga Gap"], "font": ["Creepster"], "color": ["#ff8c1a pumpkin"]})
    got = watermark.options(store, FakeImmich(), "p")
    assert got["title"] == ["Two", "One", "Macaw"]                 # newest first; a subject already a title is not repeated
    assert got["location"] == ["Saratoga Gap"] and got["people"] == ["Bob", "Sam"] and got["users"] == ["Bob", "Karen"]
    assert got["dates"][0] == "March 2, 2024" and got["fonts"] == ["Liberation Sans", "Liberation Serif", "Liberation Mono"]
    assert got["suggested_fonts"] == ["Creepster"] and got["suggested_colors"] == ["#ff8c1a pumpkin"]
    none = watermark.options(store, None, "p")
    assert none["people"] == [] and none["users"] == []
    with pytest.raises(chat.ChatError):
        watermark.options(store, None, "nope")


class FakeFontPicker:
    def __init__(self, answer=None, error=None):
        self.answer = answer or {"font": "Liberation Serif", "color": "#FF8C1A", "color_name": "pumpkin orange", "why": "Suits the mood."}
        self.error, self.calls = error, []

    def suggest_font(self, image_path, texts, hint=None):
        self.calls.append((image_path, texts, hint))
        if self.error:
            raise RuntimeError(self.error)
        return self.answer


def test_suggesting_a_font_asks_claude_with_the_text_and_remembers_the_answer(tmp_path):
    store, revisions = _with_image(tmp_path)
    recipe = FakeFontPicker()
    got = watermark.suggest_font(store, revisions, recipe, "p", ["Scarlet macaw", "", " Bob "], " spooky ")
    assert got == {"font": "Liberation Serif", "color": "#ff8c1a", "color_name": "pumpkin orange", "why": "Suits the mood."}
    assert recipe.calls[0][1:] == (["Scarlet macaw", "Bob"], "spooky")
    assert store.load().photos["p"].suggestions == {"font": ["Liberation Serif"], "color": ["#ff8c1a pumpkin orange"]}
    recipe.answer = {"font": "Liberation Mono", "color": "nonsense"}
    assert watermark.suggest_font(store, revisions, recipe, "p", [], None)["color"] == "auto"
    assert store.load().photos["p"].suggestions["font"] == ["Liberation Serif", "Liberation Mono"]
    assert store.load().photos["p"].chat == []


def test_a_font_claude_names_that_cannot_be_had_is_an_error_and_not_remembered(tmp_path, monkeypatch):
    import requests
    from app import wmrender
    store, revisions = _with_image(tmp_path)
    monkeypatch.setattr(wmrender.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(chat.ChatError) as e:
        watermark.suggest_font(store, revisions, FakeFontPicker({"font": "Creepster"}), "p", ["Hi"])
    assert e.value.status == 502 and "Creepster" in str(e.value)
    assert store.load().photos["p"].suggestions == {}
    with pytest.raises(chat.ChatError):
        watermark.suggest_font(store, revisions, FakeFontPicker(error="boom"), "p", ["Hi"])
    with pytest.raises(chat.ChatError):
        watermark.suggest_font(store, revisions, None, "p", ["Hi"])
    with pytest.raises(chat.ChatError):
        watermark.suggest_font(store, revisions, FakeFontPicker(), "p", "not a list")


def test_the_routes_draw_preview_offer_options_and_suggest_fonts(tmp_path):
    from app.webui.server import create_app
    from app.thumbs import ThumbCache
    store, revisions = _with_image(tmp_path, size=(300, 200))
    changed = []
    app = create_app(None, FakeImmich(), store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / "t")),
                     on_change=lambda: changed.append(1), recipe=FakeFontPicker())
    client = app.test_client()
    r = client.post("/api/photos/p/watermark/preview", json={"title": "Hi", "lines": [{"color": "black"}]})
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.headers["X-Watermark-Corner"] == "lower right" and r.headers["X-Watermark-Colors"] == "#1a1a1a"
    assert client.post("/api/photos/p/watermark/preview", json={"title": "Hi", "lines": [{"font": "Nope"}]}).status_code == 502
    o = client.get("/api/photos/p/watermark/options").get_json()
    assert o["users"] == ["Bob", "Karen"] and o["people"] == ["Bob", "Sam"] and o["dates"][0] == "March 2, 2024"
    assert client.get("/api/photos/nope/watermark/options").status_code == 404
    f = client.post("/api/photos/p/font", json={"texts": ["Hi"], "hint": "spooky"}).get_json()
    assert f["font"] == "Liberation Serif" and f["color"] == "#ff8c1a"
    w = client.post("/api/photos/p/watermark", json={"title": "Hi", "lines": [{"font": "Liberation Serif"}]})
    assert w.status_code == 200 and w.get_json() == {"step": 1} and changed == [1]
    assert store.load().photos["p"].current == 1
    assert client.post("/api/photos/p/watermark", json={"title": ""}).status_code == 400
