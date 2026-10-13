import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(__file__)


def read(*parts):
    with open(os.path.join(HERE, "..", "app", "webui", *parts), encoding="utf-8") as fh:
        return fh.read()


def panel_code():
    page = read("templates", "index.html")
    start = page.index('$("panel").innerHTML =')
    return page[start:page.index("wireFolds();", start)]


def test_the_sidebar_has_history_chat_and_watermark_in_that_order():
    code = panel_code()
    order = [code.index(k) for k in ('foldSection("hist", "History"', 'foldSection("chat", "Chat"', 'foldSection("wm", "Watermark"')]
    assert order == sorted(order)


def test_working_on_the_watermark_folds_the_others_and_adding_it_opens_the_chat():
    page = read("templates", "index.html")
    opening = page[page.index("const openWm = () => {"):page.index("const closeWm")]
    assert 'setFold("hist", false)' in opening and 'setFold("chat", false)' in opening and 'setFold("wm", true)' in opening
    sending = page[page.index('$("wmSend").onclick'):]
    assert 'closeWm(false); setFold("chat", true);' in sending
    assert "wmOpen" not in page            # the header opens the section now


def test_the_form_has_four_fields_a_live_preview_and_remembers_the_ticks():
    page = read("templates", "index.html")
    for box in ('id="wmTitle"', 'id="wmLoc"', 'id="wmAuthor"', 'id="wmDate"', 'id="wmPreview"'):
        assert box in page
    assert "Second line" not in page and "Third line" not in page          # the lines come from the ticks
    # every field can be edited by hand, and the preview follows what is typed or ticked
    assert "oninput = () => setText(it, $(boxId).value)" in page
    assert page.count("drawGrid(); drawPreview();") >= 3
    sending = page[page.index('$("wmSend").onclick'):]
    assert "pieces: items.map(" in sending
    assert "x.t = x.u = x.v = false" not in page                           # the ticks are kept after sending


def test_the_sections_have_styles():
    css = read("static", "pipeline.css")
    assert "#viewer .foldhead" in css and '#viewer .foldbody[hidden] { display: none; }' in css


def test_suggesting_a_title_puts_the_first_idea_in_the_title_field():
    page = read("templates", "index.html")
    assert "setText(title, names[0] || title.text)" in page
    assert "if (!title.t) {" in page


def test_the_preview_is_big_enough_to_read():
    css = read("static", "pipeline.css")
    assert "#viewer .wm .wmpreview .l1 { font-size: 1.6rem;" in css
