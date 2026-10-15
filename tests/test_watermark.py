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


def test_the_summary_names_the_lines_and_refuses_nothing_or_too_much():
    assert watermark.summary("Costa Rica", "Bob · March 2024") == 'Watermark: "Costa Rica" / "Bob · March 2024"'
    assert watermark.summary('He said "hi"', "", "C") == """Watermark: "He said 'hi'" / "C\""""
    for bad in ((" ", ""), ("x" * 121, ""), (5, ""), ("", "", "x" * 121), ("A", "", 5)):
        with pytest.raises(chat.ChatError):
            watermark.summary(*bad)


def test_the_request_to_claude_spells_the_watermark_out_and_replaces_any_earlier_one(tmp_path):
    store, revisions = _with_image(tmp_path)
    state = {"title": "Costa Rica", "subtitle": "Bob", "third": "", "lines": [{"font": "Liberation Serif", "bold": True}, {"color": "#ff8c1a"}],
             "corner": "auto", "drawn": {"corner": "upper left", "colors": ["#ffffff", "#ff8c1a"]}}
    text = watermark.request(state, revisions)
    assert text.startswith("Draw this watermark on the photo, exactly as specified")
    assert 'line 1 "Costa Rica" in Liberation Serif bold (file /usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf), colour #ffffff' in text
    assert 'line 2 "Bob" in Liberation Sans (file' in text and "colour #ff8c1a" in text
    assert "Placed in upper left" in text and "3.2%" in text                     # the corner the preview picked stands in for auto
    assert "replaces any earlier one" in text and "Change nothing else" in text
    assert "Placed in the lower right corner, or another" in watermark.request({"title": "x"}, revisions)   # nothing drawn yet: the rule
    assert "Placed in lower left" in watermark.request({"title": "x", "corner": "lower left", "drawn": {"corner": "upper left"}}, revisions)  # a chosen corner wins
    assert watermark.request({"title": "", "subtitle": ""}, revisions) == "" and watermark.request("junk", revisions) == ""


def test_adding_queues_the_watermark_for_claude_with_the_form(tmp_path):
    store, revisions = _with_image(tmp_path, size=(400, 300))
    mid = watermark.submit(store, revisions, "p", {"title": "Costa Rica", "subtitle": "Bob", "third": "Wings at Dusk",
                                                   "lines": [{"font": "Liberation Serif", "bold": True}, {"color": "#ff8c1a"}, {}]})
    p = store.load().photos["p"]
    assert mid == 1 and len(p.revisions) == 1 and p.current == 0                    # nothing drawn here
    m = p.chat[0]
    assert (m.role, m.text, m.state) == ("user", 'Watermark: "Costa Rica" / "Bob" / "Wings at Dusk"', "queued")
    assert m.watermark["title"] == "Costa Rica" and m.watermark["lines"][0] == {"font": "Liberation Serif", "color": "auto", "bold": True, "italic": False}
    assert "drawn" not in m.watermark


def test_what_the_preview_showed_settles_auto_only_when_it_was_drawn_on_the_current_image(tmp_path):
    """The preview's corner and colours were measured on whatever it was drawn on. Only when that
    was the photo's current image (the original is on file but can't be opened here) is it the
    picture Claude's result will look like; a preview on the original may have a pale mat under
    the text by the time Claude draws, and a plain panel measured nothing, so "auto" is left to
    Claude in those cases."""
    drawn = {"corner": "Upper-Left", "colors": ["#FFFFFF", "#ff8c1a", "junk"]}
    # no original on file: the preview was on a plain panel
    store, revisions = _with_image(tmp_path / "none", size=(400, 300))
    assert watermark.preview_source(store.load().photos["p"], revisions) == "none"
    watermark.submit(store, revisions, "p", {"title": "x", "subtitle": "y", "drawn": drawn})
    assert "drawn" not in store.load().photos["p"].chat[-1].watermark
    # the original on file and openable: the preview was on it, not on the matted result
    store, revisions = _with_original(tmp_path / "orig")
    assert watermark.preview_source(store.load().photos["p"], revisions) == "original"
    watermark.submit(store, revisions, "p", {"title": "x", "subtitle": "y", "drawn": drawn})
    assert "drawn" not in store.load().photos["p"].chat[-1].watermark
    assert "white on a dark patch or near-black on a light one" in watermark.request(store.load().photos["p"].chat[-1].watermark, revisions)
    # the original on file but not openable here: the preview was on the current image
    with open(revisions.path(store.load().photos["p"].sources[0].file), "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypheic not a picture Pillow can open")
    assert watermark.preview_source(store.load().photos["p"], revisions) == "current"
    watermark.submit(store, revisions, "p", {"title": "x", "subtitle": "y", "drawn": drawn})
    m = store.load().photos["p"].chat[-1]
    assert m.watermark["drawn"] == {"corner": "upper left", "colors": ["#ffffff", "#ff8c1a"]}
    assert "colour #ffffff" in watermark.request(m.watermark, revisions) and "Placed in upper left" in watermark.request(m.watermark, revisions)
    watermark.submit(store, revisions, "p", {"title": "x"})
    assert "drawn" not in store.load().photos["p"].chat[-1].watermark               # nothing from the preview: nothing kept


def test_a_photo_that_cannot_be_changed_is_refused(tmp_path):
    store, revisions = _with_image(tmp_path, media_type="video")
    with pytest.raises(chat.ChatError):
        watermark.submit(store, revisions, "p", {"title": "Costa Rica"})
    with pytest.raises(chat.ChatError) as e:
        watermark.submit(store, revisions, "nope", {"title": "x"})
    assert e.value.status == 404
    store, revisions = _with_image(tmp_path / "busy")
    store.update_photo("p", lambda p: setattr(p, "status", "processing"))
    with pytest.raises(chat.ChatError):
        watermark.submit(store, revisions, "p", {"title": "x"})
    assert store.load().photos["p"].chat == []


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


def _drawn(store, revisions, photo_id, data, recipe=None):
    """Submits the watermark and lets a fake Claude run finish it: the photo is treated as imported
    (edited from its first image) so no original is needed. Returns the fake recipe."""
    from test_chat import FakeRecipe
    recipe = recipe or FakeRecipe()
    store.update_photo(photo_id, lambda p: setattr(p, "imported", True))
    watermark.submit(store, revisions, photo_id, data)
    assert chat.run_job(store, revisions, recipe, None, photo_id) is True
    return recipe


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
    _drawn(store, revisions, "p", {"title": "Wings 'at' Dusk", "subtitle": "Playa Hermosa", "third": "Bob · March 2024", "pieces": pieces,
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
    _drawn(store, revisions, "p", {"title": "Second try", "subtitle": "x"})
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


def test_a_refused_watermark_is_not_remembered_or_queued(tmp_path):
    store, revisions = _with_image(tmp_path)
    with pytest.raises(chat.ChatError):
        watermark.submit(store, revisions, "p", {"title": "", "subtitle": "", "third": ""})
    with pytest.raises(chat.ChatError):
        watermark.submit(store, revisions, "p", {"title": "A", "pieces": "not a list"})
    p = store.load().photos["p"]
    assert p.last_watermark is None and len(p.revisions) == 1 and p.chat == []


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
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and info == {"corner": "lower right", "colors": ["#ff0000"], "source": "none"}
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
        watermark.submit(store, revisions, "p", {"title": "Hi", "lines": [{"font": "Creepster"}]})
    assert "Google Fonts" in str(e.value) and store.load().photos["p"].chat == []    # the font is fetched before Claude is asked


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
    def __init__(self, people=None, users=("Bob", "Karen", "")):
        self.people, self.users, self.asked = people if people is not None else {"a": ["Bob", "Sam"]}, list(users), []

    def list_user_names(self):
        return self.users

    def asset_people(self, asset_id):
        self.asked.append(asset_id)
        if self.people == "down":
            raise RuntimeError("down")
        return self.people.get(asset_id, [])


def test_the_options_gather_suggestions_people_users_dates_and_fonts(tmp_path):
    store, revisions = _with_image(tmp_path, suggestions={"title": ["One", "Two"], "subject": ["Macaw", "Two"],
                                                          "location": ["Saratoga Gap"], "font": ["Creepster"], "color": ["#ff8c1a pumpkin"]})
    got = watermark.options(store, FakeImmich(), "p")
    assert got["title"] == ["Two", "One", "Macaw"]                 # newest first; a subject already a title is not repeated
    assert got["location"] == ["Saratoga Gap"] and got["users"] == ["Bob", "Karen"]
    assert got["people"] == [] and got["subject"] == ""             # Immich's people are not offered (USE_IMMICH_PEOPLE)
    assert got["dates"][0] == "March 2, 2024" and got["fonts"] == ["Liberation Sans", "Liberation Serif", "Liberation Mono"]
    assert got["suggested_fonts"] == ["Creepster"] and got["suggested_colors"] == ["#ff8c1a pumpkin"]
    none = watermark.options(store, None, "p")
    assert none["people"] == [] and none["users"] == [] and none["subject"] == ""
    with pytest.raises(chat.ChatError):
        watermark.options(store, None, "nope")


def test_the_people_are_asked_for_with_every_accounts_key_since_names_are_per_account(tmp_path):
    store, _ = _with_image(tmp_path)
    p = Photo(id="c", sources=[Source(asset_id="a"), Source(asset_id="b"), Source(asset_id="")])
    # the pipeline's key sees no names on either original; the first extra key names a's, the second b's
    mine, karen, sam = FakeImmich({}), FakeImmich({"a": ["Karen", "Bob"]}), FakeImmich({"b": ["Sam", "Bob"]})
    assert watermark.people_in(p, (mine, karen, sam)) == ["Karen", "Bob", "Sam"]
    assert mine.asked == ["a", "b"] and karen.asked == ["a", "b"] and sam.asked == ["b"]   # the first that names anyone is taken
    assert watermark.people_in(p, (FakeImmich("down"), karen)) == ["Karen", "Bob"]        # a failing account is skipped
    assert watermark.people_in(p, ()) == []
    # the lookup is kept but switched off: the options ask no account for people
    asked = FakeImmich({"a": ["Sam"]})
    got = watermark.options(store, FakeImmich({}), "p", extra=(asked,))
    assert got["subject"] == "" and got["people"] == [] and asked.asked == []
    assert watermark.USE_IMMICH_PEOPLE is False


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
    changed, woken = [], []
    app = create_app(None, FakeImmich(), store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / "t")),
                     on_change=lambda: changed.append(1), on_job=lambda: woken.append(1), recipe=FakeFontPicker())
    client = app.test_client()
    r = client.post("/api/photos/p/watermark/preview", json={"title": "Hi", "lines": [{"color": "black"}]})
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.headers["X-Watermark-Corner"] == "lower right" and r.headers["X-Watermark-Colors"] == "#1a1a1a"
    assert r.headers["X-Watermark-Source"] == "none"                                 # no original on file: a plain panel
    assert client.post("/api/photos/p/watermark/whole", json={"title": "Hi"}).status_code == 404
    assert client.post("/api/photos/p/watermark/preview", json={"title": "Hi", "lines": [{"font": "Nope"}]}).status_code == 502
    o = client.get("/api/photos/p/watermark/options").get_json()
    assert o["users"] == ["Bob", "Karen"] and o["people"] == [] and o["dates"][0] == "March 2, 2024"
    assert client.get("/api/photos/nope/watermark/options").status_code == 404
    f = client.post("/api/photos/p/font", json={"texts": ["Hi"], "hint": "spooky"}).get_json()
    assert f["font"] == "Liberation Serif" and f["color"] == "#ff8c1a"
    w = client.post("/api/photos/p/watermark", json={"title": "Hi", "lines": [{"font": "Liberation Serif"}]})
    assert w.status_code == 200 and w.get_json() == {"id": 1} and woken == [1] and changed == []   # queued for Claude, the worker woken
    assert store.load().photos["p"].current == 0 and store.load().photos["p"].chat[0].text == 'Watermark: "Hi"'
    assert client.post("/api/photos/p/watermark", json={"title": ""}).status_code == 400
    # with the original on file: the preview says so, and the whole original can be had
    store2, revisions2 = _with_original(tmp_path / "o", size=(300, 200))
    app2 = create_app(None, FakeImmich({}), store2, None, revisions=revisions2, thumbs=ThumbCache(str(tmp_path / "t2")),
                      extra_immich=(FakeImmich({"a": ["Sam"]}),))
    c2 = app2.test_client()
    r = c2.post("/api/photos/p/watermark/preview", json={"title": "Hi"})
    assert r.status_code == 200 and r.headers["X-Watermark-Source"] == "original"
    r = c2.post("/api/photos/p/watermark/whole", json={"title": "Hi", "corner": "upper left"})
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.headers["X-Watermark-Corner"] == "upper left"
    assert c2.get("/api/photos/p/watermark/options").get_json()["subject"] == ""         # Immich's people are not offered


# ---- the chat path and the builder speak the same language -------------------------------


def test_the_corner_can_be_chosen_and_is_kept_with_the_step(tmp_path):
    from PIL import Image
    from app import wmrender
    store, revisions = _with_image(tmp_path, size=(400, 300))
    assert watermark.clean_corner(" Upper-Left ") == "upper left" and watermark.clean_corner("middle") == "auto" and watermark.clean_corner(None) == "auto"
    assert watermark.clean_note(" a red <glow> ") == "a red glow" and watermark.clean_note(5) == ""
    png, info = watermark.preview(store, revisions, "p", {"title": "Hi", "corner": "upper left"})
    assert info["corner"] == "upper left"
    recipe = _drawn(store, revisions, "p", {"title": "Hi", "corner": "lower left", "style_note": "a glow"})
    assert "Placed in lower left" in recipe.edits[-1][1]                        # Claude is told the chosen corner
    rev = store.load().photos["p"].current_revision()
    assert rev.watermark["corner"] == "lower left" and rev.watermark["style_note"] == "a glow"
    got = watermark.starting_point(store.load().photos["p"])
    assert got["corner"] == "lower left" and got["style_note"] == "a glow"
    assert watermark.starting_point(photo())["corner"] == "auto"
    src = tmp_path / "s.jpg"
    Image.new("RGB", (300, 200), (20, 20, 20)).save(src)
    assert wmrender.render(str(src), str(tmp_path / "o.jpg"), [{"text": "Hi"}], None, corner="upper right")["corner"] == "upper right"


def test_a_watermark_step_is_described_to_claude_in_full(tmp_path):
    store, revisions = _with_image(tmp_path)
    state = {"title": "Scarlet macaw", "subtitle": "Bob", "third": "", "lines": [
        {"font": "Liberation Serif", "color": "#ff8c1a", "bold": True, "italic": False}, {"font": "Liberation Sans", "color": "auto"}],
        "corner": "upper left", "style_note": "a faint glow"}
    text = watermark.describe(state, revisions)
    assert text.startswith('Watermark drawn by the pipeline: line 1 "Scarlet macaw" in Liberation Serif bold (file /usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf), colour #ff8c1a; line 2 "Bob" in Liberation Sans (file ')
    assert "white on a dark patch" in text and "Placed in upper left" in text and "3.2%" in text and "a faint glow" in text
    assert "Reproduce it exactly unless the new request is about the watermark" in text
    assert "lower right corner, or another" in watermark.describe({"title": "x", "lines": []}, revisions)
    assert watermark.describe({"title": "", "subtitle": ""}, revisions) == "" and watermark.describe("junk", revisions) == ""


def test_what_claude_reports_drawing_becomes_the_builders_state(tmp_path):
    from app import wmrender
    store, revisions = _with_image(tmp_path)
    previous = {"title": "Scarlet macaw", "subtitle": "Bob · May 2026", "third": "", "lines": [{"font": "Liberation Serif"}], "pieces": [
        {"key": "title", "label": "Title", "text": "Scarlet macaw", "t": 1, "u": 0, "v": 0},
        {"key": "subject", "label": "Subject", "text": "", "t": 0, "u": 0, "v": 0},
        {"key": "location", "label": "Location", "text": "Saratoga", "t": 0, "u": 0, "v": 0},
        {"key": "author", "label": "Author", "text": "Bob", "t": 0, "u": 1, "v": 0},
        {"key": "date", "label": "Date", "text": "May 2026", "t": 0, "u": 2, "v": 0}]}
    # the same text, moved and recoloured: the pieces are kept, the style follows the report
    report = {"lines": [{"text": "Scarlet macaw", "font": "Liberation Mono", "color": "#FF0000", "bold": True}, {"text": "Bob · May 2026"}],
              "corner": "Upper Left", "style_note": "a red glow"}
    got = watermark.from_report(report, previous, revisions)
    assert (got["title"], got["subtitle"], got["third"]) == ("Scarlet macaw", "Bob · May 2026", "")
    assert got["lines"][0] == {"font": "Liberation Mono", "color": "#ff0000", "bold": True, "italic": False}
    assert got["corner"] == "upper left" and got["style_note"] == "a red glow"
    assert [(x["key"], x["t"], x["u"]) for x in got["pieces"]][:5] == [("title", 1, 0), ("subject", 0, 0), ("location", 0, 0), ("author", 0, 1), ("date", 0, 2)]
    # a rewritten line: its old pieces come off it and the new text goes to the first piece then free
    got = watermark.from_report({"lines": [{"text": "Wings at Dusk"}, {"text": "Bob · May 2026"}]}, previous, revisions)
    assert got["title"] == "Wings at Dusk"
    title = got["pieces"][0]
    assert (title["key"], title["text"], title["t"]) == ("title", "Wings at Dusk", 1)
    assert [x["u"] for x in got["pieces"]] == [0, 0, 0, 1, 2]                    # the unchanged line keeps its pieces
    # with nothing before it, and a font file Claude fetched, which is kept so the family draws from now on
    src = tmp_path / "fetched.ttf"
    src.write_bytes(open("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", "rb").read())
    got = watermark.from_report({"lines": [{"text": "Boo", "font": "Creepster"}], "font_files": {"Creepster": str(src)}}, None, revisions)
    assert got["lines"][0]["font"] == "Creepster" and os.path.isfile(os.path.join(watermark.font_dir(revisions), "creepster-0-400.ttf"))
    assert got["pieces"][0] == {"key": "title", "label": "Title", "text": "Boo", "t": 1, "u": 0, "v": 0}
    assert wmrender.font_file("Creepster", False, False, watermark.font_dir(revisions))[0].endswith("creepster-0-400.ttf")
    # a font named but not had falls back to the default; junk reports are None
    import requests
    from unittest.mock import patch
    with patch.object(wmrender.requests, "get", side_effect=requests.ConnectionError("down")):
        assert watermark.from_report({"lines": [{"text": "x", "font": "Nope Font"}]}, None, revisions)["lines"][0]["font"] == "Liberation Sans"
    assert watermark.from_report({"lines": []}, None, revisions) is None and watermark.from_report("x", None, revisions) is None
    assert watermark.from_report({"lines": [{"text": "  "}]}, None, revisions) is None


def test_a_chat_edit_replays_the_watermark_in_full_and_keeps_what_claude_reports(tmp_path):
    from PIL import Image
    from test_chat import texts as chat_texts
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, steps=("first",))
    Image.new("RGB", (300, 200), (30, 30, 30)).save(revisions.path(store.load().photos["a"].current_revision().file))
    # the builder's Add: a chat request Claude draws from, the form's state kept on the step
    watermark.submit(store, revisions, "a", {"title": "Scarlet macaw", "lines": [{"font": "Liberation Serif", "color": "#ff8c1a"}], "corner": "upper left"})
    assert run(store, revisions, recipe, rules) is True
    note = recipe.runs[-1][1]
    assert "Draw this watermark on the photo, exactly as specified" in note and "Placed in upper left" in note
    assert "replaces any earlier one" in note and f"keep any font file you fetch under {watermark.font_dir(revisions)}" in note
    rev = store.load().photos["a"].current_revision()
    assert rev.instruction == 'Watermark: "Scarlet macaw"' and rev.watermark["corner"] == "upper left" and rev.watermark["lines"][0]["color"] == "#ff8c1a"
    recipe.results = [RecipeResult(status="done", watermark={"lines": [{"text": "Scarlet macaw", "font": "Liberation Serif", "color": "#00ff00"}], "corner": "lower left", "style_note": "glowing"})]
    chat.enqueue(store, "a", "make the watermark glow green")
    assert run(store, revisions, recipe, rules) is True
    note = recipe.runs[-1][1]
    assert 'Watermark drawn by the pipeline: line 1 "Scarlet macaw" in Liberation Serif (file' in note and "Placed in upper left" in note
    assert f"keep any font file you fetch under {watermark.font_dir(revisions)}" in note
    p = store.load().photos["a"]
    rev = p.current_revision()
    assert rev.instruction == "make the watermark glow green"
    assert rev.watermark["lines"][0]["color"] == "#00ff00" and rev.watermark["corner"] == "lower left" and rev.watermark["style_note"] == "glowing"
    got = watermark.starting_point(p)
    assert got["title"] == "Scarlet macaw" and got["corner"] == "lower left" and got["style_note"] == "glowing"
    # a later plain edit is told about the step Claude made too, with the note
    recipe.results = [RecipeResult(status="done")]
    chat.enqueue(store, "a", "warmer")
    run(store, revisions, recipe, rules)
    note = recipe.runs[-1][1]
    assert "Placed in lower left" in note and "Its look beyond that: glowing" in note
    assert store.load().photos["a"].current_revision().watermark is None       # Claude reported nothing this time


def test_adding_a_watermark_again_replays_the_history_without_the_earlier_watermarks(tmp_path):
    """The old watermark is not drawn again: every other step is replayed and the new watermark
    takes its place, whether the old one is the latest step or buried under later ones."""
    store, revisions, recipe, rules = build(tmp_path)
    add_photo(tmp_path, store, revisions, steps=("first", "crop tighter"))
    chat.enqueue(store, "a", "add a watermark saying Old in the corner")      # typed in chat, before steps were marked
    run(store, revisions, recipe, rules)
    watermark.submit(store, revisions, "a", {"title": "Middle"})              # a builder watermark, marked
    run(store, revisions, recipe, rules)
    chat.enqueue(store, "a", "warmer")                                        # a plain edit on top
    run(store, revisions, recipe, rules)
    watermark.submit(store, revisions, "a", {"title": "New"})
    assert run(store, revisions, recipe, rules) is True
    note = recipe.runs[-1][1]
    assert "1) crop tighter 2) add a watermark saying Old in the corner (superseded by the watermark below; do not draw it) 3) warmer" in note
    assert "Middle" not in note and 'line 1 "New"' in note                     # the marked step is left out entirely
    # a plain edit after that replays only the latest watermark in full: the earlier ones stay out
    chat.enqueue(store, "a", "brighter")
    run(store, revisions, recipe, rules)
    note = recipe.runs[-1][1]
    assert 'Watermark drawn by the pipeline: line 1 "New"' in note and "Middle" not in note
    assert "add a watermark saying Old in the corner (superseded by the watermark below; do not draw it)" in note
    assert note.index("superseded") < note.index('line 1 "New"') and "3) warmer 4) Watermark drawn" in note


def test_a_font_file_from_anywhere_but_google_fonts_is_refused(tmp_path, monkeypatch):
    from app import wmrender
    calls = []

    class R:
        status_code, text = 200, "@font-face { src: url(https://evil.example/x.ttf) format('truetype'); }"

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, **k):
        calls.append(url)
        return R()

    monkeypatch.setattr(wmrender.requests, "get", fake_get)
    with pytest.raises(wmrender.FontError):
        wmrender.font_file("Creepster", False, False, str(tmp_path / "fonts"))
    assert calls == [wmrender.GOOGLE_CSS]                       # the file itself was never fetched
    assert not os.path.exists(tmp_path / "fonts" / "creepster-0-400.ttf")


def test_the_photo_is_turned_upright_before_it_is_drawn_on_and_the_preview_uses_a_reduced_copy():
    import inspect
    from app import wmrender
    for fn in (wmrender.render, wmrender._reduced):
        assert "ImageOps.exif_transpose(im)" in inspect.getsource(fn)
    reduced = inspect.getsource(wmrender._reduced)
    assert "im.draft(" in reduced and "img.thumbnail((longest, longest))" in reduced
    assert "_reduced(src_path, PREVIEW_MAX)" in inspect.getsource(wmrender.preview)
    assert "_reduced(src_path, WHOLE_MAX)" in inspect.getsource(wmrender.whole)
    assert wmrender.PREVIEW_MAX >= 1000 and wmrender.WHOLE_MAX >= wmrender.PREVIEW_MAX


# ---- the preview is drawn on the original -------------------------------------------------


def _with_original(tmp_path, size=(600, 400), colour=(20, 20, 20), **kw):
    """A photo whose current step is a pale matted image and whose first original is on file, dark."""
    from PIL import Image
    store, revisions = _with_image(tmp_path, size=size, **kw)
    orig = tmp_path / "orig.jpg"
    Image.new("RGB", size, colour).save(orig)
    rel = revisions.save_source("p", 0, str(orig))
    Image.new("RGB", size, (235, 230, 220)).save(revisions.path(store.load().photos["p"].revisions[0].file))

    def keep(photo):
        photo.sources[0].file = rel
    store.update_photo("p", keep)
    return store, revisions


def test_the_preview_is_drawn_on_the_original_when_there_is_one_and_on_a_plain_panel_when_not(tmp_path):
    from PIL import Image
    import io
    store, revisions = _with_original(tmp_path)
    assert watermark.original_path(store.load().photos["p"], revisions)
    png, info = watermark.preview(store, revisions, "p", {"title": "Hello"})
    assert info["source"] == "original" and info["colors"] == ["#ffffff"]        # white: the original is dark, the step is pale
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (560, 180) and max(im.getpixel((5, 5))) < 60             # the original's dark pixels, not the pale step
    none, revisions2 = _with_image(tmp_path / "none", size=(600, 400))
    assert watermark.original_path(none.load().photos["p"], revisions2) is None
    png, info = watermark.preview(none, revisions2, "p", {"title": "Hello", "lines": [{"color": "#ff0000"}]})
    assert info["source"] == "none" and info["colors"] == ["#ff0000"]
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (560, 180) and im.getpixel((5, 5)) == wmrender_panel()


def wmrender_panel():
    from app import wmrender
    return wmrender.PLAIN_PANEL


def test_a_click_on_the_preview_gets_the_whole_original_with_the_watermark_on_it(tmp_path):
    from PIL import Image
    import io
    store, revisions = _with_original(tmp_path, size=(3000, 2000))
    jpeg, info = watermark.whole(store, revisions, "p", {"title": "Hello", "corner": "upper left"})
    assert jpeg[:3] == b"\xff\xd8\xff" and info["corner"] == "upper left"
    with Image.open(io.BytesIO(jpeg)) as im:
        assert im.size == (2000, 1333)                                           # sized for a screen, not the full original
    assert len(store.load().photos["p"].revisions) == 1                          # nothing written
    none, revisions2 = _with_image(tmp_path / "none")
    with pytest.raises(chat.ChatError) as e:
        watermark.whole(none, revisions2, "p", {"title": "Hello"})
    assert e.value.status == 404


def test_an_unreadable_picture_is_reported_as_an_image_error(tmp_path):
    from app import wmrender
    bad = tmp_path / "x.heic"
    bad.write_bytes(b"not a picture this server can read")
    with pytest.raises(wmrender.ImageError):
        wmrender._reduced(str(bad), 100)


def test_an_original_this_server_cannot_open_falls_back_to_the_current_image_and_says_so(tmp_path):
    from PIL import Image
    import io
    store, revisions = _with_original(tmp_path)
    rel = store.load().photos["p"].sources[0].file
    with open(revisions.path(rel), "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypheic this is not something Pillow can open")        # like a HEIC
    png, info = watermark.preview(store, revisions, "p", {"title": "Hello"})
    assert info["source"] == "current" and info["colors"] == ["#1a1a1a"]                   # dark text: the current step is pale
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (560, 180) and min(im.getpixel((5, 5))) > 200
    with pytest.raises(chat.ChatError) as e:
        watermark.whole(store, revisions, "p", {"title": "Hello"})
    assert e.value.status == 422
