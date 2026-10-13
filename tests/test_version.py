import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import version
from app.library import LibraryStore
from app.webui.server import create_app


def client(tmp_path):
    store = LibraryStore(str(tmp_path / "library.json"))
    return create_app(None, None, store, None).test_client()


def test_the_version_comes_from_the_file_in_the_image(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("0.7.9\n")
    monkeypatch.setattr(version, "VERSION_FILE", str(f))
    monkeypatch.setenv("APP_VERSION", "0.7.3")      # a stale value kept from an older container
    assert client(tmp_path).get("/api/version").get_json() == {"version": "0.7.9"}


def test_without_the_file_the_env_var_is_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("APP_VERSION", " 0.7.3 ")
    assert client(tmp_path).get("/api/version").get_json() == {"version": "0.7.3"}


def test_a_plain_checkout_reports_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert client(tmp_path).get("/api/version").get_json() == {"version": "dev"}


def test_a_missing_file_falls_back(tmp_path):
    assert version.current(path=str(tmp_path / "none"), env={"APP_VERSION": "1.2.3"}) == "1.2.3"


def test_an_empty_or_junk_file_is_ignored(tmp_path):
    f = tmp_path / "V"
    for text in ("", "  \n", "x" * 100, "\n\n"):
        f.write_text(text)
        assert version.current(path=str(f), env={"APP_VERSION": "1.2.3"}) == "1.2.3"
        assert version.current(path=str(f), env={}) == "dev"


def test_only_the_first_line_counts(tmp_path):
    f = tmp_path / "V"
    f.write_text("0.8.0\nnotes\n")
    assert version.current(path=str(f), env={}) == "0.8.0"


def test_the_repos_own_file_is_what_a_checkout_reports():
    here = open(os.path.join(os.path.dirname(__file__), "..", "VERSION")).read().strip()
    assert version.VERSION_FILE.endswith("VERSION") and version.current(env={}) == here


def test_the_image_copies_the_file_the_app_reads():
    dockerfile = open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile")).read()
    assert "COPY VERSION VERSION" in dockerfile


def test_the_shared_script_shows_it_under_the_title(tmp_path):
    js = client(tmp_path).get("/static/app.js").data
    assert b"/api/version" in js and b'"ver"' in js
