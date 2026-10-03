import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.immich_client import read_api_keys_file


def test_read_api_keys_file_missing_returns_empty(tmp_path):
    assert read_api_keys_file(str(tmp_path / "missing")) == []


def test_read_api_keys_file_skips_blank_lines_and_comments(tmp_path):
    path = tmp_path / "keys"
    path.write_text("key-one\n\n# a comment\n  \nkey-two\n")
    assert read_api_keys_file(str(path)) == ["key-one", "key-two"]


def test_read_api_keys_file_empty_path_returns_empty():
    assert read_api_keys_file("") == []
