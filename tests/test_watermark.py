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
    watermark.add(store, "p", "Costa Rica", "Bob")
    msg = store.load().photos["p"].chat[0]
    assert msg.role == "user" and msg.state == "queued" and "Costa Rica" in msg.text


def test_a_photo_that_cannot_be_changed_is_refused(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    store.update(lambda lib: lib.photos.update(p=photo(media_type="video")))
    with pytest.raises(chat.ChatError):
        watermark.add(store, "p", "Costa Rica", "Bob")
    with pytest.raises(chat.ChatError) as e:
        watermark.add(store, "nope", "x", "y")
    assert e.value.status == 404
