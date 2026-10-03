import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.secrets import resolve_secret


def test_resolve_secret_prefers_file_over_env(tmp_path):
    path = tmp_path / "secret"
    path.write_text("  file-value  \n")
    assert resolve_secret(str(path), "env-value") == "file-value"


def test_resolve_secret_falls_back_to_env_when_file_missing():
    assert resolve_secret("/no/such/file", "env-value") == "env-value"


def test_resolve_secret_falls_back_to_env_when_file_empty(tmp_path):
    path = tmp_path / "secret"
    path.write_text("   \n")
    assert resolve_secret(str(path), "env-value") == "env-value"


def test_resolve_secret_none_when_neither_present(tmp_path):
    assert resolve_secret(str(tmp_path / "missing"), None) is None
    assert resolve_secret(str(tmp_path / "missing"), "") is None


def test_resolve_secret_no_file_path_falls_back_to_env():
    assert resolve_secret("", "env-value") == "env-value"
