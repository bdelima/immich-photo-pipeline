"""Regression tests for the self-comment loop seen live: the pipeline's own
"Applied: ..." note was read back as a reviewer instruction, so one comment
caused endless revisions ("Applied: Applied: Applied: ..."), each deleting
the previous version of the photo."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import Asset, Comment, ImmichClient
from app.pipeline import Pipeline
from app.recipe_runner import RecipeResult
from app.state import ImageState, PipelineState
from tests.test_immich_client import RoutedSession


def test_comments_are_marked_own_by_author_id_not_a_flag_immich_does_not_send():
    # Real Immich activity users carry no "isOwner"; only the author's id.
    session = RoutedSession({
        "/activities": [
            {"id": "c1", "comment": "too pink", "user": {"id": "wife"}},
            {"id": "c2", "comment": "Applied: too pink", "user": {"id": "pipeline"}},
        ],
        "/users/me": {"id": "pipeline"},
    })
    client = ImmichClient("http://immich", "key", session=session)
    comments = client.list_comments(album_id="alb")
    assert [(c.id, c.is_own) for c in comments] == [("c1", False), ("c2", True)]


def test_own_user_id_is_looked_up_once():
    session = RoutedSession({"/activities": [{"id": "c", "comment": "x", "user": {"id": "u"}}], "/users/me": {"id": "me"}})
    client = ImmichClient("http://immich", "key", session=session)
    client.list_comments(album_id="a")
    client.list_comments(album_id="a")
    assert sum(1 for _, url, _ in session.calls if url.endswith("/users/me")) == 1


class LoopImmich:
    """A tiny Immich: comments live per asset, authored by whoever posted."""

    def __init__(self, own_detection_works=True):
        self.comments = {}  # asset_id -> [Comment]
        self.assets = {"old": Asset(id="old", original_file_name="x.jpg", is_favorite=False)}
        self.deleted = []
        self.uploads = 0
        self.own_detection_works = own_detection_works
        self._n = 0

    def _add(self, asset_id, text, user_id):
        self._n += 1
        cid = f"c{self._n}"
        is_own = (user_id == "pipeline") and self.own_detection_works
        self.comments.setdefault(asset_id, []).append(Comment(id=cid, text=text, user_id=user_id, is_own=is_own))
        return cid

    def list_album_assets(self, album_id):
        return list(self.assets.values())

    def list_comments(self, *, album_id, asset_id=None):
        return list(self.comments.get(asset_id, []))

    def post_comment(self, text, *, album_id, asset_id=None):
        return self._add(asset_id, text, "pipeline")

    def download_asset_original(self, asset_id, dest_dir):
        path = os.path.join(dest_dir, "src.jpg")
        open(path, "wb").write(b"x")
        return path

    def upload_asset(self, path, name):
        self.uploads += 1
        new_id = f"new{self.uploads}"
        self.assets[new_id] = Asset(id=new_id, original_file_name=name, is_favorite=False)
        return new_id

    def add_assets_to_album(self, album_id, ids):
        pass

    def set_favorite(self, asset_id, favorite):
        pass

    def delete_assets(self, ids, force=True):
        self.deleted.append((list(ids), force))
        for i in ids:
            self.assets.pop(i, None)


class CountingRecipe:
    def __init__(self):
        self.notes = []

    def classify_comment_intent(self, text):
        return "revise"

    def run_single(self, source, output, note=None):
        self.notes.append(note)
        return RecipeResult(status="done", output_path=output)


def run_review_cycles(immich, cycles):
    cfg = SimpleNamespace(review_album_id="review")
    recipe = CountingRecipe()
    pipeline = Pipeline(config=cfg, immich=immich, recipe=recipe, store=None)
    img = ImageState(source_asset_ids=["src"], current_asset_id="old", home="review")
    state = PipelineState(images={"L": img})
    immich._add("old", "tilt it down 3 degrees", "wife")
    for _ in range(cycles):
        pipeline._flow2_review(state)
    return recipe, immich, img


def test_one_comment_causes_exactly_one_revision():
    recipe, immich, img = run_review_cycles(LoopImmich(), cycles=6)
    assert recipe.notes == ["tilt it down 3 degrees"]
    assert img.current_asset_id == "new1"


def test_loop_is_prevented_even_if_author_detection_fails():
    # The pipeline also records the ids of its own posts as handled, so a
    # failure to recognize the author can't restart the loop.
    recipe, immich, img = run_review_cycles(LoopImmich(own_detection_works=False), cycles=6)
    assert recipe.notes == ["tilt it down 3 degrees"]


def test_previous_version_goes_to_the_trash_not_a_force_delete():
    recipe, immich, img = run_review_cycles(LoopImmich(), cycles=2)
    assert immich.deleted == [(["old"], False)]


def test_identical_upload_never_deletes_the_only_copy():
    # Immich returns the existing asset's id when identical bytes are
    # uploaded; the "new" asset is then the old one.
    immich = LoopImmich()
    immich.upload_asset = lambda path, name: "old"
    recipe, immich, img = run_review_cycles(immich, cycles=2)
    assert immich.deleted == []
    assert "old" in immich.assets
    assert img.current_asset_id == "old"
