import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(__file__)


def read(*parts):
    with open(os.path.join(HERE, "..", "app", "webui", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_count_changes_are_marked_until_the_row_is_clicked_and_a_drop_to_zero_clears_on_a_timer():
    js = read("static", "app.js")
    assert 'PILL_VIEWS = ["queue", "review", "trash"]' in js
    assert "ZERO_CLEAR_MS = 10000" in js
    assert "function noteCount" in js and "function paintCount" in js and "function clearCountMark" in js
    assert 'class="cdelta ' in js and 'e.target.closest("#tree a.item")' in js        # a click on the row clears its mark
    page = read("templates", "index.html")
    assert "paintCount(a, a.dataset.view, counts[i])" in page


def test_the_badge_and_pill_have_styles():
    css = read("static", "pipeline.css")
    for bit in ("nav .count .n.pill", "nav .count.changed .n", "nav .count .cdelta", "--up:", "--down:"):
        assert bit in css


def test_the_original_opens_with_an_info_button_beside_close_and_a_three_group_popup():
    page = read("templates", "index.html")
    assert "showOriginal(p.id, p.sources.find(x => x.index === +b.dataset.orig), b.dataset.type)" in page
    fn = page[page.index("function showOriginal"):page.index("function toggleInfo")]
    assert 'id="infoBtn"' in fn and 'aria-label="About this original"' in fn and 'class="btn sec sm close"' in fn
    assert fn.index("infobtn") < fn.index("close")                         # the "i" is left of Close
    assert "tools.onclick = e => e.stopPropagation()" in fn and '$("infoPop").onclick = e => e.stopPropagation()' in fn
    info = page[page.index("function originalInfo"):page.index("// The finished photo")]
    order = [info.index(k) for k in ('"Owner"', '"Place"', '"Coordinates"', '["Size"', '["Camera"', '["Exposure"', '["File name"', '["Asset ID"')]
    assert order == sorted(order)
    assert '.join("<hr>")' in info and ".filter(r => r[1])" in info          # empty rows and groups are left out
    assert "toFixed(4)" in info                                              # place equal to the coordinates is hidden


def test_escape_closes_the_popup_before_the_picture():
    page = read("templates", "index.html")
    assert 'if (!$("infoPop") || $("infoPop").hidden) closeOriginal(); else toggleInfo(false);' in page


def test_the_info_button_matches_close_in_height_and_is_round():
    css = read("static", "pipeline.css")
    assert "#overlay .tools .close, #overlay .infobtn { height: 1.75rem;" in css and "#overlay .infobtn { width: 1.75rem; border-radius: 50%;" in css
    assert "#overlay .infopop" in css and "user-select: all" in css
