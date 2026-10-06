"""The pipeline's library: every photo it knows about, where each one lives,
and every revision it has ever had.

This replaces the old per-lineage state in state.py for the web app. The
principle behind it: the library is the truth, and Immich albums are
projections of it (see projection.py). A photo's home, whether it is
promoted to Live, and whether it is trashed are all decided here, never read
back from an Immich album, like or comment.

Plain JSON on disk, written atomically (write-temp + rename). The poll loop,
the web UI's request threads and the job worker all write to it, so nothing
loads the whole file, works for a minute and saves it again. Every change is
a short `LibraryStore.update(fn)`: the lock is held only for the load, the
change and the save, never across an Immich or Claude call.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

SCHEMA_VERSION = 1

# Where a photo can live besides a managed album (named by the user).
INBOX = "inbox"      # picked up from an entry queue, no result yet
REVIEW = "review"    # processed, waiting for a decision

# A photo's progress through the pipeline.
STATUS_WAITING = "waiting"                  # a lone portrait waiting for a partner
STATUS_PROCESSING = "processing"
STATUS_AWAITING_ANSWER = "awaiting_answer"  # Claude asked a question about the photo
STATUS_FAILED = "failed"
STATUS_READY = "ready"

T = TypeVar("T")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Source:
    """One original photo or video that a photo was made from."""
    # The contributor's Immich asset. It is theirs, so it may be deleted at
    # any time: the copy in the revision store is what reprocessing uses.
    asset_id: str
    # Path of the stored copy, relative to the revision store ("" if none).
    file: str = ""
    name: str = ""
    owner_id: str = ""


@dataclass
class Revision:
    """One version of a photo. Revisions are only ever appended, and each one
    is made on top of the one that was current (`parent`), so the list is a
    plain, linear history. A revert is a new revision too: it holds a copy of
    the image it reverts to (`reverts_to`), so "revert to step 5" adds a step
    and nothing is ever hidden or abandoned."""
    n: int
    parent: int | None
    # Path of the image file, relative to the revision store.
    file: str
    # What the reviewer asked for to make this version (None for the first
    # result, and for an imported one).
    instruction: str | None = None
    # The standing rules in force for the run that made it.
    rules: list[str] = field(default_factory=list)
    session_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    sha256: str = ""
    # "processed" (made by the recipe), "legacy" (imported finished),
    # "original" (a video, untouched) or "reverted" (a copy of an earlier
    # revision, see `reverts_to`).
    origin: str = "processed"
    # For a revert: the revision whose image this one is a copy of. Its
    # instruction is None; the instructions that produced the image are
    # those of the revision it reverts to (see Photo.lineage).
    reverts_to: int | None = None


@dataclass
class Photo:
    # Stable for the photo's whole life. For a photo carried over from the
    # old state this is its old lineage id.
    id: str
    # "single" or "collage".
    kind: str = "single"
    # "image" or "video". Videos are managed (browse, promote, trash, move)
    # but not yet reprocessed.
    media_type: str = "image"
    sources: list[Source] = field(default_factory=list)
    revisions: list[Revision] = field(default_factory=list)
    # The revision number currently shown and published, if there is one.
    current: int | None = None
    # INBOX, REVIEW, or the name of a managed album.
    home: str = INBOX
    status: str = STATUS_READY
    # The entry queue it came from: "wallpaper", "collage", or None.
    queue: str | None = None
    # Promoted to the Live album. Independent of the album it is in.
    live: bool = False
    trashed: bool = False
    trashed_at: str | None = None
    # Where it was when trashed, so a restore can say so.
    trashed_from: str | None = None
    # The Immich asset that publishes the current revision (always one the
    # pipeline account owns). None while there is no result yet, and while
    # trashed.
    immich_asset_id: str | None = None
    # The revision that asset shows. When it differs from `current`, the
    # photo needs publishing again (see publish.py).
    published_revision: int | None = None
    # Assets this photo no longer uses (an older revision's upload, or the
    # asset of a photo that was trashed), waiting to be moved to Immich's
    # trash. Kept until Immich has done it, so a failure is retried.
    stale_asset_ids: list[str] = field(default_factory=list)
    # While status is awaiting_answer: Claude's question, and the headless
    # session to resume with the answer.
    question: str | None = None
    session_id: str | None = None
    # While status is failed.
    error: str | None = None
    # The instructions the old pipeline recorded for a photo carried over
    # from it. The intermediate images never existed as files, so these are
    # shown but cannot be reverted to.
    legacy_notes: list[str] = field(default_factory=list)
    # True if there is no original to reprocess from (a photo that was
    # already finished when the pipeline first saw it).
    imported: bool = False
    created_at: str = field(default_factory=now_iso)

    # ---- revisions -------------------------------------------------------

    def revision(self, n: int | None) -> Revision | None:
        if n is None:
            return None
        for rev in self.revisions:
            if rev.n == n:
                return rev
        return None

    def current_revision(self) -> Revision | None:
        return self.revision(self.current)

    def next_revision_number(self) -> int:
        return max((r.n for r in self.revisions), default=-1) + 1

    def chain(self, n: int | None = None) -> list[Revision]:
        """The history up to `n` (default: the current revision), oldest
        first, following parents. This is what the user sees as steps 0, 1,
        2 ... Reverts are steps like any other."""
        rev = self.revision(self.current if n is None else n)
        out: list[Revision] = []
        seen: set[int] = set()
        while rev is not None and rev.n not in seen:
            out.append(rev)
            seen.add(rev.n)
            rev = self.revision(rev.parent)
        out.reverse()
        return out

    def content_source(self, n: int) -> int | None:
        """The revision whose image revision `n` really is: `n` itself,
        unless it is a revert, in which case the one it reverts to (followed
        through further reverts)."""
        seen: set[int] = set()
        rev = self.revision(n)
        while rev is not None and rev.reverts_to is not None and rev.n not in seen:
            seen.add(rev.n)
            rev = self.revision(rev.reverts_to)
        return rev.n if rev is not None else None

    def lineage(self, n: int | None = None) -> list[Revision]:
        """The revisions whose instructions produced the image of revision
        `n` (default: the current one), oldest first. A revert contributes
        nothing of its own: its image came from the revision it reverts to,
        so the steps it undid are left out. This is the history to replay or
        to describe to Claude, as opposed to `chain`, which is the history
        to show."""
        rev = self.revision(self.current if n is None else n)
        out: list[Revision] = []
        seen: set[int] = set()
        while rev is not None and rev.n not in seen:
            seen.add(rev.n)
            if rev.reverts_to is not None:
                rev = self.revision(rev.reverts_to)
                continue
            out.append(rev)
            rev = self.revision(rev.parent)
        out.reverse()
        return out

    def instructions(self, n: int | None = None) -> list[str]:
        """The instructions that produced revision `n`'s image, oldest
        first (see `lineage`)."""
        return [r.instruction for r in self.lineage(n) if r.instruction]

    def step_of(self, n: int) -> int | None:
        """The position of revision `n` in the history (0 for the first
        result). This is the number the user sees as "step 5"."""
        for step, rev in enumerate(self.chain()):
            if rev.n == n:
                return step
        return None

    def revision_at_step(self, step: int) -> Revision | None:
        chain = self.chain()
        return chain[step] if 0 <= step < len(chain) else None

    def add_revert(self, target_n: int, file: str, sha256: str = "") -> Revision:
        """Appends a revision that is a copy of revision `target_n` (whose
        copy is stored at `file`) and makes it current."""
        if self.revision(target_n) is None:
            raise ValueError(f"photo {self.id} has no revision {target_n}")
        new = Revision(
            n=self.next_revision_number(), parent=self.current, file=file, sha256=sha256,
            origin="reverted", reverts_to=target_n,
        )
        self.revisions.append(new)
        self.current = new.n
        return new


@dataclass
class Library:
    photos: dict[str, Photo] = field(default_factory=dict)
    # Managed album name -> Immich album id.
    albums: dict[str, str] = field(default_factory=dict)

    def in_home(self, home: str, *, include_trashed: bool = False) -> list[Photo]:
        return [
            p for p in self.photos.values()
            if p.home == home and (include_trashed or not p.trashed)
        ]

    def live_photos(self) -> list[Photo]:
        return [p for p in self.photos.values() if p.live and not p.trashed]

    def trashed_photos(self) -> list[Photo]:
        return [p for p in self.photos.values() if p.trashed]

    def album_names(self) -> list[str]:
        return sorted(self.albums)


# ---- (de)serialization ------------------------------------------------------


def _build(cls, data: dict[str, Any]):
    """Builds a dataclass from a dict, ignoring keys it does not know and
    leaving out ones it lacks, so files written by an older or newer version
    still load."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _photo_from_dict(data: dict[str, Any]) -> Photo:
    photo = _build(Photo, {k: v for k, v in data.items() if k not in ("sources", "revisions")})
    photo.sources = [_build(Source, s) for s in data.get("sources", [])]
    photo.revisions = [_build(Revision, r) for r in data.get("revisions", [])]
    return photo


def library_from_dict(raw: dict[str, Any]) -> Library:
    return Library(
        photos={pid: _photo_from_dict(p) for pid, p in raw.get("photos", {}).items()},
        albums=dict(raw.get("albums", {})),
    )


def library_to_dict(library: Library) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "photos": {pid: asdict(p) for pid, p in library.photos.items()},
        "albums": library.albums,
    }


class LibraryStore:
    """Loads and saves a Library as one JSON file."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path

    @contextlib.contextmanager
    def _exclusive(self):
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        with self._lock:
            with open(self._path + ".lock", "a") as lock_fh:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def load(self) -> Library:
        """A fresh copy of the library. Changing it changes nothing on
        disk; use update() for that. Safe to call without any lock, because
        the file is replaced atomically."""
        if not os.path.exists(self._path):
            return Library()
        with open(self._path, "r", encoding="utf-8") as fh:
            return library_from_dict(json.load(fh))

    def update(self, fn: Callable[[Library], T]) -> T:
        """Loads the library, calls `fn(library)` to change it, and saves
        the result, all under one lock (across processes too). Returns what
        `fn` returns. If `fn` raises, nothing is saved. Keep `fn` short:
        it must not call Immich or Claude."""
        with self._exclusive():
            library = self.load()
            result = fn(library)
            self._save(library)
            return result

    def update_photo(self, photo_id: str, fn: Callable[[Photo], T]) -> T:
        """update() for one photo. Raises KeyError if there is no such photo."""
        def apply(library: Library) -> T:
            return fn(library.photos[photo_id])
        return self.update(apply)

    def _save(self, library: Library) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".library-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(library_to_dict(library), fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
