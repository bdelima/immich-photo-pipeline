import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import (
    INBOX, REVIEW, Library, LibraryStore, Photo, Revision, Source, library_from_dict, library_to_dict,
)


def rev(n, parent, instruction=None):
    return Revision(n=n, parent=parent, file=f"p/revisions/{n}.jpg", instruction=instruction)


def linear_photo(count=6):
    """Revisions 0..count-1, each made from the one before."""
    photo = Photo(id="p", current=count - 1)
    for n in range(count):
        photo.revisions.append(rev(n, None if n == 0 else n - 1, None if n == 0 else f"step {n}"))
    return photo


# ---- storage ---------------------------------------------------------------


def test_round_trip_keeps_everything(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    photo = Photo(
        id="a", kind="collage", media_type="image",
        sources=[Source(asset_id="s1", file="a/sources/0-x.jpg", name="x.jpg", owner_id="u1"), Source(asset_id="s2")],
        revisions=[Revision(n=0, parent=None, file="a/revisions/0.jpg", rules=["no dark mats"], sha256="abc"),
                   Revision(n=1, parent=0, file="a/revisions/1.jpg", instruction="swap", session_id="sess")],
        current=1, home="Holiday", queue="collage", live=True, immich_asset_id="im-1",
        legacy_notes=["old note"], imported=False,
    )
    store.update(lambda lib: (lib.photos.__setitem__("a", photo), lib.albums.__setitem__("Holiday", "alb-1")))
    loaded = store.load()
    assert loaded.albums == {"Holiday": "alb-1"}
    got = loaded.photos["a"]
    assert got == photo
    assert got.revisions[1].instruction == "swap" and got.sources[0].owner_id == "u1"


def test_load_missing_file_is_an_empty_library(tmp_path):
    lib = LibraryStore(str(tmp_path / "nope.json")).load()
    assert lib.photos == {} and lib.albums == {}


def test_load_ignores_unknown_keys_and_fills_missing_ones():
    raw = {
        "version": 99, "future": True,
        "photos": {"a": {"id": "a", "home": "Holiday", "something_new": 1,
                         "revisions": [{"n": 0, "parent": None, "file": "a/revisions/0.jpg", "extra": "x"}]}},
        "albums": {"Holiday": "alb"},
    }
    lib = library_from_dict(raw)
    photo = lib.photos["a"]
    assert photo.home == "Holiday" and photo.media_type == "image" and photo.status == "ready"
    assert photo.revisions[0].origin == "processed" and photo.live is False


def test_update_saves_and_returns_the_result(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    assert store.update(lambda lib: lib.photos.setdefault("a", Photo(id="a")).id) == "a"
    assert "a" in store.load().photos


def test_update_that_raises_saves_nothing(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.setdefault("a", Photo(id="a")))

    def boom(lib):
        lib.photos["a"].home = "changed"
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        store.update(boom)
    assert store.load().photos["a"].home == INBOX


def test_update_photo_changes_one_photo_and_missing_photo_raises(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.update(a=Photo(id="a"), b=Photo(id="b")))
    store.update_photo("a", lambda p: setattr(p, "live", True))
    lib = store.load()
    assert lib.photos["a"].live is True and lib.photos["b"].live is False
    with pytest.raises(KeyError):
        store.update_photo("zzz", lambda p: None)


def test_loaded_library_is_a_copy(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.update(a=Photo(id="a")))
    store.load().photos["a"].home = "changed"
    assert store.load().photos["a"].home == INBOX


def test_concurrent_updates_do_not_lose_each_other(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.update(a=Photo(id="a")))
    errors = []

    def work(i):
        try:
            store.update(lambda lib: lib.photos.update({f"p{i}": Photo(id=f"p{i}")}))
            store.update_photo("a", lambda p: p.legacy_notes.append(str(i)))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lib = store.load()
    assert len([p for p in lib.photos if p.startswith("p")]) == 25
    assert sorted(lib.photos["a"].legacy_notes, key=int) == [str(i) for i in range(25)]


def test_no_temp_files_left_behind(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update(lambda lib: lib.photos.update(a=Photo(id="a")))
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []
    json.loads((tmp_path / "library.json").read_text())


# ---- revisions and steps -----------------------------------------------------


def test_linear_history():
    photo = linear_photo(6)
    assert [r.n for r in photo.chain()] == [0, 1, 2, 3, 4, 5]
    assert photo.instructions() == ["step 1", "step 2", "step 3", "step 4", "step 5"]
    assert photo.instructions(2) == ["step 1", "step 2"]
    assert photo.step_of(3) == 3
    assert photo.revision_at_step(5).n == 5 and photo.revision_at_step(9) is None
    assert photo.next_revision_number() == 6


def test_a_revert_is_a_new_step_stacked_on_top():
    photo = linear_photo(6)
    new = photo.add_revert(2, "p/revisions/6.jpg", "sha")
    assert (new.n, new.parent, new.reverts_to, new.origin) == (6, 5, 2, "reverted")
    assert photo.current == 6
    assert [r.n for r in photo.chain()] == [0, 1, 2, 3, 4, 5, 6]   # nothing hidden
    assert photo.step_of(6) == 6 and photo.step_of(4) == 4          # step numbers never move
    assert photo.content_source(6) == 2


def test_instructions_skip_what_a_revert_undid():
    photo = linear_photo(6)
    photo.add_revert(2, "p/revisions/6.jpg")
    # The image is step 2's, so Claude is told about steps 1 and 2 only.
    assert photo.instructions() == ["step 1", "step 2"]
    assert [r.n for r in photo.lineage()] == [0, 1, 2]
    # The history before the revert is unchanged.
    assert photo.instructions(5) == ["step 1", "step 2", "step 3", "step 4", "step 5"]


def test_a_revision_made_after_a_revert_builds_on_the_reverted_image():
    photo = linear_photo(6)
    photo.add_revert(2, "p/revisions/6.jpg")
    new = rev(photo.next_revision_number(), photo.current, "different idea")
    photo.revisions.append(new)
    photo.current = new.n
    assert new.n == 7 and new.parent == 6
    assert photo.instructions() == ["step 1", "step 2", "different idea"]
    assert [r.n for r in photo.chain()] == list(range(8))


def test_undoing_a_revert_is_another_revert():
    photo = linear_photo(6)
    photo.add_revert(2, "p/revisions/6.jpg")
    photo.add_revert(5, "p/revisions/7.jpg")
    assert photo.current == 7 and photo.content_source(7) == 5
    assert photo.instructions() == ["step 1", "step 2", "step 3", "step 4", "step 5"]
    # A revert to a revert resolves to the image it ultimately is.
    photo.add_revert(6, "p/revisions/8.jpg")
    assert photo.content_source(8) == 2 and photo.instructions() == ["step 1", "step 2"]


def test_revert_to_an_unknown_revision_is_refused():
    photo = linear_photo(2)
    with pytest.raises(ValueError):
        photo.add_revert(9, "p/revisions/2.jpg")
    assert len(photo.revisions) == 2 and photo.current == 1


def test_reverts_survive_a_round_trip(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    photo = linear_photo(3)
    photo.add_revert(0, "p/revisions/3.jpg", "sha")
    store.update(lambda lib: lib.photos.__setitem__("p", photo))
    assert store.load().photos["p"] == photo


def test_a_reverts_loop_does_not_hang():
    photo = Photo(id="p", current=1, revisions=[
        Revision(n=0, parent=None, file="a", reverts_to=1), Revision(n=1, parent=0, file="b", reverts_to=0),
    ])
    assert photo.instructions() == []
    assert photo.content_source(1) == 1


def test_chain_of_a_photo_with_no_revisions_is_empty():
    photo = Photo(id="p")
    assert photo.chain() == [] and photo.current_revision() is None and photo.next_revision_number() == 0


def test_chain_survives_a_corrupt_parent_loop():
    photo = Photo(id="p", current=1, revisions=[rev(0, 1), rev(1, 0)])
    assert len(photo.chain()) == 2


# ---- queries ---------------------------------------------------------------


def test_queries_leave_out_trashed_photos():
    lib = Library(photos={
        "a": Photo(id="a", home="Holiday", live=True),
        "b": Photo(id="b", home="Holiday", live=True, trashed=True),
        "c": Photo(id="c", home=REVIEW),
    })
    assert [p.id for p in lib.in_home("Holiday")] == ["a"]
    assert [p.id for p in lib.in_home("Holiday", include_trashed=True)] == ["a", "b"]
    assert [p.id for p in lib.live_photos()] == ["a"]
    assert [p.id for p in lib.trashed_photos()] == ["b"]


def test_dict_round_trip_is_stable():
    lib = Library(photos={"a": linear_photo(3)}, albums={"X": "1"})
    assert library_to_dict(library_from_dict(library_to_dict(lib))) == library_to_dict(lib)
