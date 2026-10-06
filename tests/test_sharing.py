import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import ImmichClient, ImmichError
from app.pipeline import Pipeline
from app.sharing import ensure_all_shared, ensure_shared, resolve_user_ids
from app.state import ImageState, PipelineState
from types import SimpleNamespace


class FakeAccount:
    """One account's client: knows its own user id, and (for the owner)
    holds the albums."""

    def __init__(self, user_id, albums=None, fail_me=False, fail_share=False):
        self.user_id = user_id
        self.albums = albums if albums is not None else {}
        self.fail_me = fail_me
        self.fail_share = fail_share
        self.shared = []  # (album_id, user_ids, role)
        self.created = []
        self.activity_enabled = []

    def get_my_user_id(self):
        if self.fail_me:
            raise ImmichError("GET /users/me -> 401")
        return self.user_id

    def get_album(self, album_id):
        if album_id not in self.albums:
            raise ImmichError("GET /albums -> 404")
        return self.albums[album_id]

    def add_album_users(self, album_id, user_ids, role="editor"):
        if self.fail_share:
            raise ImmichError("PUT /albums/x/users -> 403")
        self.shared.append((album_id, list(user_ids), role))

    def enable_album_activity(self, album_id):
        self.activity_enabled.append(album_id)

    def list_albums(self):
        return [{"id": i, "albumName": a["albumName"]} for i, a in self.albums.items() if "albumName" in a]

    def create_album(self, name):
        self.created.append(name)
        album_id = f"new-{name}"
        self.albums[album_id] = {"id": album_id, "ownerId": self.user_id, "albumUsers": []}
        return album_id

    def add_assets_to_album(self, album_id, ids):
        pass

    def remove_assets_from_album(self, album_id, ids):
        pass


def album(owner="owner", users=()):
    return {"ownerId": owner, "albumUsers": [{"user": {"id": u}, "role": "editor"} for u in users]}


def test_resolve_user_ids_skips_owner_duplicates_and_bad_keys():
    owner = FakeAccount("owner")
    extras = [FakeAccount("wife"), FakeAccount("wife"), FakeAccount("owner"), FakeAccount("x", fail_me=True), FakeAccount("kid")]
    assert resolve_user_ids(owner, extras) == ["wife", "kid"]


def test_resolve_user_ids_returns_nothing_if_owner_lookup_fails():
    assert resolve_user_ids(FakeAccount("owner", fail_me=True), [FakeAccount("wife")]) == []


def test_ensure_shared_adds_only_missing_users_as_editors():
    owner = FakeAccount("owner", {"a1": album(users=["wife"])})
    assert ensure_shared(owner, "a1", ["wife", "kid"]) is True
    assert owner.shared == [("a1", ["kid"], "editor")]


def test_ensure_shared_is_a_no_op_when_everyone_already_has_access():
    owner = FakeAccount("owner", {"a1": album(users=["wife", "kid"])})
    assert ensure_shared(owner, "a1", ["wife", "kid"]) is True
    assert owner.shared == []


def test_ensure_shared_with_no_users_does_nothing():
    owner = FakeAccount("owner", {})
    assert ensure_shared(owner, "a1", []) is True


def test_ensure_shared_never_raises_when_the_album_is_not_ours():
    owner = FakeAccount("owner", {"a1": album(owner="someone-else")}, fail_share=True)
    assert ensure_shared(owner, "a1", ["wife"]) is False
    assert ensure_shared(owner, "missing", ["wife"]) is False


def test_ensure_all_shared_skips_blank_and_duplicate_ids():
    owner = FakeAccount("owner", {"a1": album(), "a2": album()})
    ensure_all_shared(owner, ["a1", "", "a1", "a2"], ["wife"])
    assert owner.shared == [("a1", ["wife"], "editor"), ("a2", ["wife"], "editor")]


def test_new_managed_album_is_shared_when_promoting():
    owner = FakeAccount("owner")
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=owner, recipe=None, store=None, share_user_ids=["wife"])
    state = PipelineState(images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="review")})
    pipeline._promote_to_album(state, "L", "a1", "Holiday")
    assert owner.shared == [("new-Holiday", ["wife"], "editor")]
    assert state.watched_albums["Holiday"] == "new-Holiday"


def test_promoting_into_an_existing_album_shares_it_too():
    # An album made by hand is adopted, and must end up shared like the
    # ones the pipeline creates (otherwise a thumbs-up on it vanishes).
    owner = FakeAccount("owner", {"h1": album(users=[])})
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=owner, recipe=None, store=None, share_user_ids=["wife"])
    state = PipelineState(
        images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="review")},
        watched_albums={"Holiday": "h1"},
    )
    assert pipeline._promote_to_album(state, "L", "a1", "Holiday") is True
    assert owner.shared == [("h1", ["wife"], "editor")] and owner.created == []


def test_an_album_already_shared_is_not_reshared_when_promoting():
    owner = FakeAccount("owner", {"h1": album(users=["wife"])})
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=owner, recipe=None, store=None, share_user_ids=["wife"])
    state = PipelineState(
        images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="review")},
        watched_albums={"Holiday": "h1"},
    )
    pipeline._promote_to_album(state, "L", "a1", "Holiday")
    assert owner.shared == []


def test_an_album_the_pipeline_cannot_share_is_reported_not_fatal():
    owner = FakeAccount("owner", {"h1": album(owner="someone-else")}, fail_share=True)
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=owner, recipe=None, store=None, share_user_ids=["wife"])
    state = PipelineState(
        images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="review")},
        watched_albums={"Holiday": "h1"},
    )
    assert pipeline._promote_to_album(state, "L", "a1", "Holiday") is False
    assert state.images["L"].home == "Holiday"


def test_ensure_shared_turns_on_activity_when_it_is_off():
    off = dict(album(users=["wife"]), isActivityEnabled=False)
    on = dict(album(users=["wife"]), isActivityEnabled=True)
    owner = FakeAccount("owner", {"a1": off, "a2": on, "a3": album(users=["wife"])})
    for album_id in ("a1", "a2", "a3"):
        assert ensure_shared(owner, album_id, ["wife"]) is True
    assert owner.activity_enabled == ["a1"]


def test_promoting_without_share_users_does_not_touch_sharing():
    owner = FakeAccount("owner")
    cfg = SimpleNamespace(review_album_id="review")
    pipeline = Pipeline(config=cfg, immich=owner, recipe=None, store=None)
    state = PipelineState(images={"L": ImageState(source_asset_ids=["s"], current_asset_id="a1", home="review")})
    pipeline._promote_to_album(state, "L", "a1", "Holiday")
    assert owner.shared == []


# ---- the real client's requests --------------------------------------------


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))

        class Resp:
            ok = True
            content = b'{"id": "u1"}'
            headers = {"content-type": "application/json"}

            def json(self_inner):
                return {"id": "u1"}

        return Resp()


def test_client_builds_the_share_and_user_requests():
    session = FakeSession()
    client = ImmichClient("http://immich", "key", session=session)
    assert client.get_my_user_id() == "u1"
    client.add_album_users("a1", ["u2", "u3"], "editor")
    client.add_album_users("a1", [])
    assert session.calls[0][:2] == ("GET", "http://immich/api/users/me")
    method, url, kwargs = session.calls[1]
    assert (method, url) == ("PUT", "http://immich/api/albums/a1/users")
    assert kwargs["json"] == {"albumUsers": [{"userId": "u2", "role": "editor"}, {"userId": "u3", "role": "editor"}]}
    assert len(session.calls) == 2  # the empty share made no request
    client.enable_album_activity("a1")
    method, url, kwargs = session.calls[-1]
    assert (method, url) == ("PATCH", "http://immich/api/albums/a1")
    assert kwargs["json"] == {"isActivityEnabled": True}
