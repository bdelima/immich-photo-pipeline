import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(__file__)


def read(*parts):
    with open(os.path.join(HERE, "..", "app", "webui", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_close_sits_in_the_sidebar_header_not_the_photo_header():
    page = read("templates", "index.html")
    assert page.count('id="vClose"') == 1
    bar = page[page.index('class="asidebar"'):]
    assert bar.index('id="vClose"') < bar.index("</div>")
    head = page[page.index('$("vhead").innerHTML ='):page.index('const run = (path, what)')]
    assert "vClose" not in head


def test_the_sidebar_scrolls_so_a_tall_watermark_form_is_never_cut_off():
    css = read("static", "pipeline.css")
    aside = next(line for line in css.splitlines() if line.startswith("#viewer aside {"))
    assert "overflow-y: auto" in aside
    assert "position: sticky" in css[css.index("#viewer .asidebar"):].splitlines()[0]
    assert "max-height" in next(line for line in css.splitlines() if line.startswith("#chatlog {"))


def test_each_tool_sits_with_the_line_it_fills_in():
    page = read("templates", "index.html")
    form = page[page.index("function watermarkForm"):page.index("// The country this container")]
    order = [form.index(k) for k in ('id="wmTitle"', 'id="wmHint"', 'id="wmIdentify"', 'id="wmSub"',
                                       'id="wmOsm"', 'id="wmThird"', 'id="wmGrid"', 'id="wmSend"')]
    assert order == sorted(order)
    assert "Immich location" in form
