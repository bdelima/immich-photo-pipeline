"""The watermark builder: up to three lines made of pieces (title, subject, location,
author, date), seeded from what Immich knew about the original (see metadata.py),
each line in its own font, colour and weight. Adding it draws the text here (see
wmrender.py) as a new step in the photo's history, so it can be reverted like any
other; Claude is only asked when the reviewer wants a title, a subject, a place or
a font suggested."""
from __future__ import annotations

import calendar
import datetime
import os
import re

from . import chat, geocode, wmrender
from .library import LibraryStore, Photo, Revision

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
                "pieces": pieces, "lines": clean_lines(last.get("lines")), "remembered": True}
    return {**template(photo), "third": "", "pieces": base_pieces(photo, placed=True), "lines": clean_lines(None), "remembered": False}


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


def instruction(title, subtitle, third=None) -> str:
    """The request to Claude. Up to three lines, none of them required (but
    at least one is): the title on top, a second line underneath it and an
    optional third below that."""
    title, subtitle, third = _line(title, "title"), _line(subtitle, "second line"), _line(third, "third line")
    if not title and not subtitle and not third:
        raise chat.ChatError("there is nothing to write in the watermark")
    lines = [
        f'the title "{title}" on top' if title else "",
        f'"{subtitle}" ' + ("underneath it" if title else "") if subtitle else "",
        f'"{third}" ' + ("below that" if title or subtitle else "") if third else "",
    ]
    return (
        "Add a small, tasteful watermark to this photo only (this is not a standing rule): "
        + " and ".join(x.strip() for x in lines if x)
        + ". Put it in the lower right corner, unless that would cover the subject; then use the nearest "
        "corner that does not. Set it in Arial, with a soft shadow and a slight highlight so the text looks "
        "a little 3D, and keep it readable against the photo. "
        "Do not change anything else about the photo."
    )


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


def _request(data: dict) -> tuple[list[str], dict]:
    """The lines' texts and what to remember, from what the form sent."""
    title, subtitle, third = data.get("title"), data.get("subtitle"), data.get("third")
    texts = [_line(title, "title"), _line(subtitle, "second line"), _line(third, "third line")]
    if not any(texts):
        raise chat.ChatError("there is nothing to write in the watermark")
    last = {"title": texts[0], "subtitle": texts[1], "third": texts[2], "lines": clean_lines(data.get("lines"))}
    cleaned = clean_pieces(data.get("pieces"))
    if cleaned is not None:
        last["pieces"] = cleaned
    return texts, last


def preview(store: LibraryStore, revisions, photo_id: str, data: dict) -> tuple[bytes, dict]:
    """A picture of the corner the watermark will sit on, with the text drawn as it will be.
    Nothing is changed. The texts may all be empty (the corner is shown plain)."""
    photo, path = _workable(store, revisions, photo_id)
    data = data if isinstance(data, dict) else {}
    texts = [_line(data.get("title"), "title"), _line(data.get("subtitle"), "second line"), _line(data.get("third"), "third line")]
    try:
        return wmrender.preview(path, _styled(texts, data.get("lines")), font_dir(revisions))
    except wmrender.FontError as exc:
        raise chat.ChatError(str(exc), 502)


class _Conflict(Exception):
    pass


def apply(store: LibraryStore, revisions, photo_id: str, data: dict) -> int:
    """Draws the watermark on the photo's current image as a new step in its history and
    returns that step's number. Drawn here, not by Claude. The lines, pieces and styles are
    remembered with the step (and as the photo's last watermark), so the form starts from them
    next time."""
    photo, path = _workable(store, revisions, photo_id)
    if photo.status != "ready":
        raise chat.ChatError("It isn't finished yet.")
    if photo.busy:
        raise chat.ChatError("It is being revised; wait for that to finish.")
    texts, last = _request(data if isinstance(data, dict) else {})
    expected_current = photo.current
    n = photo.next_revision_number()
    os.makedirs(revisions.root, exist_ok=True)
    out = os.path.join(revisions.root, f".wm-{photo_id}-{n}.jpg")
    try:
        wmrender.render(path, out, _styled(texts, last["lines"]), font_dir(revisions))
    except wmrender.FontError as exc:
        raise chat.ChatError(str(exc), 502)
    except ValueError as exc:
        raise chat.ChatError(str(exc))
    rel, digest = revisions.save_revision(photo_id, n, out)
    try:
        os.unlink(out)
    except OSError:
        pass
    note = summary(texts[0], texts[1], texts[2])

    def record(p: Photo) -> None:
        if p.trashed or p.current != expected_current or p.next_revision_number() != n:
            raise _Conflict()
        p.revisions.append(Revision(n=n, parent=p.current, file=rel, instruction=note, sha256=digest,
                                    origin="processed", watermark=last))
        p.current = n
        p.last_watermark = last

    try:
        store.update_photo(photo_id, record)
    except BaseException as exc:
        try:
            os.unlink(revisions.path(rel))
        except OSError:
            pass
        if isinstance(exc, _Conflict):
            raise chat.ChatError("the photo changed while the watermark was being drawn; try again", 409)
        raise
    return store.load().photos[photo_id].step_of(n)


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


def options(store: LibraryStore, immich, photo_id: str) -> dict:
    """What the pulldowns under the fields offer: the titles and subjects Claude suggested
    (newest first), the places found, the people Immich named in the photo, every Immich user
    (for the author), ways of writing the date, and the fonts and colours suggested. Immich is
    asked for the users and people; when it can't be, those lists are empty."""
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
        for source in photo.sources:
            try:
                people += [n for n in immich.asset_people(source.asset_id) if n and n not in people]
            except Exception:
                continue
    return {
        "title": recent("title") + [t for t in recent("subject") if t not in recent("title")],
        "location": recent("location"),
        "people": people,
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
