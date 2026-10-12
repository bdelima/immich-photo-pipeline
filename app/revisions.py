"""The revision store: a directory holding a copy of every original a photo
was made from and every revision of it, kept for as long as the photo exists
(trashing a photo keeps them; only purging it deletes them).

It is deliberately one directory (REVISIONS_PATH, /data/revisions by
default) so it can be bind-mounted onto a bigger disk. The library records
the relative path of each file; nothing else here knows about photos.

Layout:
    <photo id>/sources/<index>-<name>     the originals, as they were dropped
    <photo id>/revisions/<n>.<ext>        revision n of the photo
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RevisionStoreError(ValueError):
    pass


def _check_id(photo_id: str) -> str:
    if not _ID_RE.match(photo_id or ""):
        raise RevisionStoreError(f"unsafe photo id {photo_id!r}")
    return photo_id


def _safe_name(name: str) -> str:
    base = _NAME_UNSAFE.sub("_", os.path.basename(name or "")).strip("._")
    return base or "file"


class RevisionStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def path(self, relpath: str) -> str:
        """The absolute path of a stored file. Rejects anything that would
        point outside the store."""
        if not relpath:
            raise RevisionStoreError("empty path")
        full = os.path.abspath(os.path.join(self.root, relpath))
        if os.path.commonpath([self.root, full]) != self.root or full == self.root:
            raise RevisionStoreError(f"path {relpath!r} is outside the revision store")
        return full

    def exists(self, relpath: str) -> bool:
        if not relpath:
            return False
        try:
            return os.path.isfile(self.path(relpath))
        except RevisionStoreError:
            return False

    def save_source(self, photo_id: str, index: int, src_path: str, name: str = "") -> str:
        """Copies an original into the store; returns its relative path."""
        _check_id(photo_id)
        rel = os.path.join(photo_id, "sources", f"{index}-{_safe_name(name or src_path)}")
        self._copy_in(src_path, rel)
        return rel

    def save_revision(self, photo_id: str, n: int, src_path: str) -> tuple[str, str]:
        """Copies a finished image in as revision `n`; returns (relative
        path, sha256). Refuses to overwrite an existing revision: they are
        never changed once written."""
        _check_id(photo_id)
        ext = os.path.splitext(src_path)[1].lower()
        if not re.match(r"^\.[a-z0-9]{1,5}$", ext):
            ext = ".jpg"
        rel = os.path.join(photo_id, "revisions", f"{n}{ext}")
        if self.exists(rel):
            raise RevisionStoreError(f"revision {n} of {photo_id} already exists")
        digest = self._copy_in(src_path, rel)
        return rel, digest

    def purge(self, photo_id: str) -> None:
        """Deletes everything stored for a photo. Only for a photo being
        deleted for good."""
        _check_id(photo_id)
        shutil.rmtree(os.path.join(self.root, photo_id), ignore_errors=True)

    def _copy_in(self, src_path: str, rel: str) -> str:
        dest = self.path(rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        digest = hashlib.sha256()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as out, open(src_path, "rb") as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return digest.hexdigest()
