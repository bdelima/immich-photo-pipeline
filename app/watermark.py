"""The watermark builder: a two-line template seeded from what Immich knew
about the original (see metadata.py), which the reviewer can change before it
is sent. Sending it asks Claude, through the photo's chat, to add the text as a
new step in the photo's history (so it can be reverted like any other)."""
from __future__ import annotations

import calendar

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


def template(photo: Photo) -> dict:
    """The starting title and second line, from the first original that has
    the details: the place; then the uploader's name and the month."""
    title, uploader, when = "", "", ""
    for source in photo.sources:
        m = source.meta or {}
        title = title or m.get("place", "")
        uploader = uploader or m.get("owner_name", "")
        when = when or month_year(m.get("taken", ""))
    return {"title": title, "subtitle": " · ".join(x for x in (uploader, when) if x)}


def _line(value, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise chat.ChatError(f"the {what} must be text")
    line = " ".join(value.replace('"', "'").split())
    if len(line) > MAX_LINE:
        raise chat.ChatError(f"the {what} is at most {MAX_LINE} characters; this one is {len(line)}")
    return line


def instruction(title, subtitle) -> str:
    title, subtitle = _line(title, "title"), _line(subtitle, "second line")
    if not title and not subtitle:
        raise chat.ChatError("there is nothing to write in the watermark")
    lines = [f'the title "{title}" on top' if title else "", f'"{subtitle}" ' + ("underneath it" if title else "") if subtitle else ""]
    return (
        "Add a small, tasteful watermark to this photo only (this is not a standing rule): "
        + " and ".join(x.strip() for x in lines if x)
        + ". Put it where it does not cover the subject and stays readable against the photo. "
        "Do not change anything else about the photo."
    )


def add(store: LibraryStore, photo_id: str, title, subtitle) -> int:
    """Queues the watermark as a request to Claude. Same rules as the chat box
    (a photo that can't be changed, or too many waiting, is refused)."""
    return chat.enqueue(store, photo_id, instruction(title, subtitle))
