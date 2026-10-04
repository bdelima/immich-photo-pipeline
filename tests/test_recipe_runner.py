import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import ENV_TOKEN_VAR, CommentContext, RecipeRunner, format_auth_instructions, resolve_oauth_token

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


def _runner(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    return RecipeRunner("claude", SKILL_PATH, str(secrets_file))


def _interpret(runner, result_obj, ctx=None, text="a comment"):
    class FakeProc:
        returncode = 0
        stdout = _wrapped(result_obj)
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        return runner.interpret_comment(text, ctx)


def _run_prompt(runner, method, *args, **kwargs):
    class FakeProc:
        returncode = 0
        stdout = _wrapped({"output_path": "/tmp/out.jpg"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        getattr(runner, method)(*args, **kwargs)
    return mock_run.call_args.args[0][2]


def test_run_single_without_rules_or_note_has_no_preferences_block_or_lesson_contract(tmp_path):
    prompt = _run_prompt(_runner(tmp_path), "run_single", "/tmp/in.jpg", "/tmp/out.jpg")
    assert "reviewer_preferences" not in prompt
    assert '"lesson"' not in prompt


def test_run_single_injects_rules_before_the_note_and_contract(tmp_path):
    prompt = _run_prompt(
        _runner(tmp_path), "run_single", "/tmp/in.jpg", "/tmp/out.jpg",
        note="darker mat", rules=["Prefer thin bevels.", "Never use pure white mats."],
    )
    block_at = prompt.index("<reviewer_preferences>")
    assert "- Prefer thin bevels." in prompt and "- Never use pure white mats." in prompt
    assert block_at < prompt.index("darker mat") < prompt.index("reply with ONLY this exact JSON")
    assert prompt.index("</reviewer_preferences>") < prompt.index("darker mat")


def test_run_collage_injects_rules_too(tmp_path):
    prompt = _run_prompt(
        _runner(tmp_path), "run_collage", ["/tmp/a.jpg", "/tmp/b.jpg"], "/tmp/out.jpg",
        rules=["Keep items balanced by size."],
    )
    assert "- Keep items balanced by size." in prompt


def test_lesson_contract_is_only_offered_on_a_revision(tmp_path):
    runner = _runner(tmp_path)
    first = _run_prompt(runner, "run_single", "/tmp/in.jpg", "/tmp/out.jpg")
    revision = _run_prompt(runner, "run_single", "/tmp/in.jpg", "/tmp/out.jpg", note="darker")
    assert '"lesson"' not in first
    assert '"lesson"' in revision and '"lesson_scope"' in revision


def test_run_single_parses_a_proposed_lesson(tmp_path):
    runner = _runner(tmp_path)

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"output_path": "/tmp/out.jpg", "lesson": " Use a thinner bevel on dark photos. ", "lesson_scope": "single"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
        result = runner.run_single("/tmp/in.jpg", "/tmp/out.jpg", note="bevel too heavy")
    assert (result.lesson, result.lesson_scope) == ("Use a thinner bevel on dark photos.", "single")


def test_run_single_lesson_defaults_when_absent_or_malformed(tmp_path):
    runner = _runner(tmp_path)
    for payload, expected in (
        ({"output_path": "/tmp/out.jpg"}, (None, "all")),
        ({"output_path": "/tmp/out.jpg", "lesson": "  "}, (None, "all")),
        ({"output_path": "/tmp/out.jpg", "lesson": 3}, (None, "all")),
        ({"output_path": "/tmp/out.jpg", "lesson": "A rule.", "lesson_scope": "weird"}, ("A rule.", "all")),
    ):
        class FakeProc:
            returncode = 0
            stdout = _wrapped(payload)
            stderr = ""

        with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()):
            result = runner.run_single("/tmp/in.jpg", "/tmp/out.jpg", note="x")
        assert (result.lesson, result.lesson_scope) == expected


# ---- interpret_comment ----------------------------------------------------


def _bad_proc(returncode=0, stdout="not json at all"):
    class FakeProc:
        pass
    FakeProc.returncode, FakeProc.stdout, FakeProc.stderr = returncode, stdout, "boom"
    return FakeProc()


def test_interpret_returns_none_when_the_call_fails_or_is_unreadable(tmp_path):
    runner = _runner(tmp_path)
    import subprocess as sp
    for proc in (_bad_proc(1, ""), _bad_proc(0, "not json at all"), _bad_proc(0, json.dumps({"result": "nope"}))):
        with patch("app.recipe_runner.subprocess.run", return_value=proc):
            assert runner.interpret_comment("anything") is None
    with patch("app.recipe_runner.subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=60)):
        assert runner.interpret_comment("anything") is None


def test_interpret_delete_none_and_unknown(tmp_path):
    assert _interpret(_runner(tmp_path), {"intent": "delete"}).intent == "delete"
    assert _interpret(_runner(tmp_path), {"intent": "none"}).intent == "none"
    assert _interpret(_runner(tmp_path), {"intent": "something-new"}).intent == "revise"


def test_interpret_teach_returns_rule_and_scope(tmp_path):
    verdict = _interpret(_runner(tmp_path), {"intent": "teach", "rule": " Keep collage items balanced by size. ", "scope": "collage"})
    assert (verdict.intent, verdict.rule, verdict.scope) == ("teach", "Keep collage items balanced by size.", "collage")
    verdict = _interpret(_runner(tmp_path), {"intent": "teach", "rule": "Prefer thin bevels.", "scope": "portraits"})
    assert verdict.scope == "all"


def test_interpret_teach_without_a_rule_is_treated_as_revise(tmp_path):
    for payload in ({"intent": "teach"}, {"intent": "teach", "rule": "   "}, {"intent": "teach", "rule": 5}):
        assert _interpret(_runner(tmp_path), payload).intent == "revise"


def test_interpret_album_uses_the_existing_albums_exact_name(tmp_path):
    ctx = CommentContext(awaiting="album", albums=["Holiday", "Everyday"])
    verdict = _interpret(_runner(tmp_path), {"intent": "album", "album": "  'holiday' "}, ctx)
    assert (verdict.intent, verdict.album) == ("album", "Holiday")
    assert _interpret(_runner(tmp_path), {"intent": "album", "album": "Brand New"}, ctx).album == "Brand New"


def test_interpret_album_without_a_usable_name_asks_back(tmp_path):
    for payload in ({"intent": "album"}, {"intent": "album", "album": "x" * 200}, {"intent": "album", "album": "two\nlines"}):
        verdict = _interpret(_runner(tmp_path), payload)
        assert verdict.intent == "unclear" and "album" in verdict.question


def test_interpret_answer_only_counts_when_a_question_is_open(tmp_path):
    asked = CommentContext(awaiting="clarification", request="recenter", question="On what?")
    assert _interpret(_runner(tmp_path), {"intent": "answer"}, asked).intent == "answer"
    assert _interpret(_runner(tmp_path), {"intent": "answer"}, CommentContext()).intent == "revise"


def test_interpret_forget_needs_a_known_rule(tmp_path):
    ctx = CommentContext(rules=[("r3", "Prefer thin bevels.")])
    assert _interpret(_runner(tmp_path), {"intent": "forget", "rule_id": "R3"}, ctx).rule_id == "r3"
    verdict = _interpret(_runner(tmp_path), {"intent": "forget", "rule_id": "r9"}, ctx)
    assert verdict.intent == "unclear" and "forget rN" in verdict.question


def test_interpret_yes_no_need_a_proposal(tmp_path):
    ctx = CommentContext(proposal=("r1", "Use thin bevels."))
    assert _interpret(_runner(tmp_path), {"intent": "yes"}, ctx).intent == "yes"
    assert _interpret(_runner(tmp_path), {"intent": "no"}, ctx).intent == "no"
    assert _interpret(_runner(tmp_path), {"intent": "yes"}, CommentContext()).intent == "none"


def test_interpret_unclear_carries_its_question_or_a_default(tmp_path):
    verdict = _interpret(_runner(tmp_path), {"intent": "unclear", "question": "Album or crop change?"})
    assert (verdict.intent, verdict.question) == ("unclear", "Album or crop change?")
    assert _interpret(_runner(tmp_path), {"intent": "unclear"}).question


def test_interpret_prompt_carries_the_context(tmp_path):
    ctx = CommentContext(
        where="Holiday", awaiting="clarification", request="recenter this", question="On the dog or the house?",
        albums=["Holiday", "Everyday"], rules=[("r2", "Prefer thin bevels.")], proposal=("r5", "Keep mats pale."),
    )

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"intent": "revise"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        _runner(tmp_path).interpret_comment("yes move its crop", ctx)
    cmd = mock_run.call_args.args[0]
    assert not cmd[2].startswith("/photo-mat-recipe")  # a plain prompt, not a recipe run
    prompt = cmd[2]
    for needle in ("yes move its crop", "recenter this", "On the dog or the house?", "Holiday, Everyday",
                   "r2", "Prefer thin bevels.", "r5", "Keep mats pale.", "managed album 'Holiday'"):
        assert needle in prompt
    assert "Prefer unclear over guessing" in prompt and "Never choose album for something that reads as an edit" in prompt


def test_interpret_prompt_for_the_album_question(tmp_path):
    ctx = CommentContext(awaiting="album", albums=["Holiday"])

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"intent": "album", "album": "Holiday"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        _runner(tmp_path).interpret_comment("holiday please", ctx)
    prompt = mock_run.call_args.args[0][2]
    assert "Which album should this be promoted to?" in prompt and "Holiday" in prompt and "the Review queue" in prompt
