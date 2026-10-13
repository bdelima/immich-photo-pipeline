"""The watermark builder: a template of up to three lines, seeded from what Immich knew
about the original (see metadata.py), which the reviewer can change before it
is sent. Sending it asks Claude, through the photo's chat, to add the text as a
new step in the photo's history (so it can be reverted like any other)."""
from __future__ import annotations

import calendar
import os
import re

from . import chat, geocode
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


# The four things a watermark is made of, each with its own field in the form. Every one can
# be placed on any of the three lines (t = first, u = second, v = third).
FIXED = (("title", "Title"), ("location", "Location"), ("author", "Author"), ("date", "Date"))
MAX_PIECES = 20
MAX_LABEL = 40


def base_pieces(photo: Photo, *, placed: bool) -> list[dict]:
    """The four fields filled in from the original's details: the title is empty, the location
    comes from Immich, the author is the uploader, the date is the month. `placed` puts the
    location on the first line and the author and date on the second, as the template always did."""
    p = parts(photo)
    texts = {"title": "", "location": p["place"], "author": p["owner"], "date": p["date"]}
    lines = {"title": "", "location": "t", "author": "u", "date": "u"}
    # `auto` marks a piece as taken from Immich just now (the page may tidy it, e.g. leave off the home country)
    return [{"key": key, "label": label, "text": texts[key], "auto": True,
             "t": placed and lines[key] == "t", "u": placed and lines[key] == "u", "v": False}
            for key, label in FIXED]


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
                    "t": bool(x.get("t")), "u": bool(x.get("u")), "v": bool(x.get("v"))})
    return out


def starting_point(photo: Photo) -> dict:
    """What the watermark form starts with: the last watermark asked for on
    this photo if there was one, otherwise the template from the original's
    details. `remembered` says which. `pieces` is what the form's fields and
    tick boxes hold: the four fixed ones (title, location, author, date) and any
    others that were added, each with the lines it is ticked for."""
    last = photo.last_watermark
    if isinstance(last, dict) and any(last.get(k) for k in ("title", "subtitle", "third")):
        saved = clean_pieces(last.get("pieces")) if isinstance(last.get("pieces"), list) else None
        if saved is None:
            # Remembered by an older version, which kept only the lines: each line is kept as a piece
            # ticked for its own line, so the lines come back exactly.
            saved = [{"key": "", "label": f"Line {n}", "text": last.get(k, ""), "t": n == 1, "u": n == 2, "v": n == 3}
                     for n, k in enumerate(("title", "subtitle", "third"), 1) if last.get(k)]
        have = {x["key"] for x in saved if x["key"]}
        fixed = [x for x in base_pieces(photo, placed=False) if x["key"] not in have]
        order = {key: i for i, (key, _) in enumerate(FIXED)}          # the four first, in order; the rest after
        pieces = sorted(fixed + saved, key=lambda x: order.get(x["key"], len(FIXED)))
        return {"title": last.get("title", ""), "subtitle": last.get("subtitle", ""),
                "third": last.get("third", ""), "pieces": pieces, "remembered": True}
    return {**template(photo), "third": "", "pieces": base_pieces(photo, placed=True), "remembered": False}


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
MAX_AVOID = 12
STYLES = ("subject", "title")


def _clean(text: str) -> str:
    return " ".join(text.replace('"', "'").replace("<", "").replace(">", "").split())


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
    earlier = [a for a in (_clean(a)[:MAX_LINE] for a in avoid or []) if a][-MAX_AVOID:]
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
    return place


def add(store: LibraryStore, photo_id: str, title, subtitle, third=None, pieces=None) -> int:
    """Queues the watermark as a request to Claude. Same rules as the chat box
    (a photo that can't be changed, or too many waiting, is refused). The lines
    are remembered on the photo, with the pieces and ticks they were made from."""
    text = instruction(title, subtitle, third)
    cleaned = clean_pieces(pieces)
    message_id = chat.enqueue(store, photo_id, text)
    last = {"title": _line(title, "title"), "subtitle": _line(subtitle, "second line"), "third": _line(third, "third line")}
    if cleaned is not None:
        last["pieces"] = cleaned
    store.update_photo(photo_id, lambda p: setattr(p, "last_watermark", last))
    return message_id
