import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import LibraryStore
from app.webui.server import create_app


def client(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    return create_app(None, None, store, None).test_client()


def test_the_version_comes_from_the_image(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "0.7.3")
    assert client(tmp_path).get("/api/version").get_json() == {"version": "0.7.3"}


def test_a_plain_checkout_reports_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert client(tmp_path).get("/api/version").get_json() == {"version": "dev"}


def test_the_shared_script_shows_it_under_the_title(tmp_path):
    js = client(tmp_path).get("/static/app.js").data
    assert b"/api/version" in js and b'"ver"' in js
