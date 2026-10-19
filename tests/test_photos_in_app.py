"""Photos and Recipe rules are views inside the main page, not pages of their own."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import LibraryStore
from app.revisions import RevisionStore
from app.rules import RulesStore
from app.thumbs import ThumbCache
from app.webui.server import create_app

HERE = os.path.join(os.path.dirname(__file__), "..", "app", "webui")


def read(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as fh:
        return fh.read()


class Immich:
    pass


def client(tmp_path, immich=True, rules=True):
    app = create_app(
        None, Immich() if immich else None, LibraryStore(str(tmp_path / "lib.json")), None,
        rules=RulesStore(str(tmp_path / "rules.json")) if rules else None,
        revisions=RevisionStore(str(tmp_path / "rev")), thumbs=ThumbCache(str(tmp_path / "th")))
    return app.test_client()


def test_there_are_no_pages_called_photos_or_rules_any_more(tmp_path):
    c = client(tmp_path)
    assert c.get("/photos").status_code == 404 and c.get("/rules").status_code == 404
    for gone in ("photos.html",):
        assert not os.path.exists(os.path.join(HERE, "templates", gone))
    assert "/photos" not in read("static", "app.js") and 'href="/rules"' not in read("templates", "index.html")


def test_the_main_page_holds_both_views_and_the_sidebar_offers_what_the_server_can_show(tmp_path):
    page = client(tmp_path).get("/").get_data(as_text=True)
    assert 'id="photosView"' in page and 'id="rulesView"' in page and 'id="pipeView"' in page
    assert 'window.FEATURES = {"photos": true, "rules": true}' in page.replace("\n", "")
    assert "photos.js" in page and "rules.js" in page and "photos.css" in page
    only = client(tmp_path, immich=False, rules=False).get("/").get_data(as_text=True)
    assert 'window.FEATURES = {"photos": false, "rules": false}' in only.replace("\n", "")


def test_the_sidebar_is_photos_a_line_the_places_a_line_then_recipe_rules():
    js = read("static", "app.js")
    body = js[js.index("async function renderTree"):js.index("// A small version hint")]
    order = [body.index(k) for k in ('paneItem("photos"', 'class="sep"', 'treeItem("queue"', 'paneItem("collage"', 'treeItem("review"', 'treeItem("live"',
                                      '>Albums<', 'treeItem("trash"', 'class="links"', 'paneItem("rules"')]
    assert order == sorted(order)
    assert body.count('<div class="sep">') == 1 and body.count('<div class="links">') == 1
    assert 'PANE_VIEWS = { photos: "Photos", collage: "Collage Maker", rules: "Recipe rules" }' in js
    assert 'class="brand"' in read("templates", "index.html") and "Immich Photo Pipeline" in read("templates", "index.html")


def test_choosing_photos_or_rules_swaps_the_area_on_the_right_and_nothing_else():
    page = read("templates", "index.html")
    assert 'const isPane = v => v === "photos" || v === "collage" || v === "rules";' in page
    assert "if (isPane(v) && paneOn(v)) return openPane(v);" in page
    assert 'PhotosPane.show(v)' in page and "RulesPane.show()" in page and "PhotosPane.hide()" in page
    assert "if (isPane(view)) return;" in page                       # the live sync has no grid to keep in step there
    assert '#tree a.item:not(.pane)' in page                         # and its count check ignores the two views


def test_the_rules_view_is_drawn_by_the_server_and_redrawn_after_a_change(tmp_path):
    c = client(tmp_path)
    c.post("/api/rules", json={"text": "Keep it tidy.", "scope": "all"})
    html = c.get("/api/rules/view").get_data(as_text=True)
    assert "<html" not in html and "Keep it tidy." in html and 'data-max-chars="' in html and 'id="add-form"' in html
    js = read("static", "rules.js")
    assert "location.reload" not in js and "await show()" in js     # redraws in place
    assert client(tmp_path, rules=False).get("/api/rules/view").status_code == 404


def test_the_timeline_follows_the_photos_down_to_the_day_and_jumps_to_the_newest_in_a_row():
    js = read("static", "photos.js")
    assert "const FOLLOW_MS = 120;" in js and "clearTimeout(followTimer); followTimer = setTimeout(follow, FOLLOW_MS)" in js
    assert 'node(g.y, g.y, g.n, "y", true)' in js                    # counts on years only
    assert 'node(m.month, shortMonth(m.month), 0, "mo", true' in js and 'node(d, dayShort(d), 0, "dy", false' in js
    assert "async function jump(key)" in js and "auto[k] = true" in js and "delete auto[k]" in js
    assert "a row the person opened stays" in js
    css = read("static", "photos.css")
    assert re.search(r"\.ph-scroll \{[^}]*overflow-y: auto", css) and re.search(r"\.ph-tl \{[^}]*width: 5\.6rem", css)


def test_months_are_kept_and_only_near_rows_are_drawn_with_the_next_and_previous_fetched_ahead():
    js = read("static", "photos.js")
    assert "if (s.items) return Promise.resolve(s);" in js           # a month's list is fetched once per session
    assert "function clearRows" in js and "TILE_NEAR" in js and "s.rendered" in js
    assert "for (const j of [i - 1, i + 1])" in js and "new Image().src = thumbUrl" in js
    assert not re.search(r"s\.items\s*=\s*null", js)                 # never thrown away
    assert "opacity: 0" in read("static", "photos.css") and ".ph-tile img.in" in read("static", "photos.css")


def test_the_viewer_has_stepped_zoom_icons_and_the_round_i_and_the_actions():
    js = read("static", "photos.js")
    assert "const ZSTEP = 0.25, ZMAX = 4;" in js and 'id="pv-fit"' in js and 'id="pv-out"' in js and 'id="pv-in"' in js
    assert 'class="pv-i" id="pv-info"' in js and "pv-zoom" not in js                      # the old Zoom/Info text buttons are gone
    assert '$("pv-out").hidden = $("pv-fit").hidden = $("pv-pct").hidden = vid || zoom <= 1;' in js     # shown only while zoomed in
    assert "e.ctrlKey" in js and "pointerdown" in js                                      # Ctrl + wheel zooms, a drag pans
    assert 'id="pv-send"' in js and 'id="pv-dl"' in js and 'id="pv-trash"' in js


def test_photos_search_select_send_download_and_trash_use_the_household_routes():
    js = read("static", "photos.js")
    for route in ("/api/household/search?", "/api/household/filters", "/api/household/suggest?type=", "/api/household/send",
                  "/api/household/trash", "/api/household/download", "/download"):
        assert route in js
    assert 'month?bucket=" + encodeURIComponent(s.bucket) + (showSent ? "&sent=1" : "")' in js      # in-pipeline photos hidden unless asked
    assert "Show photos already in the pipeline" in js and "let showSent = false" in js
    assert '"Send and purge"' in js and "pipeline === \"trash\"" in js                              # the purge offer for photos in the trash
    assert "purge: trashed.length > 0" in js and "/api/household/purge" not in js                  # one request: the server purges only what it sent
    assert "a video can\\'t go in a collage" in js
    assert '"ph-pick"' in js and '"ph-dpick' in js and "Select day" in js


def test_collage_maker_is_a_place_under_queue_that_shares_the_photos_view_and_has_a_count():
    app, page, photos = read("static", "app.js"), read("templates", "index.html"), read("static", "photos.js")
    assert 'feat.photos ? paneItem("collage", active, waiting)' in app and '"/api/collage/count"' in app    # offered with Photos
    assert 'v !== "photos" && v !== "collage"' in page and 'document.addEventListener("collage-changed"' in page
    assert "async function enterCollage()" in photos and "function leaveCollage()" in photos and "/api/collage/waiting" in photos


def test_collage_maker_selects_two_to_twelve_and_makes_or_removes_with_the_wireframes_words():
    js = read("static", "photos.js")
    assert "const COLLAGE_MIN = 2, COLLAGE_MAX = 12;" in js
    for text in ('"Make collage (select " + COLLAGE_MIN + " or more)"', '"Make collage (" + COLLAGE_MAX + " at most)"', '"Make collage from " + n',
                 '"A collage takes " + COLLAGE_MAX + " photos at most. Deselect "', "Select the ones that belong together (",
                 "Sent to Claude. The collage will appear in Review when it is done.",
                 "Removed from Collage Maker. The photos stay in the owner's Immich library."):
        assert text in js
    assert '"/api/collage/make", { ids }' in js and '"/api/collage/remove", { ids }' in js           # ids in the order selected
    assert 'for (const id of ["ph-selday", "ph-seldl", "ph-seltrash", "ph-selsend"]) $(id).hidden = on;' in js     # no Send to, Download, Trash, Select day
    assert ".ph-collage .ph-dpick { display: none; }" in read("static", "photos.css")


def test_the_viewer_in_collage_maker_has_only_select_remove_zoom_and_details():
    js = read("static", "photos.js")
    assert '$("pv-send").hidden = $("pv-dl").hidden = $("pv-trash").hidden = collage;' in js
    assert '$("pv-pick").hidden = $("pv-remove").hidden = !collage;' in js
    assert '"Selected ✓" : "Select for collage"' in js and 'id="pv-remove" hidden>Remove from Collage Maker' in js


def test_search_results_are_shown_like_the_timeline_by_month_and_day_with_their_own_timeline():
    js = read("static", "photos.js")
    assert 'id="ph-rtl"' in js and "function rebuildResults()" in js and "buildTimeline(months)" in js     # months, days and a timeline of the results
    assert "function enterSearch()" in js and "function leaveSearch()" in js                             # the main page's state is put aside, then back
    assert "resSec" not in js and "flatItems" in js                                                      # the viewer steps across months of results
    assert "a result that was also on the timeline had taken its entry" in js                          # clearing a search gives the timeline its photos back for selecting


def test_the_browsers_back_button_closes_the_viewer_the_same_as_its_own_back_button():
    js = read("static", "photos.js")
    assert 'history.pushState({ phViewer: 1 }, "")' in js and 'window.addEventListener("popstate"' in js
    assert '$("pv-back").onclick = backFromViewer;' in js and "history.back()" in js


def test_the_browsers_back_button_closes_the_photo_window_and_the_original_over_it_in_the_pipeline_views():
    page = read("templates", "index.html")
    assert "function pushLayer(name, close)" in page and 'window.addEventListener("popstate"' in page
    assert 'pushLayer("viewer", closeViewer)' in page and 'pushLayer("overlay", closeOriginal)' in page
    assert 'backLayer("viewer", closeViewer)' in page and 'backLayer("overlay", closeOriginal)' in page
    assert "$(\"vClose\").onclick = $(\"vClose2\").onclick = closeViewer;" not in page      # the buttons give the history step back
