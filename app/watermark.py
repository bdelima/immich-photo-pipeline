"""The watermark builder: a template of up to three lines, seeded from what Immich knew
about the original (see metadata.py), which the reviewer can change before it
is sent. Sending it asks Claude, through the photo's chat, to add the text as a
new step in the photo's history (so it can be reverted like any other)."""
from __future__ import annotations

import calendar
import os

from . import chat
from .library import LibraryStore, Photo

MAX_LINE = 120


def month_year(taken: str) -> str:
    """"2024-03-02 14:05" -> "March 2024" ("" if it isn't a date)."""
    try:
        year, month = int(taken[0:4]), int(taken[5:7])
        return f"{calendar.month_name[month]} {year}"
    except (ValueError, IndexError):
        return ""


def parts(photo: Photo) -> dict:
    """What Immich knew about the photo, as separate pieces the builder can
    place on a line: the place, the uploader's name and the month. Each is
    taken from the first original that has it ("" when none does). The
    coordinates (None when none has them) let the page ask for a place when
    Immich could not name one."""
    place, owner, date, coordinates = "", "", "", None
    for source in photo.sources:
        m = source.meta or {}
        place = place or m.get("place", "")
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


def add(store: LibraryStore, photo_id: str, title, subtitle, third=None) -> int:
    """Queues the watermark as a request to Claude. Same rules as the chat box
    (a photo that can't be changed, or too many waiting, is refused)."""
    return chat.enqueue(store, photo_id, instruction(title, subtitle, third))
