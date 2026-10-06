"""Thumbnails for the web UI, made on first request and kept on disk.

A revision file is never overwritten (see app/revisions.py), so a thumbnail
made from it never goes stale: the cache key is just the file's path in the
revision store plus the size. Trashing or reverting a photo changes which
revision is shown, not the files, so nothing here is ever invalidated.

Pillow does the work. If it is missing, or an image can't be read, `get`
returns None and the UI falls back to a placeholder rather than failing.
Videos get no thumbnail (the container has no ffmpeg).
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile

from .revisions import RevisionStore, RevisionStoreError

log = logging.getLogger(__name__)

DEFAULT_SIZE = 360
QUALITY = 82


class ThumbCache:
    def __init__(self, root: str, size: int = DEFAULT_SIZE):
        self.root = os.path.abspath(root)
        self.size = size

    def _cached_path(self, relpath: str) -> str:
        key = hashlib.sha1(f"{self.size}:{relpath}".encode("utf-8")).hexdigest()
        return os.path.join(self.root, key[:2], f"{key}.jpg")

    def get(self, revisions: RevisionStore, relpath: str) -> str | None:
        """The path of the thumbnail for a stored image, making it if needed.
        None if it can't be made."""
        try:
            source = revisions.path(relpath)
        except RevisionStoreError:
            return None
        cached = self._cached_path(relpath)
        if os.path.isfile(cached):
            return cached
        if not os.path.isfile(source):
            return None
        try:
            from PIL import Image, ImageOps
        except ImportError:  # pragma: no cover - Pillow is in requirements.txt
            log.warning("Pillow is not installed; thumbnails are unavailable")
            return None
        tmp = None
        try:
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            with Image.open(source) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((self.size, self.size))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cached), suffix=".tmp")
                os.close(fd)
                img.save(tmp, "JPEG", quality=QUALITY)
            os.replace(tmp, cached)
            return cached
        except Exception:
            log.warning("could not make a thumbnail for %s", relpath, exc_info=True)
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            return None
