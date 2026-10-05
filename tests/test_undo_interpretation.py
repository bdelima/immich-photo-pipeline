"""How the interpreter's reply for "undo" is read, and what it is told."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recipe_runner import CommentContext
from tests.test_recipe_runner import _interpret, _runner, _wrapped


def test_interpret_undo_steps(tmp_path):
    ctx = CommentContext(history=["swap them", "darker mat", "tighter gaps"])
    runner = _runner(tmp_path)
    assert _interpret(runner, {"intent": "undo"}, ctx).steps == 1
    assert _interpret(runner, {"intent": "undo", "steps": 2}, ctx).steps == 2
    assert _interpret(runner, {"intent": "undo", "steps": "all"}, ctx).steps == 3
    assert _interpret(runner, {"intent": "undo", "steps": 99}, ctx).steps == 3
    assert _interpret(runner, {"intent": "undo", "steps": 0}, ctx).steps == 1
    assert _interpret(runner, {"intent": "undo", "steps": "two"}, ctx).steps == 1
    assert _interpret(runner, {"intent": "undo"}, CommentContext()).intent == "undo"


def test_interpret_prompt_lists_the_adjustments_and_the_undo_intent(tmp_path):
    ctx = CommentContext(history=["swap them", "darker mat"])

    class FakeProc:
        returncode = 0
        stdout = _wrapped({"intent": "undo"})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        _runner(tmp_path).interpret_comment("undo", ctx)
    prompt = mock_run.call_args.args[0][2]
    assert "1) swap them 2) darker mat" in prompt and "- undo:" in prompt and "go back to the original" in prompt
    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        _runner(tmp_path).interpret_comment("undo", CommentContext())
    assert "No adjustments have been applied to this photo yet." in mock_run.call_args.args[0][2]
