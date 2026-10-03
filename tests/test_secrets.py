import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.secrets import parse_secrets_file, resolve_secret, resolve_secret_list


def test_parse_secrets_file_missing_returns_empty(tmp_path):
    assert parse_secrets_file(str(tmp_path / "missing")) == {}


def test_parse_secrets_file_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# comment\n\nIMMICH_API_KEY=abc123\n   \n# another\n")
    assert parse_secrets_file(str(path)) == {"IMMICH_API_KEY": ["abc123"]}


def test_parse_secrets_file_collects_repeated_key_in_order(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("IMMICH_EXTRA_API_KEY=key-one\nIMMICH_EXTRA_API_KEY=key-two\n")
    assert parse_secrets_file(str(path)) == {"IMMICH_EXTRA_API_KEY": ["key-one", "key-two"]}


def test_parse_secrets_file_ignores_lines_without_equals(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("not-a-valid-line\nIMMICH_API_KEY=abc\n")
    assert parse_secrets_file(str(path)) == {"IMMICH_API_KEY": ["abc"]}


def test_resolve_secret_prefers_file_over_env(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("IMMICH_API_KEY=file-value\n")
    assert resolve_secret(str(path), "IMMICH_API_KEY", "env-value") == "file-value"


def test_resolve_secret_falls_back_to_env_when_key_missing_from_file(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("SOME_OTHER_KEY=whatever\n")
    assert resolve_secret(str(path), "IMMICH_API_KEY", "env-value") == "env-value"


def test_resolve_secret_falls_back_to_env_when_file_missing(tmp_path):
    assert resolve_secret(str(tmp_path / "missing"), "IMMICH_API_KEY", "env-value") == "env-value"


def test_resolve_secret_none_when_neither_present(tmp_path):
    assert resolve_secret(str(tmp_path / "missing"), "IMMICH_API_KEY", None) is None
    assert resolve_secret(str(tmp_path / "missing"), "IMMICH_API_KEY", "") is None


def test_resolve_secret_list_returns_all_values_in_order(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text(
        "IMMICH_EXTRA_API_KEY=key-one\n"
        "IMMICH_API_KEY=ignored-by-this-lookup\n"
        "IMMICH_EXTRA_API_KEY=key-two\n"
    )
    assert resolve_secret_list(str(path), "IMMICH_EXTRA_API_KEY") == ["key-one", "key-two"]


def test_resolve_secret_list_empty_when_key_absent(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("IMMICH_API_KEY=abc\n")
    assert resolve_secret_list(str(path), "IMMICH_EXTRA_API_KEY") == []


def test_resolve_secret_list_empty_when_file_missing(tmp_path):
    assert resolve_secret_list(str(tmp_path / "missing"), "IMMICH_EXTRA_API_KEY") == []
