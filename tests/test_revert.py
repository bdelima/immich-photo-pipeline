import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import LibraryStore, Photo, Revision
from app.revert import RevertConflict, RevertError, revert_to
from app.revisions import RevisionStore


def build(tmp_path, steps=4):
    store = LibraryStore(str(tmp_path / "library.json"))
    revisions = RevisionStore(str(tmp_path / "revisions"))
    photo = Photo(id="p")
    for n in range(steps):
        src = tmp_path / f"src{n}.jpg"
        src.write_bytes(f"image {n}".encode())
        rel, digest = revisions.save_revision("p", n, str(src))
        photo.revisions.append(Revision(n=n, parent=n - 1 if n else None, file=rel, sha256=digest,
                                        instruction=f"step {n}" if n else None))
    photo.current = steps - 1
    store.update(lambda lib: lib.photos.__setitem__("p", photo))
    return store, revisions


def read(revisions, store, n):
    return open(revisions.path(store.load().photos["p"].revision(n).file), "rb").read()


def test_revert_stacks_a_copy_on_top(tmp_path):
    store, revisions = build(tmp_path)
    new = revert_to(store, revisions, "p", 1)
    photo = store.load().photos["p"]
    assert new.n == 4 and photo.current == 4 and len(photo.revisions) == 5
    assert read(revisions, store, 4) == b"image 1"
    assert read(revisions, store, 3) == b"image 3"          # the steps it skipped are still there
    assert new.sha256 == photo.revision(1).sha256
    assert photo.instructions() == ["step 1"]


def test_reverting_to_what_is_already_shown_does_nothing(tmp_path):
    store, revisions = build(tmp_path)
    assert revert_to(store, revisions, "p", 3) is None
    revert_to(store, revisions, "p", 1)
    assert revert_to(store, revisions, "p", 1) is None      # same image as the current revert
    assert len(store.load().photos["p"].revisions) == 5


def test_undoing_a_revert(tmp_path):
    store, revisions = build(tmp_path)
    revert_to(store, revisions, "p", 0)
    revert_to(store, revisions, "p", 3)
    photo = store.load().photos["p"]
    assert photo.current == 5 and read(revisions, store, 5) == b"image 3"
    assert photo.instructions() == ["step 1", "step 2", "step 3"]


def test_unknown_photo_or_step(tmp_path):
    store, revisions = build(tmp_path)
    with pytest.raises(RevertError):
        revert_to(store, revisions, "nope", 0)
    with pytest.raises(RevertError):
        revert_to(store, revisions, "p", 99)
    assert len(store.load().photos["p"].revisions) == 4


def test_a_conflict_leaves_no_stray_copy(tmp_path):
    store, revisions = build(tmp_path)
    real_save = revisions.save_revision

    def racing_save(photo_id, n, src_path):
        out = real_save(photo_id, n, src_path)
        # Another change lands while the copy is being made.
        store.update_photo("p", lambda p: p.revisions.append(Revision(n=n, parent=3, file="x")))
        return out

    revisions.save_revision = racing_save
    with pytest.raises(RevertConflict):
        revert_to(store, revisions, "p", 1)
    assert not revisions.exists(os.path.join("p", "revisions", "4.jpg"))
    assert store.load().photos["p"].current == 3
