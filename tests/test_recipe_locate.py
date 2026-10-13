import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.recipe_runner import RecipeRunner


def _wrapped(result_obj):
    return json.dumps({"session_id": "sid", "result": json.dumps(result_obj)})


def _runner(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    return RecipeRunner("claude", "/app/.claude/skills/photo-mat-recipe", str(secrets_file))


def _locate(tmp_path, reply, lat=9.43, lon=-84.16):
    class Proc:
        returncode = 0
        stderr = ""
        stdout = _wrapped(reply)

    with patch("app.recipe_runner.subprocess.run", return_value=Proc()) as mock_run:
        out = _runner(tmp_path).locate(lat, lon)
    return out, mock_run.call_args[0][0]


def test_locate_asks_claude_for_the_place_at_the_coordinates(tmp_path):
    place, cmd = _locate(tmp_path, {"place": " Quepos, Puntarenas, Costa Rica "})
    assert place == "Quepos, Puntarenas, Costa Rica" and not cmd[2].startswith("/")
    assert "latitude 9.43, longitude -84.16" in cmd[2] and "City, Region, Country" in cmd[2]


def test_locate_raises_with_a_reason_when_claude_cannot_say(tmp_path):
    import subprocess as sp

    class Bad:
        returncode, stderr, stdout = 1, "boom", ""

    class Blank:
        returncode, stderr, stdout = 0, "", _wrapped({"place": "  "})

    for proc, fragment in ((Bad, "failed"), (Blank, "did not name a place")):
        with patch("app.recipe_runner.subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match=fragment):
                _runner(tmp_path).locate(1.0, 2.0)
    with patch("app.recipe_runner.subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=120)):
        with pytest.raises(RuntimeError, match="too long"):
            _runner(tmp_path).locate(1.0, 2.0)
