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


def test_adding_queues_a_message_for_the_worker(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo()))
    watermark.add(store, "p", "Costa Rica", "Bob", "Wings at Dusk")
    msg = store.load().photos["p"].chat[0]
    assert msg.role == "user" and msg.state == "queued" and "Costa Rica" in msg.text and "Wings at Dusk" in msg.text


def test_a_photo_that_cannot_be_changed_is_refused(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo(media_type="video")))
    with pytest.raises(chat.ChatError):
        watermark.add(store, "p", "Costa Rica", "Bob")
    with pytest.raises(chat.ChatError) as e:
        watermark.add(store, "nope", "x", "y")
    assert e.value.status == 404


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


def _with_image(tmp_path, **kw):
    from PIL import Image
    from app.library import Revision
    from app.revisions import RevisionStore
    store = LibraryStore(str(tmp_path / "lib.json"))
    revisions = RevisionStore(str(tmp_path / "rev"))
    img = tmp_path / "i.jpg"
    Image.new("RGB", (8, 8)).save(img)
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
    return [seps[k].join(x["text"] for x in pieces if x[k] and x["text"]) for k in ("t", "u", "v")]


def test_the_form_starts_from_the_template_until_a_watermark_has_been_asked_for():
    got = watermark.starting_point(photo())
    assert (got["title"], got["subtitle"], got["third"], got["remembered"]) == ("Playa Hermosa, Costa Rica", "Bob · March 2024", "", False)
    # four fields: title, location, author and date; the title is empty, the rest come from Immich
    assert [(x["key"], x["text"]) for x in got["pieces"]] == [
        ("title", ""), ("location", "Playa Hermosa, Costa Rica"), ("author", "Bob"), ("date", "March 2024")]
    assert lines_of(got["pieces"]) == ["Playa Hermosa, Costa Rica", "Bob · March 2024", ""]


def test_asking_for_a_watermark_remembers_its_lines_pieces_and_ticks_for_next_time(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo()))
    pieces = [
        {"key": "title", "label": "Title", "text": ' Wings "at" Dusk ', "t": True, "u": False, "v": False},
        {"key": "location", "label": "Location", "text": "Playa Hermosa", "t": False, "u": True, "v": False},
        {"key": "author", "label": "Author", "text": "Bob", "t": False, "u": False, "v": True},
        {"key": "date", "label": "Date", "text": "March 2024", "t": False, "u": False, "v": True},
        {"key": "", "label": "Title idea", "text": "The Quiet Harbor", "t": False, "u": False, "v": False},
    ]
    watermark.add(store, "p", "Wings 'at' Dusk", "Playa Hermosa", "Bob · March 2024", pieces)
    last = store.load().photos["p"].last_watermark
    assert (last["title"], last["subtitle"], last["third"]) == ("Wings 'at' Dusk", "Playa Hermosa", "Bob · March 2024")
    got = watermark.starting_point(store.load().photos["p"])
    assert got["remembered"] is True
    assert [(x["key"], x["label"], x["text"], x["t"], x["u"], x["v"]) for x in got["pieces"]] == [
        ("title", "Title", "Wings 'at' Dusk", True, False, False),
        ("location", "Location", "Playa Hermosa", False, True, False),
        ("author", "Author", "Bob", False, False, True),
        ("date", "Date", "March 2024", False, False, True),
        ("", "Title idea", "The Quiet Harbor", False, False, False)]
    assert lines_of(got["pieces"]) == ["Wings 'at' Dusk", "Playa Hermosa", "Bob · March 2024"]
    watermark.add(store, "p", "Second try", "x")
    assert store.load().photos["p"].last_watermark["title"] == "Second try"


def test_a_watermark_remembered_by_an_older_version_keeps_its_lines_as_pieces():
    p = photo(last_watermark={"title": "Wings at Dusk", "subtitle": "Bob", "third": ""})
    got = watermark.starting_point(p)
    assert got["remembered"] is True and lines_of(got["pieces"]) == ["Wings at Dusk", "Bob", ""]
    assert [x["key"] for x in got["pieces"]][:4] == ["title", "location", "author", "date"]
    assert not any(x["t"] or x["u"] or x["v"] for x in got["pieces"] if x["key"])      # the four start unticked


def test_pieces_from_the_page_are_cleaned_and_bounded():
    got = watermark.clean_pieces([
        {"key": "title", "label": "x", "text": "a  <b>\"q\"</b>", "t": 1},
        {"key": "title", "label": "dup", "text": "again"},                 # a fixed one only once
        {"key": "bogus", "label": "Subject", "text": "Macaw", "u": True},   # an unknown key is an extra
        {"key": "", "label": "", "text": "no label, no key"},               # dropped
        {"text": 5, "label": 7},
    ] + [{"label": "Subject", "text": str(i)} for i in range(40)])
    assert got[0] == {"key": "title", "label": "x", "text": "a b'q'/b", "t": True, "u": False, "v": False}
    assert got[1]["key"] == "" and got[1]["text"] == "Macaw" and got[1]["u"] is True
    assert len(got) <= watermark.MAX_PIECES
    assert watermark.clean_pieces(None) is None
    for bad in ("x", [1], {"a": 1}):
        with pytest.raises(chat.ChatError):
            watermark.clean_pieces(bad)


def test_a_refused_watermark_is_not_remembered(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo()))
    with pytest.raises(chat.ChatError):
        watermark.add(store, "p", "", "", "")
    with pytest.raises(chat.ChatError):
        watermark.add(store, "p", "A", "", "", "not a list")
    assert store.load().photos["p"].last_watermark is None


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
