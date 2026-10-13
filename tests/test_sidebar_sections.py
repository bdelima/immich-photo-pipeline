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


def test_the_form_starts_from_the_remembered_lines_without_ticking_pieces():
    page = read("templates", "index.html")
    assert 'value="${esc(w.third || "")}"' in page
    assert "fromTemplate = !w.remembered" in page


def test_the_sections_have_styles():
    css = read("static", "pipeline.css")
    assert "#viewer .foldhead" in css and '#viewer .foldbody[hidden] { display: none; }' in css


def test_suggesting_a_title_leaves_the_remembered_second_and_third_lines_alone():
    page = read("templates", "index.html")
    assert "(firstTime && fromTemplate) || k === \"t\"" in page
    assert "if (firstTime && fromTemplate) for (const o of items) if (o.fixed)" in page
