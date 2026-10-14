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


def test_working_on_the_watermark_folds_the_others_and_adding_it_opens_the_history():
    page = read("templates", "index.html")
    opening = page[page.index("const openWm = () => {"):page.index("const closeWm")]
    assert 'setFold("hist", false)' in opening and 'setFold("chat", false)' in opening and 'setFold("wm", true)' in opening
    sending = page[page.index('$("wmSend").onclick'):]
    assert 'closeWm(false); setFold("hist", true);' in sending           # the new step is in view
    assert "wmOpen" not in page            # the header opens the section now


def test_the_form_has_a_field_per_piece_a_server_drawn_preview_and_remembers_the_rows_and_styles():
    page = read("templates", "index.html")
    assert 'FIELDS = { title: "wmTitle", subject: "wmSubject", location: "wmLoc", author: "wmAuthor", date: "wmDate" }' in page
    for box in ('id="wmPreviewImg"', 'id="wmRows"', 'id="wmFields"'):
        assert box in page
    assert "Second line" not in page and "Third line" not in page and "1st" not in page   # the rows carry no labels
    assert "oninput = () => setText(it, $(boxId).value)" in page        # a field can always be edited by hand
    assert '"/api/photos/" + id + "/watermark/preview"' in page           # the preview is drawn by the server
    sending = page[page.index('$("wmSend").onclick'):]
    assert "pieces: items.map(" in sending and "lines: WM_LINES.map(k => lines[k])" in sending
    assert "x.t = x.u = x.v = false" not in page                           # the rows are kept after sending


def test_the_sections_have_styles():
    css = read("static", "pipeline.css")
    assert "#viewer .foldhead" in css and '#viewer .foldbody[hidden] { display: none; }' in css


def test_suggestions_become_choices_under_the_title_and_never_fill_it_in_by_themselves():
    page = read("templates", "index.html")
    ask = page[page.index("const ask = async (style, hint)"):page.index("const locate = async source")]
    assert "setText(" not in ask and "options.title = [...names" in ask        # newest first, nothing filled in
    assert "tick where" not in page                                              # no verbiage


def test_a_piece_is_on_one_line_only_and_a_later_add_is_written_later():
    page = read("templates", "index.html")
    adding = page[page.index('[data-add]")) s.onchange'):page.index('[data-aa]")) b.onclick')]
    assert "for (const j of WM_LINES) it[j] = 0;" in adding and "Math.max(0, ...items.map(o => o[k])) + 1" in adding
    assert "sort((a, b) => a[k] - b[k])" in page                                 # the line is written in that order
    assert "if (x[k] > 0 && seen) x[k] = 0;" in page                             # an older save is tidied on opening


def test_each_line_has_its_own_font_colour_bold_and_italic_and_a_font_can_be_asked_for():
    page = read("templates", "index.html")
    for bit in ('data-font="${k}"', 'data-color="${k}"', 'data-bold="${k}"', 'data-italic="${k}"', '<option value="__suggest">Suggest font…</option>',
                '"/api/photos/" + id + "/font"', 'data-fonthint="${k}"'):
        assert bit in page
    assert 'lines[k] = { font: s.font || "Liberation Sans", color: s.color || "auto"' in page


def test_old_rows_of_ideas_fold_into_the_title_and_location_choices():
    page = read("templates", "index.html")
    fold = page[page.index("const extras = { title: [], location: [] };"):page.index("items = items.filter(x => x.key);")]
    assert 'const into = /location/i.test(x.label) ? "location" : "title";' in fold
    assert "target.text = x.text; target[line] = x[line];" in fold


def test_the_preview_is_the_photo_corner_and_not_huge_text():
    css = read("static", "pipeline.css")
    assert "#viewer .wm .wmpreview img { display: block; width: 100%;" in css
    assert "font-size: 1.6rem" not in css


def test_a_photo_opens_with_every_section_of_the_sidebar_folded():
    page = read("templates", "index.html")
    assert "const folds = { hist: false, chat: false, wm: false };" in page
    opening = page[page.index("async function openViewer"):page.index('$("viewer").classList.add("open")')]
    assert 'if (!$("viewer").classList.contains("open")) for (const k of Object.keys(folds)) folds[k] = false;' in opening
