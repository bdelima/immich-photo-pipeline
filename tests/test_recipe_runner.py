import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import ENV_TOKEN_VAR, RecipeRunner, format_auth_instructions, resolve_oauth_token

SKILL_PATH = "/app/.claude/skills/photo-mat-recipe"


def _wrapped(result_obj, session_id="sid-123"):
    """Mimics the real CLI's --output-format json wrapper: the model's
    actual answer is a JSON string nested inside the `result` field, not
    at the top level (confirmed live -- see recipe_runner.py's module
    docstring, point 2)."""
    return json.dumps({"session_id": session_id, "result": json.dumps(result_obj)})


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


def test_skill_name_and_root_derived_from_skill_path(tmp_path):
    runner = RecipeRunner("claude", SKILL_PATH, str(tmp_path / "missing"))
    assert runner._skill_name == "photo-mat-recipe"
    assert runner._skill_root == "/app"


def test_check_auth_fails_fast_with_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
    runner = RecipeRunner("claude", SKILL_PATH, str(tmp_path / "missing"))
    ok, err = runner.check_auth()
    assert ok is False
    assert "no" in err.lower()


def test_check_auth_ok_when_subprocess_succeeds(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        ok, err = runner.check_auth()
    assert ok is True
    assert err is None
    passed_env = mock_run.call_args.kwargs["env"]
    assert passed_env[ENV_TOKEN_VAR] == "a-real-token"
    # Headless mode has nobody to answer a tool-permission prompt, so
    # this must always be set (confirmed live: a Bash call is silently
    # denied without it -- see module docstring, point 3).
    assert "--permission-mode" in mock_run.call_args.args[0]
    assert "bypassPermissions" in mock_run.call_args.args[0]


def test_check_auth_fails_when_subprocess_exits_nonzero(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

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
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"intent": "delete"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        intent = runner.classify_comment_intent("delete this please")
    assert intent == "delete"
    # No skill invocation: this is a plain classification prompt, not a
    # photo-mat-recipe run, so the prompt isn't slash-prefixed.
    cmd = mock_run.call_args.args[0]
    assert not cmd[2].startswith("/photo-mat-recipe")


def test_classify_comment_intent_returns_revise_on_revise_verdict(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"intent": "revise"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("make the mat darker")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_nonzero_exit(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "something went wrong"

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_unparseable_outer(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_unparseable_nested_result(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"session_id": "sid", "result": "not json either"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_classify_comment_intent_defaults_to_revise_on_timeout(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    import subprocess as subprocess_module

    with patch("app.recipe_runner.subprocess.run", side_effect=subprocess_module.TimeoutExpired(cmd="claude", timeout=60)):
        intent = runner.classify_comment_intent("delete this")
    assert intent == "revise"


def test_run_single_invokes_as_slash_command_with_no_skill_flag(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"output_path": "/tmp/out.jpg"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        runner.run_single("/tmp/in.jpg", "/tmp/out.jpg")
    cmd = mock_run.call_args.args[0]
    assert "--skill" not in cmd
    assert cmd[2].startswith("/photo-mat-recipe ")
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    # Discovery needs the subprocess's cwd to be the project root the
    # skill's `.claude/skills/<name>/` folder lives under, not /app's
    # skill subfolder itself.
    assert mock_run.call_args.kwargs["cwd"] == "/app"


def test_run_single_parses_done_status_from_nested_result(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"output_path": "/tmp/out.jpg"}, session_id="sid-abc")
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        result = runner.run_single("/tmp/in.jpg", "/tmp/out.jpg")
    assert result.status == "done"
    assert result.output_path == "/tmp/out.jpg"
    assert result.session_id == "sid-abc"


def test_run_single_parses_needs_clarification_from_nested_result(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"needs_clarification": True, "question": "Crop tighter?"}, session_id="sid-xyz")
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        result = runner.run_single("/tmp/in.jpg", "/tmp/out.jpg")
    assert result.status == "needs_clarification"
    assert result.question == "Crop tighter?"
    assert result.session_id == "sid-xyz"


def test_run_single_tolerates_a_preamble_before_the_json(tmp_path):
    """Reproduces the real failure seen on the first live photo run:
    the model finished a genuine recipe run correctly but still
    prefaced its required JSON with a one-line summary, despite being
    told to reply with ONLY the JSON (see module docstring, point 2)."""
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    preamble_reply = (
        "Checked the result: the mat, bevel and subject all look right. "
        'Finishing up now.\n\n{"output_path": "/tmp/out.jpg"}'
    )

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"session_id": "sid-real", "result": preamble_reply})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        result = runner.run_single("/tmp/in.jpg", "/tmp/out.jpg")
    assert result.status == "done"
    assert result.output_path == "/tmp/out.jpg"


def test_run_single_raises_when_result_is_not_the_required_json(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"session_id": "sid", "result": "Sure, I'll get right on that!"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        try:
            runner.run_single("/tmp/in.jpg", "/tmp/out.jpg")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "JSON contract" in str(exc)


def test_resume_does_not_slash_prefix_the_prompt(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"output_path": "/tmp/out.jpg"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        runner.resume("sid-abc", "crop tighter")
    cmd = mock_run.call_args.args[0]
    # A resumed session already has the skill loaded as context; the
    # answer text is plain, not slash-prefixed again.
    assert not cmd[2].startswith("/photo-mat-recipe")
    assert "crop tighter" in cmd[2]
    assert "--resume" in cmd and "sid-abc" in cmd
