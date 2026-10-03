import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import ENV_TOKEN_VAR, RecipeRunner, format_auth_instructions, resolve_oauth_token


def test_resolve_oauth_token_prefers_file_over_env(tmp_path, monkeypatch):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=file-token\n")
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token(str(secrets_file)) == "file-token"


def test_resolve_oauth_token_falls_back_to_env_when_file_missing(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token("/no/such/file") == "env-token"


def test_resolve_oauth_token_falls_back_to_env_when_key_absent_from_file(tmp_path, monkeypatch):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("IMMICH_API_KEY=something-else\n")
    monkeypatch.setenv(ENV_TOKEN_VAR, "env-token")
    assert resolve_oauth_token(str(secrets_file)) == "env-token"


def test_resolve_oauth_token_none_when_neither_present(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
    assert resolve_oauth_token(str(tmp_path / "missing")) is None


def test_format_auth_instructions_mentions_setup_token_and_the_configured_path():
    text = format_auth_instructions("/run/secrets/immich_secrets.env")
    assert "claude setup-token" in text
    assert "/run/secrets/immich_secrets.env" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "docker exec" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in text


def test_check_auth_fails_fast_with_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(tmp_path / "missing"))
    ok, err = runner.check_auth()
    assert ok is False
    assert "no" in err.lower()


def test_check_auth_ok_when_subprocess_succeeds(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

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
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    class FakeProc:
        returncode = 1
        stderr = "Invalid or expired token"

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        ok, err = runner.check_auth()
    assert ok is False
    assert "Invalid or expired token" in err


def test_classify_comment_intent_returns_delete_on_delete_verdict(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = '{"intent": "delete"}'
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        intent = runner.classify_comment_intent("delete this please")
    assert intent == "delete"
    # No --skill flag: this is a plain classification prompt, not a
    # photo-mat-recipe run.
    assert "--skill" not in mock_run.call_args.args[0]


def test_classify_comment_intent_returns_revise_on_revise_verdict(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = '{"intent": "revise"}'
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("make the mat darker")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_nonzero_exit(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "something went wrong"

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_unparseable_output(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_timeout(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", "/app/photo-mat-recipe", str(secrets_file))

    import subprocess as subprocess_module

    with patch("app.recipe_runner.subprocess.run", side_effect=subprocess_module.TimeoutExpired(cmd="claude", timeout=60)):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"
