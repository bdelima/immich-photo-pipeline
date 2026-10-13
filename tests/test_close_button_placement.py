import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(__file__)


def read(*parts):
    with open(os.path.join(HERE, "..", "app", "webui", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_there_is_a_close_in_the_photo_header_and_one_in_the_sidebar():
    page = read("templates", "index.html")
    head = page[page.index('$("vhead").innerHTML ='):page.index("const run = (path, what)")]
    assert 'id="vClose2"' in head and "close-top" in head
    assert page.count('id="vClose"') == 1 and 'class="asidebar"' in page
    assert '$("vClose").onclick = $("vClose2").onclick = closeViewer;' in page


def test_the_photo_header_close_only_shows_in_the_stacked_layout():
    css = read("static", "pipeline.css")
    assert re.search(r"#viewer \.close-top \{ display: none; \}", css)
    block = css[css.index("@media (max-width: 760px), (orientation: portrait)"):]
    assert "#viewer .close-top { display: inline-block; }" in block
    assert "#viewer .asidebar .btn { display: none; }" in block
