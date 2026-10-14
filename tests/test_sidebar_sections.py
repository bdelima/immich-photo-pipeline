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


def test_the_corner_can_be_picked_under_the_rows_and_a_chat_edits_note_is_shown():
    page = read("templates", "index.html")
    assert 'WM_CORNERS = [["auto", "Auto"], ["lower right", "Lower right"]' in page
    assert '<div class="wmrow corner"><span class="fl">Corner</span><select class="pd" data-corner' in page
    assert 'Look, from a chat edit: <b>${esc(styleNote)}</b>' in page
    sending = page[page.index('$("wmSend").onclick'):]
    assert "corner, style_note: styleNote" in sending
    assert "lines: WM_LINES.map(k => lines[k]), corner }" in page                   # the preview follows the corner


def test_the_preview_is_drawn_on_the_original_and_a_click_shows_it_whole():
    page = read("templates", "index.html")
    assert 'id="wmWhole" hidden>Click to see it whole</span>' in page
    assert 'const src = r.headers.get("X-Watermark-Source");' in page
    assert '(onOriginal ? " on the original · " + drawn + " corner"' in page and '" · no original on file"' in page
    click = page[page.index('$("wmPreview").onclick'):page.index("// The colour each line was drawn in")]
    assert "if (!onOriginal) return;" in click and '"/api/photos/" + id + "/watermark/whole"' in click
    assert 'const o = $("overlay");' in click and "closeOriginal()" in click      # the overlay the original's thumbnail uses
    css = read("static", "pipeline.css")
    assert "#viewer .wm .wmpreview.whole { cursor: zoom-in; }" in css


def test_the_subject_pulldown_is_current_and_one_combined_entry():
    page = read("templates", "index.html")
    assert 'if (key === "subject") return options.subject ? [[options.subject, options.subject]] : [];' in page
    assert "options.people" not in page


def test_the_sidebar_title_is_immich_photo_pipeline_with_the_site_icon_beside_it():
    page = read("templates", "index.html")
    brand = page[page.index('<div class="brand">'):page.index("</nav>")]
    assert "icon-small.svg" in brand and brand.index("<img") < brand.index("<h1>")      # the icon is to the left
    assert "<h1>Immich Photo Pipeline</h1>" in brand
    assert "nav .brand { display: flex;" in read("static", "pipeline.css")
    assert '(h.closest(".brand") || h).after(el)' in read("static", "app.js")           # the version goes under the row


def test_the_preview_says_when_it_was_drawn_on_the_current_image_because_the_original_cannot_be_opened():
    page = read("templates", "index.html")
    assert 'src === "current" ? " on the current image (the original can\'t be opened here) · "' in page
    assert 'onOriginal = src === "original";' in page
