import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.revisions import RevisionStore, RevisionStoreError


def make_file(tmp_path, name="in.jpg", data=b"image-bytes"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_save_revision_copies_the_file_and_returns_its_hash(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    src = make_file(tmp_path)
    rel, digest = store.save_revision("photo1", 0, src)
    assert rel == os.path.join("photo1", "revisions", "0.jpg")
    assert digest == hashlib.sha256(b"image-bytes").hexdigest()
    assert open(store.path(rel), "rb").read() == b"image-bytes"
    assert store.exists(rel)


def test_a_revision_is_never_overwritten(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    src = make_file(tmp_path)
    store.save_revision("photo1", 0, src)
    with pytest.raises(RevisionStoreError):
        store.save_revision("photo1", 0, make_file(tmp_path, "other.jpg", b"different"))
    assert open(store.path(os.path.join("photo1", "revisions", "0.jpg")), "rb").read() == b"image-bytes"


def test_the_extension_is_kept_and_an_odd_one_falls_back_to_jpg(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    png, _ = store.save_revision("p", 1, make_file(tmp_path, "x.PNG"))
    odd, _ = store.save_revision("p", 2, make_file(tmp_path, "x.toolongextension"))
    assert png.endswith("1.png") and odd.endswith("2.jpg")


def test_save_source_uses_a_safe_name(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    rel = store.save_source("p", 2, make_file(tmp_path), "../../my photo (1).jpg")
    assert rel == os.path.join("p", "sources", "2-my_photo_1_.jpg")
    assert os.path.commonpath([store.root, store.path(rel)]) == store.root


def test_paths_outside_the_store_are_rejected(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    for bad in ("../x", "/etc/passwd", "a/../../x", ""):
        with pytest.raises(RevisionStoreError):
            store.path(bad)
    assert store.exists("../x") is False and store.exists("") is False


def test_unsafe_photo_ids_are_rejected(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    src = make_file(tmp_path)
    for bad in ("", "../x", "a/b", "a b"):
        with pytest.raises(RevisionStoreError):
            store.save_revision(bad, 0, src)
        with pytest.raises(RevisionStoreError):
            store.purge(bad)


def test_purge_removes_only_that_photo(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    a, _ = store.save_revision("a", 0, make_file(tmp_path))
    b, _ = store.save_revision("b", 0, make_file(tmp_path))
    store.purge("a")
    assert not store.exists(a) and store.exists(b)
    store.purge("never-existed")  # harmless


def test_no_partial_files_are_left_if_the_source_is_missing(tmp_path):
    store = RevisionStore(str(tmp_path / "store"))
    with pytest.raises(OSError):
        store.save_revision("p", 0, str(tmp_path / "missing.jpg"))
    leftovers = [f for _, _, files in os.walk(store.root) for f in files]
    assert leftovers == []
