import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import ENV_TOKEN_VAR, RecipeRunner, format_auth_instructions, resolve_oauth_token


def test_resolve_oauth_token_prefers_file_over_env(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("  file-token  \n")
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token(str(token_file)) == "file-token"


def test_resolve_oauth_token_falls_back_to_env_when_file_missing(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token("/no/such/file") == "env-token"


def test_resolve_oauth_token_falls_back_to_env_when_file_empty(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("   \n")
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token(str(token_file)) == "env-token"


def test_resolve_oauth_token_none_when_neither_present(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
    assert resolve_oauth_token(str(tmp_path / "missing")) is None


def test_format_auth_instructions_mentions_setup_token_and_the_configured_path():
    text = format_auth_instructions("/run/secrets/claude_oauth_token")
    assert "claude setup-token" in text
    assert "/run/secrets/claude_oauth_token" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "docker exec" in text


def test_check_auth_fails_fast_with_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(tmp_path / "missing"))
    ok, err = runner.check_auth()
    assert ok is False
    assert "no token" in err


def test_check_auth_ok_when_subprocess_succeeds(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("a-real-token")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(token_file))

    class FakeProc:
        returncode = 0
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        ok, err = runner.check_auth()
    assert ok is True
    assert err is None
    passed_env = mock_run.call_args.kwargs["env"]
    assert passed_env[ENV_TOKEN_VAR] == "a-real-token"


def test_check_auth_fails_when_subprocess_exits_nonzero(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("a-real-token")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(token_file))

    class FakeProc:
        returncode = 1
        stderr = "Invalid or expired token"

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        ok, err = runner.check_auth()
    assert ok is False
    assert "Invalid or expired token" in err
