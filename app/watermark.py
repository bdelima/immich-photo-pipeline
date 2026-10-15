"""The watermark builder: up to three lines made of pieces (title, subject, location,
author, date), seeded from what Immich knew about the original (see metadata.py),
each line in its own font, colour and weight. The preview is drawn here (see
wmrender.py); adding it sends Claude an exact description of what the preview showed
(text, font files, colours, sizes, corner) as a chat request, so Claude re-runs the
photo with the earlier steps replayed, draws the watermark in place of any earlier
one, and the result is a new step in the history that can be reverted like any other."""
from __future__ import annotations

import calendar
import datetime
import os
import re

from . import chat, geocode, wmrender
from .library import LibraryStore, Photo

MAX_LINE = 120


def month_year(taken: str) -> str:
    """"2024-03-02 14:05" -> "March 2024" ("" if it isn't a date)."""
    try:
        year, month = int(taken[0:4]), int(taken[5:7])
        return f"{calendar.month_name[month]} {year}"
    except (ValueError, IndexError):
        return ""


# Immich can leave a place that is just the coordinates; that is not a place name.
_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parts(photo: Photo) -> dict:
    """What Immich knew about the photo, as separate pieces the builder can
    place on a line: the place, the uploader's name and the month. Each is
    taken from the first original that has it ("" when none does). The
    coordinates (None when none has them) let the page offer to name the place
    when Immich could not. A place that is only a pair of numbers counts as no
    place (the numbers are kept as coordinates)."""
    place, owner, date, coordinates = "", "", "", None
    for source in photo.sources:
        m = source.meta or {}
        text = m.get("place", "")
        looks = _COORDS.match(text) if isinstance(text, str) else None
        if looks and coordinates is None:
            coordinates = [float(looks.group(1)), float(looks.group(2))]
        place = place or ("" if looks else text)
        owner = owner or m.get("owner_name", "")
        date = date or month_year(m.get("taken", ""))
        if coordinates is None and m.get("latitude") is not None and m.get("longitude") is not None:
            coordinates = [m["latitude"], m["longitude"]]
    return {"place": place, "owner": owner, "date": date, "coordinates": coordinates}


def template(photo: Photo) -> dict:
    """The starting title and second line: the place; then the uploader's
    name and the month."""
    p = parts(photo)
    return {"title": p["place"], "subtitle": " · ".join(x for x in (p["owner"], p["date"]) if x)}


# The five things a watermark is made of, each with its own field in the form. Every one can
# be placed on one of the three lines (t = first, u = second, v = third).
FIXED = (("title", "Title"), ("subject", "Subject"), ("location", "Location"), ("author", "Author"), ("date", "Date"))
MAX_PIECES = 20
MAX_LABEL = 40


def base_pieces(photo: Photo, *, placed: bool) -> list[dict]:
    """The five fields filled in from the original's details: the title and subject are empty, the location
    comes from Immich, the author is the uploader, the date is the month. `placed` puts the
    location on the first line and the author, then the date, on the second, as the template always did."""
    p = parts(photo)
    texts = {"title": "", "subject": "", "location": p["place"], "author": p["owner"], "date": p["date"]}
    lines = {"title": "", "subject": "", "location": "t", "author": "u", "date": "u"}
    # `auto` marks a piece as taken from Immich just now (the page may tidy it, e.g. leave off the home country)
    return [{"key": key, "label": label, "text": texts[key], "auto": True,
             "t": int(placed and lines[key] == "t"), "u": int(placed and lines[key] == "u") * (2 if key == "date" else 1), "v": 0}
            for key, label in FIXED]


MAX_TICK = 999


def _tick(value) -> int:
    """Whether a piece is on a line, and where: 0 is off, otherwise the order it was ticked in
    (the smaller comes first on the line). A plain true, as older versions saved, is 1."""
    if value is True:
        return 1
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, min(value, MAX_TICK))
    return 0


def clean_pieces(pieces) -> list[dict] | None:
    """The pieces the form sent (or that were saved), made safe: None if there are none."""
    if pieces is None:
        return None
    if not isinstance(pieces, list) or not all(isinstance(x, dict) for x in pieces):
        raise chat.ChatError("the pieces must be a list")
    keys = {key for key, _ in FIXED}
    out: list[dict] = []
    for x in pieces[:MAX_PIECES]:
        key = x.get("key") if x.get("key") in keys else ""
        if key and any(o["key"] == key for o in out):
            continue
        label = _clean(x.get("label") if isinstance(x.get("label"), str) else "")[:MAX_LABEL]
        text = _clean(x.get("text") if isinstance(x.get("text"), str) else "")[:MAX_LINE]
        if not key and not label:
            continue
        out.append({"key": key, "label": label or dict(FIXED)[key], "text": text,
                    "t": _tick(x.get("t")), "u": _tick(x.get("u")), "v": _tick(x.get("v"))})
    return out


def _remembered(photo: Photo) -> dict | None:
    """The watermark the form should start from: the one made at the step now showing, or else at
    the nearest earlier step that made the image (a revert shows the one of the step it reverts
    to). A photo none of whose steps carries one (watermarked before this was kept per step) uses
    the last one asked for. None means the template."""
    for rev in reversed(photo.lineage()):
        if rev.watermark:
            return rev.watermark
    if not any(r.watermark for r in photo.revisions):
        return photo.last_watermark
    return None


def starting_point(photo: Photo) -> dict:
    """What the watermark form starts with: the last watermark asked for on
    this photo if there was one, otherwise the template from the original's
    details. `remembered` says which. `pieces` is what the form's fields and
    rows hold: the five fixed ones (title, subject, location, author, date) and any
    others that were added, each with the lines it is ticked for."""
    last = _remembered(photo)
    if isinstance(last, dict) and any(last.get(k) for k in ("title", "subtitle", "third")):
        saved = clean_pieces(last.get("pieces")) if isinstance(last.get("pieces"), list) else None
        if saved is None:
            # Remembered by an older version, which kept only the lines: each line is kept as a piece
            # ticked for its own line, so the lines come back exactly.
            saved = [{"key": "", "label": f"Line {n}", "text": last.get(k, ""), "t": int(n == 1), "u": int(n == 2), "v": int(n == 3)}
                     for n, k in enumerate(("title", "subtitle", "third"), 1) if last.get(k)]
        have = {x["key"] for x in saved if x["key"]}
        fixed = [x for x in base_pieces(photo, placed=False) if x["key"] not in have]
        order = {key: i for i, (key, _) in enumerate(FIXED)}          # the five first, in order; the rest after
        pieces = sorted(fixed + saved, key=lambda x: order.get(x["key"], len(FIXED)))
        return {"title": last.get("title", ""), "subtitle": last.get("subtitle", ""), "third": last.get("third", ""),
                "pieces": pieces, "lines": clean_lines(last.get("lines")), "corner": clean_corner(last.get("corner")),
                "style_note": clean_note(last.get("style_note")), "remembered": True}
    return {**template(photo), "third": "", "pieces": base_pieces(photo, placed=True), "lines": clean_lines(None),
            "corner": "auto", "style_note": "", "remembered": False}


# ---- the style of each line ----
MAX_LINES = 3
_COLOR_WORDS = ("auto", "white", "black")


def clean_lines(lines) -> list[dict]:
    """The style of the three lines ({font, color, bold, italic} each), as sent or saved, made
    safe; what is missing is the default (Liberation Sans, auto colour, plain)."""
    out = []
    for i in range(MAX_LINES):
        x = lines[i] if isinstance(lines, list) and i < len(lines) and isinstance(lines[i], dict) else {}
        font = _clean(x.get("font") if isinstance(x.get("font"), str) else "")[:wmrender.MAX_FONT_NAME] or wmrender.DEFAULT_FONT
        color = (x.get("color") if isinstance(x.get("color"), str) else "auto").strip().lower()
        if color not in _COLOR_WORDS and wmrender.parse_color(color) is None:
            color = "auto"
        out.append({"font": font, "color": color, "bold": bool(x.get("bold")), "italic": bool(x.get("italic"))})
    return out


CORNERS = ("auto",) + wmrender.CORNERS
MAX_NOTE = 160


def clean_corner(value) -> str:
    """"lower right" etc., or "auto" (the renderer picks)."""
    v = " ".join(value.lower().split()).replace("-", " ") if isinstance(value, str) else "auto"
    return v if v in CORNERS else "auto"


def clean_note(value) -> str:
    """A short free-text note about the watermark's look that the fields can't say (a glow, a tilt),
    kept so a chat edit can reproduce it."""
    return _clean(value)[:MAX_NOTE] if isinstance(value, str) else ""


def _styled(texts: list[str], lines) -> list[dict]:
    """The lines as the renderer wants them: text plus style."""
    return [{"text": t, **st} for t, st in zip(texts, clean_lines(lines))]


def font_dir(revisions) -> str | None:
    """Where fetched fonts are kept: beside the revisions (None in tests with no store)."""
    return os.path.join(revisions.root, "_fonts") if revisions is not None else None


def _line(value, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise chat.ChatError(f"the {what} must be text")
    line = " ".join(value.replace('"', "'").split())
    if len(line) > MAX_LINE:
        raise chat.ChatError(f"the {what} is at most {MAX_LINE} characters; this one is {len(line)}")
    return line


MAX_HINT = 80
MAX_AVOID = 15
STYLES = ("subject", "title")


def _clean(text: str) -> str:
    return " ".join(text.replace('"', "'").replace("<", "").replace(">", "").split())


def _recent(texts: list) -> list[str]:
    """The last MAX_AVOID different, non-empty texts, the newest kept when one repeats."""
    out: list[str] = []
    for t in texts:
        if isinstance(t, str) and t:
            if t in out:
                out.remove(t)
            out.append(t)
    return out[-MAX_AVOID:]


def identify(store: LibraryStore, revisions, recipe, photo_id: str, hint=None, style="subject", avoid=None) -> list[str]:
    """Asks Claude to look at the photo's current image and say something to
    seed the watermark text: its name (style "subject": the main subject, or
    what `hint` points at; one answer) or three short poetic titles, the way a
    painting is named (style "title"). `avoid` lists earlier answers so a
    retry differs. Nothing is changed or added to the history."""
    if style not in STYLES:
        raise chat.ChatError("the style must be 'subject' or 'title'")
    if recipe is None or revisions is None:
        raise chat.ChatError("Claude isn't available here right now.", 503)
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise chat.ChatError("no such photo", 404)
    if photo.media_type == "video":
        raise chat.ChatError("Videos can't be looked at.")
    if photo.trashed:
        raise chat.ChatError("It is in the trash. Restore it to work on it.")
    revision = photo.current_revision()
    path = revisions.path(revision.file) if revision and revision.file else None
    if not path or not os.path.exists(path):
        raise chat.ChatError("There is no image to look at yet.")
    if hint is not None and not isinstance(hint, str):
        raise chat.ChatError("the hint must be text")
    hint = _clean(hint or "")
    if len(hint) > MAX_HINT:
        raise chat.ChatError(f"the hint is at most {MAX_HINT} characters; this one is {len(hint)}")
    if avoid is not None and (not isinstance(avoid, list) or not all(isinstance(a, str) for a in avoid)):
        raise chat.ChatError("the earlier answers must be a list of text")
    kept = photo.suggestions.get(style) if isinstance(photo.suggestions, dict) else None
    earlier = _recent((kept if isinstance(kept, list) else []) + [_clean(a)[:MAX_LINE] for a in avoid or []])
    try:
        found = recipe.identify(path, hint or None, style, earlier or None)
    except RuntimeError as exc:
        raise chat.ChatError(f"Claude couldn't identify it: {exc}", 502)
    names: list[str] = []
    for name in found:
        name = _clean(name)[:MAX_LINE]
        if name and name not in names:
            names.append(name)
    if not names:
        raise chat.ChatError("Claude did not name anything", 502)
    # Remember them on the photo (the last MAX_AVOID per style), so a later ask differs even
    # after the photo was closed and opened again.
    def remember(p: Photo) -> None:
        old = p.suggestions if isinstance(p.suggestions, dict) else {}
        p.suggestions = {**old, style: _recent((old.get(style) if isinstance(old.get(style), list) else []) + names)}
    try:
        store.update_photo(photo_id, remember)
    except KeyError:
        pass
    return names


SOURCES = ("osm", "claude")


def locate(store: LibraryStore, recipe, photo_id: str, source: str) -> str:
    """Names the place at the photo's coordinates, from OpenStreetMap
    (source "osm") or by asking Claude ("claude"), so the two can be compared.
    Read-only: nothing about the photo changes."""
    if source not in SOURCES:
        raise chat.ChatError("the source must be 'osm' or 'claude'")
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise chat.ChatError("no such photo", 404)
    coordinates = parts(photo)["coordinates"]
    if not coordinates:
        raise chat.ChatError("This photo has no coordinates.")
    if source == "claude":
        if recipe is None:
            raise chat.ChatError("Claude isn't available here right now.", 503)
        try:
            place = recipe.locate(*coordinates)
        except RuntimeError as exc:
            raise chat.ChatError(f"Claude couldn't name the place: {exc}", 502)
    else:
        place = geocode.reverse(*coordinates)
        if not place:
            raise chat.ChatError("OpenStreetMap couldn't name that place (or couldn't be reached).", 502)
    place = _clean(place)[:MAX_LINE]
    if not place:
        raise chat.ChatError("No place came back.", 502)
    remember_place(store, photo_id, place)
    return place


def summary(title, subtitle, third=None) -> str:
    """What the history shows for a watermark step: its lines, quoted."""
    texts = [t for t in (_line(title, "title"), _line(subtitle, "second line"), _line(third, "third line")) if t]
    if not texts:
        raise chat.ChatError("there is nothing to write in the watermark")
    return "Watermark: " + " / ".join(f'"{t}"' for t in texts)


def _workable(store: LibraryStore, revisions, photo_id: str) -> tuple[Photo, str]:
    """The photo and the path of its current image, or a ChatError saying why not."""
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise chat.ChatError("no such photo", 404)
    if photo.media_type == "video":
        raise chat.ChatError("Videos can't be watermarked.")
    if photo.trashed:
        raise chat.ChatError("It is in the trash. Restore it to work on it.")
    revision = photo.current_revision()
    path = revisions.path(revision.file) if revisions is not None and revision and revision.file else None
    if not path or not os.path.exists(path):
        raise chat.ChatError("There is no image to write on yet.")
    return photo, path


def _request(data: dict, source: str = "none") -> tuple[list[str], dict]:
    """The lines' texts and what to remember, from what the form sent. `source` is what the
    preview was drawn on (see preview_source)."""
    title, subtitle, third = data.get("title"), data.get("subtitle"), data.get("third")
    texts = [_line(title, "title"), _line(subtitle, "second line"), _line(third, "third line")]
    if not any(texts):
        raise chat.ChatError("there is nothing to write in the watermark")
    last = {"title": texts[0], "subtitle": texts[1], "third": texts[2], "lines": clean_lines(data.get("lines")),
            "corner": clean_corner(data.get("corner")), "style_note": clean_note(data.get("style_note"))}
    cleaned = clean_pieces(data.get("pieces"))
    if cleaned is not None:
        last["pieces"] = cleaned
    # What the preview showed: the corner the renderer picked and the colour each line came out
    # in. They stand in for "auto" only when the preview was drawn on the current image, the one
    # Claude's result will look like. A preview on the original (or on a plain panel) measured a
    # different picture: a dark sky there may be a pale mat under the text on the result, so "auto"
    # is left for Claude to judge on the image it draws on.
    drawn = data.get("drawn") if isinstance(data.get("drawn"), dict) else {}
    if source == "current":
        corner = clean_corner(drawn.get("corner"))
        colors = drawn.get("colors") if isinstance(drawn.get("colors"), list) else []
        colors = [c.strip().lower() for c in colors if isinstance(c, str) and wmrender.parse_color(c)][:MAX_LINES]
        if corner != "auto" or colors:
            last["drawn"] = {"corner": corner, "colors": colors}
    return texts, last


def original_path(photo: Photo, revisions) -> str | None:
    """The stored copy of the photo's first original, or None when there is none on file (an
    imported photo). The preview is drawn on this rather than on the current step: it is a rough
    idea of how the text looks against the picture, and the original has no mat and no earlier
    watermark on it to muddle that."""
    if revisions is None:
        return None
    for source in photo.sources:
        if source.file:
            path = revisions.path(source.file)
            if os.path.exists(path):
                return path
    return None


def preview_source(photo: Photo, revisions) -> str:
    """What the preview is drawn on: "original" when the first original is on file and can be
    opened here, "current" when it is on file but can't be (a HEIC, for example), so the photo's
    current image stands in, and "none" when there is no original (an imported photo)."""
    path = original_path(photo, revisions)
    if not path:
        return "none"
    return "original" if wmrender.can_open(path) else "current"


def _preview_texts(data: dict) -> list[str]:
    return [_line(data.get("title"), "title"), _line(data.get("subtitle"), "second line"), _line(data.get("third"), "third line")]


def preview(store: LibraryStore, revisions, photo_id: str, data: dict) -> tuple[bytes, dict]:
    """A picture of the corner the watermark will sit on, drawn on the original (or, with no
    original on file, on a plain panel), with the text as it will be. Nothing is changed. The
    texts may all be empty (the corner is shown plain). The info says which: "source" is
    "original", "current" (the original can't be opened here, a HEIC for example, so the photo's
    current image is used) or "none"."""
    photo, current = _workable(store, revisions, photo_id)
    data = data if isinstance(data, dict) else {}
    path = original_path(photo, revisions)
    lines = _styled(_preview_texts(data), data.get("lines"))
    corner = clean_corner(data.get("corner"))
    source = "original" if path else "none"
    try:
        try:
            png, info = wmrender.preview(path, lines, font_dir(revisions), corner=corner)
        except wmrender.ImageError:
            if not path:
                raise
            png, info = wmrender.preview(current, lines, font_dir(revisions), corner=corner)
            source = "current"
    except wmrender.ImageError as exc:
        raise chat.ChatError(f"The picture can't be drawn on: {exc}", 422)
    except wmrender.FontError as exc:
        raise chat.ChatError(str(exc), 502)
    info["source"] = source
    return png, info


def whole(store: LibraryStore, revisions, photo_id: str, data: dict) -> tuple[bytes, dict]:
    """The whole original with the watermark drawn on it as the form stands, as a JPEG for the
    screen (what a click on the preview shows). 404 when there is no original on file."""
    photo, _ = _workable(store, revisions, photo_id)
    data = data if isinstance(data, dict) else {}
    path = original_path(photo, revisions)
    if not path:
        raise chat.ChatError("There is no original on file to show.", 404)
    try:
        return wmrender.whole(path, _styled(_preview_texts(data), data.get("lines")), font_dir(revisions),
                              corner=clean_corner(data.get("corner")))
    except wmrender.ImageError:
        raise chat.ChatError("The original can't be shown here (its format isn't one this server can open).", 422)
    except wmrender.FontError as exc:
        raise chat.ChatError(str(exc), 502)


def submit(store: LibraryStore, revisions, photo_id: str, data: dict) -> int:
    """Sends the watermark to Claude as a chat request: the photo is re-run with its earlier steps
    replayed and this watermark drawn in place of any earlier one (see chat._revise), the result
    a new step in the history. Returns the queued message's id. The lines, pieces, styles and
    what the preview showed are kept with the request, so the step remembers them and the form
    starts from them next time. Fonts are fetched first, so Claude is given files that exist."""
    photo, _ = _workable(store, revisions, photo_id)
    texts, last = _request(data if isinstance(data, dict) else {}, preview_source(photo, revisions))
    for text, st in zip(texts, last["lines"]):
        if text:
            try:
                wmrender.font_file(st["font"], st["bold"], st["italic"], font_dir(revisions))
            except wmrender.FontError as exc:
                raise chat.ChatError(str(exc), 502)
    return chat.enqueue(store, photo_id, summary(texts[0], texts[1], texts[2]), watermark=last)


# ---- suggesting a font ----
MAX_COLOR_NAME = 30


def suggest_font(store: LibraryStore, revisions, recipe, photo_id: str, texts, hint=None) -> dict:
    """Asks Claude to look at the photo and the text and name a font (one Google Fonts has) and
    a colour for it. The font is fetched so it can be drawn; if it can't be, that is an error.
    Returns {"font", "color", "color_name", "why"}; the font and colour are remembered on the
    photo so the form offers them again."""
    if recipe is None or revisions is None:
        raise chat.ChatError("Claude isn't available here right now.", 503)
    photo, path = _workable(store, revisions, photo_id)
    if hint is not None and not isinstance(hint, str):
        raise chat.ChatError("the hint must be text")
    hint = _clean(hint or "")
    if len(hint) > MAX_HINT:
        raise chat.ChatError(f"the hint is at most {MAX_HINT} characters; this one is {len(hint)}")
    if texts is not None and (not isinstance(texts, list) or not all(isinstance(t, str) for t in texts)):
        raise chat.ChatError("the texts must be a list of text")
    texts = [_clean(t)[:MAX_LINE] for t in texts or [] if _clean(t)]
    try:
        found = recipe.suggest_font(path, texts, hint or None)
    except RuntimeError as exc:
        raise chat.ChatError(f"Claude couldn't suggest a font: {exc}", 502)
    font = _clean(found.get("font") if isinstance(found.get("font"), str) else "")[:wmrender.MAX_FONT_NAME]
    color = (found.get("color") if isinstance(found.get("color"), str) else "").strip().lower()
    if wmrender.parse_color(color) is None:
        color = "auto"
    color_name = _clean(found.get("color_name") if isinstance(found.get("color_name"), str) else "")[:MAX_COLOR_NAME]
    why = _clean(found.get("why") if isinstance(found.get("why"), str) else "")[:MAX_LINE]
    if not font:
        raise chat.ChatError("Claude did not name a font", 502)
    try:
        wmrender.check_font(font, font_dir(revisions))
    except wmrender.FontError as exc:
        raise chat.ChatError(f"Claude suggested '{font}', but {exc}", 502)
    swatch = f"{color} {color_name}".strip() if color != "auto" else ""

    def remember(p: Photo) -> None:
        old = p.suggestions if isinstance(p.suggestions, dict) else {}
        fonts = _recent((old.get("font") if isinstance(old.get("font"), list) else []) + [font])
        colors = _recent((old.get("color") if isinstance(old.get("color"), list) else []) + ([swatch] if swatch else []))
        p.suggestions = {**old, "font": fonts, "color": colors}
    try:
        store.update_photo(photo_id, remember)
    except KeyError:
        pass
    return {"font": font, "color": color, "color_name": color_name, "why": why}


# ---- the watermark as the chat path sees it ----
# A chat edit re-runs Claude from the original with every earlier step described to it (see
# replay.py). A watermark drawn here is described in full, with the font files on disk, so Claude
# can reproduce it faithfully when the request is about something else, and has every detail to
# start from when the request is about the watermark itself.


def _spec(state: dict, revisions) -> str:
    """The watermark spelled out for Claude: each line's text, font (and file), colour and weight,
    the corner, and the sizing rule the renderer uses. "" when there is nothing to write. Fonts
    are fetched if need be; one that can't be is named without a file. The corner and colours
    the preview showed (`drawn`, kept only when the preview was on the current image) stand in
    for "auto"; otherwise "auto" is left for Claude to judge on the image it draws on."""
    if not isinstance(state, dict):
        return ""
    texts = [state.get("title", ""), state.get("subtitle", ""), state.get("third", "")]
    lines = clean_lines(state.get("lines"))
    drawn = state.get("drawn") if isinstance(state.get("drawn"), dict) else {}
    drawn_colors = [c for c in (drawn.get("colors") or []) if isinstance(c, str)]
    parts = []
    n = 0
    for i, (text, st) in enumerate(zip(texts, lines), 1):
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            path, _ = wmrender.font_file(st["font"], st["bold"], st["italic"], font_dir(revisions))
        except wmrender.FontError:
            path = None
        weight = " ".join(w for w, on in (("bold", st["bold"]), ("italic", st["italic"])) if on)
        color = st["color"]
        if color == "auto":
            color = drawn_colors[n] if n < len(drawn_colors) else "white on a dark patch or near-black on a light one"
        n += 1
        parts.append(f'line {i} "{text.strip()}" in {st["font"]}' + (f" {weight}" if weight else "") + (f" (file {path})" if path else "")
                     + f", colour {color}")
    if not parts:
        return ""
    corner = clean_corner(state.get("corner"))
    if corner == "auto":
        corner = clean_corner(drawn.get("corner"))
    where = corner if corner != "auto" else "the lower right corner, or another if that one is busy"
    note = clean_note(state.get("style_note"))
    return ("; ".join(parts) + f". Placed in {where} with a margin of 2.5% of the shorter side; the first line's text "
            "height is 3.2% of the shorter side and the others' 2.4%; a soft drop shadow and a faint highlight; "
            "right-aligned in a right-hand corner, left-aligned in a left-hand one"
            + (f". Its look beyond that: {note}" if note else "") + ".")


def describe(state: dict, revisions) -> str:
    """The replay text for an earlier watermark step, so a later chat edit reproduces it."""
    spec = _spec(state, revisions)
    if not spec:
        return ""
    return "Watermark drawn by the pipeline: " + spec + " Reproduce it exactly unless the new request is about the watermark."


def request(state: dict, revisions) -> str:
    """The chat request that adds the watermark from the builder: draw exactly this, in place of
    any earlier watermark, and change nothing else."""
    spec = _spec(state, revisions)
    if not spec:
        return ""
    return ("Draw this watermark on the photo, exactly as specified, using the font files named (fetch a missing one from "
            "Google Fonts and keep it beside the others): " + spec + " This watermark replaces any earlier one: do not draw "
            "any watermark described or asked for in an earlier step. Change nothing else about the photo.")


_REPORT_KEYS = ("title", "subtitle", "third")


def from_report(report, previous: dict | None, revisions) -> dict | None:
    """The watermark state for a step Claude made, from what it reported drawing ({"lines":
    [{"text","font","color","bold","italic"}...], "corner", "style_note", "font_files": {family:
    path}}). Lines whose text is unchanged keep their pieces from `previous`; a changed line's
    text goes to the first piece not on any line. A font file Claude fetched is kept in the font
    folder. None when the report is empty or unusable."""
    if not isinstance(report, dict) or not isinstance(report.get("lines"), list):
        return None
    raw = [x for x in report["lines"] if isinstance(x, dict)][:MAX_LINES]
    texts = [_clean(x.get("text") if isinstance(x.get("text"), str) else "")[:MAX_LINE] for x in raw] + [""] * (MAX_LINES - len(raw))
    if not any(texts):
        return None
    files = report.get("font_files") if isinstance(report.get("font_files"), dict) else {}
    for family, src in files.items():
        if isinstance(family, str) and isinstance(src, str) and revisions is not None:
            wmrender.keep_font_file(_clean(family)[:wmrender.MAX_FONT_NAME], src, font_dir(revisions))
    lines = clean_lines(raw)
    for st in lines:
        if st["font"] not in wmrender.INSTALLED:
            try:
                wmrender.check_font(st["font"], font_dir(revisions))
            except wmrender.FontError:
                st["font"] = wmrender.DEFAULT_FONT          # named but not drawable here: the builder falls back
    prev = previous if isinstance(previous, dict) else {}
    pieces = clean_pieces(prev.get("pieces")) if isinstance(prev.get("pieces"), list) else None
    if pieces is None:
        pieces = [{"key": k, "label": lbl, "text": "", "t": 0, "u": 0, "v": 0} for k, lbl in FIXED]
    keys = ("t", "u", "v")
    for i, key in enumerate(_REPORT_KEYS):
        if texts[i] == (prev.get(key) or ""):
            continue
        for x in pieces:
            x[keys[i]] = 0
        if not texts[i]:
            continue
        free = next((x for x in pieces if x["key"] and not any(x[k] for k in keys)), None)
        if free is None:
            free = {"key": "", "label": f"Line {i + 1}", "text": "", "t": 0, "u": 0, "v": 0}
            pieces.append(free)
        free["text"], free[keys[i]] = texts[i], 1
    return {"title": texts[0], "subtitle": texts[1], "third": texts[2], "pieces": pieces, "lines": lines,
            "corner": clean_corner(report.get("corner")), "style_note": clean_note(report.get("style_note"))}


# ---- the choices under each field ----
_DATE_FORMATS = ("{month} {day}, {year}", "{day} {month} {year}", "{weekday}, {month} {day}, {year}", "{month} {year}", "{year}")


def date_choices(taken: str) -> list[str]:
    """Natural ways of writing the day the photo was taken ("2026-05-12 12:32"): May 12, 2026 ·
    12 May 2026 · Tuesday, May 12, 2026 · May 2026 · 2026. [] when there is no date."""
    try:
        d = datetime.date(int(taken[0:4]), int(taken[5:7]), int(taken[8:10]))
    except (ValueError, IndexError, TypeError):
        return []
    f = {"month": calendar.month_name[d.month], "day": d.day, "year": d.year, "weekday": calendar.day_name[d.weekday()]}
    return [fmt.format(**f) for fmt in _DATE_FORMATS]


def _taken(photo: Photo) -> str:
    for source in photo.sources:
        if (source.meta or {}).get("taken"):
            return source.meta["taken"]
    return ""


def people_in(photo: Photo, clients) -> list[str]:
    """The people Immich has named in the photo's originals, without repeats, in the order the
    originals were added. Named people are per account in Immich: the pipeline's own account can
    see a household member's original but not the names they gave the faces in it, so each
    account is asked in turn (the pipeline's first, then every extra key) and the first that
    names anyone is taken for that original. An account that fails or sees nothing never stops
    the next being asked."""
    people: list[str] = []
    for source in photo.sources:
        if not source.asset_id:
            continue
        for client in clients:
            try:
                found = [n for n in client.asset_people(source.asset_id) if n]
            except Exception:
                continue
            if found:
                people += [n for n in found if n not in people]
                break
    return people


# Immich's named people are not offered for the Subject field for now. Immich keeps people per
# account (a face in one household member's photo is a different person from the same face in
# another's), and its people-sharing feature does not merge what was already recognised, so in
# practice the originals' faces are unnamed and the lookup returned nothing. Set this to True to
# ask again (people_in, with every account's key) once the household's people are named in Immich.
USE_IMMICH_PEOPLE = False


def options(store: LibraryStore, immich, photo_id: str, extra=()) -> dict:
    """What the pulldowns under the fields offer: the titles and subjects Claude suggested
    (newest first), the places found, every Immich user (for the author), ways of writing the
    date, and the fonts and colours suggested. The people Immich named in the photo (one entry,
    the names joined with commas) only with USE_IMMICH_PEOPLE; otherwise none. Immich is asked
    for the users (and the people, with the extra accounts' keys tried, see people_in); when it
    can't be, those are empty."""
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise chat.ChatError("no such photo", 404)
    sug = photo.suggestions if isinstance(photo.suggestions, dict) else {}
    recent = lambda key: list(reversed(sug.get(key))) if isinstance(sug.get(key), list) else []
    users: list[str] = []
    people: list[str] = []
    if immich is not None:
        try:
            users = [u for u in immich.list_user_names() if u]
        except Exception:
            users = []
        if USE_IMMICH_PEOPLE:
            people = people_in(photo, (immich, *extra))
    return {
        "title": recent("title") + [t for t in recent("subject") if t not in recent("title")],
        "location": recent("location"),
        "people": people,
        "subject": ", ".join(people),
        "users": users,
        "dates": date_choices(_taken(photo)),
        "fonts": list(wmrender.INSTALLED),
        "suggested_fonts": recent("font"),
        "suggested_colors": recent("color"),
    }


def remember_place(store: LibraryStore, photo_id: str, place: str) -> None:
    """Keeps a place a lookup found, so the Location pulldown offers it again."""
    def remember(p: Photo) -> None:
        old = p.suggestions if isinstance(p.suggestions, dict) else {}
        p.suggestions = {**old, "location": _recent((old.get("location") if isinstance(old.get("location"), list) else []) + [place])}
    try:
        store.update_photo(photo_id, remember)
    except KeyError:
        pass
