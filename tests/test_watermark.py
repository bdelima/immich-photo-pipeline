import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import chat, watermark
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
    assert recipe.avoids[0] is None and recipe.avoids[1] == ["Old 'one'", "Two"]
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
    assert recipe.avoids[-1] == ["Old"]
    assert client.post("/api/photos/p/identify", json={"avoid": "Old"}).status_code == 400
    assert client.post("/api/photos/p/identify", json={"style": "limerick"}).status_code == 400
    assert client.post("/api/photos/nope/identify", json={}).status_code == 404
    bare = create_app(None, None, store, None, revisions=revisions, thumbs=ThumbCache(str(tmp_path / "th"))).test_client()
    assert bare.post("/api/photos/p/identify", json={}).status_code == 503


# ---- starting from the last watermark -----------------------------------------------------


def test_the_form_starts_from_the_template_until_a_watermark_has_been_asked_for():
    got = watermark.starting_point(photo())
    assert got == {"title": "Playa Hermosa, Costa Rica", "subtitle": "Bob · March 2024", "third": "", "remembered": False}


def test_asking_for_a_watermark_remembers_its_lines_for_next_time(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo()))
    watermark.add(store, "p", ' Wings "at" Dusk ', "", "Bob")
    last = store.load().photos["p"].last_watermark
    assert last == {"title": "Wings 'at' Dusk", "subtitle": "", "third": "Bob"}
    assert watermark.starting_point(store.load().photos["p"]) == {**last, "remembered": True}
    watermark.add(store, "p", "Second try", "x")
    assert store.load().photos["p"].last_watermark["title"] == "Second try"


def test_a_refused_watermark_is_not_remembered(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo()))
    with pytest.raises(chat.ChatError):
        watermark.add(store, "p", "", "", "")
    assert store.load().photos["p"].last_watermark is None


def test_an_older_library_without_the_field_still_loads():
    from app.library import library_from_dict, library_to_dict, Library
    lib = Library(photos={"p": photo()})
    raw = library_to_dict(lib)
    del raw["photos"]["p"]["last_watermark"]
    assert library_from_dict(raw).photos["p"].last_watermark is None
