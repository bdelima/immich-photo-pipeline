import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import actions
from app.library import Library, Photo
from app.slugs import album_for_slug, slug_map, slugify


def test_slugs_are_lowercase_words_joined_by_hyphens():
    assert slugify("Costa Rica") == "costa-rica"
    assert slugify("  Beach -- Days!  ") == "beach-days"
    assert slugify("São Paulo 2024") == "sao-paulo-2024"
    assert slugify("!!!") == "album"


def test_clashing_names_still_get_unique_addresses():
    assert slug_map(["Costa Rica", "Costa-Rica", "Beach"]) == {
        "Beach": "beach", "Costa Rica": "costa-rica", "Costa-Rica": "costa-rica-2"}


def test_a_key_finds_an_album_by_slug_or_by_exact_name():
    names = ["Costa Rica", "Beach"]
    assert album_for_slug(names, "costa-rica") == "Costa Rica"
    assert album_for_slug(names, "Costa Rica") == "Costa Rica"       # an old link
    assert album_for_slug(names, "nowhere") is None


def library_with(*names):
    lib = Library()
    for n in names:
        lib.albums[n] = "id-" + n
    return lib


def test_a_new_album_name_that_clashes_with_an_existing_slug_is_refused():
    lib = library_with("Costa Rica")
    with pytest.raises(actions.ActionError) as err:
        actions.album_name(lib, "Costa-Rica")
    assert "Costa Rica" in str(err.value)
    with pytest.raises(actions.ActionError):
        actions.album_name(lib, "costa rica!")


def test_the_same_name_in_another_case_still_means_the_existing_album():
    assert actions.album_name(library_with("Costa Rica"), "costa rica") == "Costa Rica"


def test_an_album_a_photo_was_just_moved_to_counts_as_existing():
    lib = Library()
    lib.photos["p"] = Photo(id="p", home="Costa Rica")
    with pytest.raises(actions.ActionError):
        actions.album_name(lib, "Costa-Rica")


def test_unrelated_names_are_fine():
    assert actions.album_name(library_with("Costa Rica"), "Beach") == "Beach"
