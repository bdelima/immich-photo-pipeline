"""A disk copy of the Immich thumbnails the Photos view shows.

The Photos timeline is browsed back and forth, and every thumbnail would otherwise be fetched
from Immich again each time. The first request for a thumbnail fetches it and keeps the bytes
here; every later request is a local file read.

There is deliberately no size limit: a thumbnail is a few tens of kilobytes (about 4 GB per
100,000 photos), and the copy is disposable, so deleting the folder only means the thumbnails
are fetched again on demand. An Immich thumbnail only changes if the photo is edited there; a
copy is kept as it is until the folder is cleared.
"""
from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger(__name__)

_EXT = {"image/jpeg": "jpg", "image/webp": "webp", "image/png": "png", "image/avif": "avif", "image/gif": "gif"}
_TYPE = {ext: mime for mime, ext in _EXT.items()}


class ImmichThumbCache:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _stem(self, asset_id: str, size: str) -> str:
        # Asset ids are checked as UUIDs by the caller (household.valid_asset_id); the folder
        # fan-out keeps any one directory small.
        return os.path.join(self.root, asset_id[:2], f"{asset_id}-{size}")

    def get(self, asset_id: str, size: str) -> tuple[str, str] | None:
        """(path, content type) of the kept copy, or None if there is none."""
        stem = self._stem(asset_id, size)
        for ext, mime in _TYPE.items():
            path = f"{stem}.{ext}"
            if os.path.isfile(path):
                return path, mime
        return None

    def put(self, asset_id: str, size: str, data: bytes, content_type: str) -> tuple[str, str] | None:
        """Keeps a fetched picture; returns where, or None if it could not be written (the
        caller then serves the bytes it has -- a full disk must not break the page)."""
        mime = (content_type or "").split(";")[0].strip().lower()
        ext = _EXT.get(mime)
        if not ext or not data:
            return None
        path = f"{self._stem(asset_id, size)}.{ext}"
        tmp = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)          # a reader never sees half a file
            return path, mime
        except OSError as exc:
            log.warning("could not keep the thumbnail of %s: %s", asset_id, exc)
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return None
