"""Short, plain addresses for albums: "Costa Rica" is #album:costa-rica.

A slug is the name lowercased, with accents dropped and every run of
characters that is not a letter or digit turned into one hyphen. Two names
can give the same slug ("Costa Rica" and "Costa-Rica"), so creating one that
clashes with an existing album is refused (see actions.album_name). For
albums that already clash, `slug_map` keeps the addresses unique: the first
name in sorted order keeps the plain slug and the rest get -2, -3, ...
"""
from __future__ import annotations

import re
import unicodedata


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "album"


def slug_map(names) -> dict[str, str]:
    """Album name -> its unique slug."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in sorted(set(names)):
        base = slugify(name)
        slug, n = base, 1
        while slug in taken:
            n += 1
            slug = f"{base}-{n}"
        taken.add(slug)
        out[name] = slug
    return out


def album_for_slug(names, key: str) -> str | None:
    """The album a view key means: its exact name (old links, with the
    spaces as %20) or its slug."""
    names = list(names)
    if key in names:
        return key
    for name, slug in slug_map(names).items():
        if slug == key:
            return name
    return None
