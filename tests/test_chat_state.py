"""The pieces slice E adds beneath the chat: how a photo's chat is stored,
and how a resumed recipe session is told where to write its answer."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.library import ChatMessage, LibraryStore, Photo
from app.recipe_runner import RecipeRunner

SKILL_PATH = "/app/.claude/skills/photo-mat-recipe"


def test_chat_and_pending_instruction_survive_a_save_and_load(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.json"))
    photo = Photo(id="a", pending_instruction="remove him")
    photo.chat = [ChatMessage(id=1, role="user", text="warmer", state="queued"),
                  ChatMessage(id=2, role="claude", text="Done.")]
    store.update(lambda lib: lib.photos.update({"a": photo}))
    loaded = store.load().photos["a"]
    assert loaded.pending_instruction == "remove him"
    assert [(m.id, m.role, m.text, m.state) for m in loaded.chat] == [
        (1, "user", "warmer", "queued"), (2, "claude", "Done.", "done")]
    assert loaded.busy and loaded.next_message_id() == 3
    loaded.chat[0].state = "done"
    assert not loaded.busy


def test_only_the_reviewers_unfinished_messages_make_a_photo_busy():
    photo = Photo(id="a")
    photo.chat = [ChatMessage(id=1, role="claude", text="hi", state="queued"),
                  ChatMessage(id=2, role="user", text="x", state="failed"),
                  ChatMessage(id=3, role="user", text="y", state="done")]
    assert not photo.busy and photo.open_messages() == []
    photo.chat.append(ChatMessage(id=4, role="user", text="z", state="working"))
    assert photo.busy and [m.id for m in photo.open_messages()] == [4]


def test_a_library_saved_before_chat_existed_still_loads(tmp_path):
    path = tmp_path / "lib.json"
    path.write_text(json.dumps({"version": 1, "photos": {"a": {"id": "a"}}, "albums": {}}))
    photo = LibraryStore(str(path)).load().photos["a"]
    assert photo.chat == [] and photo.pending_instruction is None and not photo.busy


def test_resume_tells_claude_where_to_write_when_given_a_path(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"session_id": "s", "result": json.dumps({"output_path": "/fresh/output.jpg"})})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        result = runner.resume("sid-abc", "the one on the left", output_path="/fresh/output.jpg")
    prompt = mock_run.call_args.args[0][2]
    assert "/fresh/output.jpg" in prompt and "the same output path as before" not in prompt
    assert "--resume" in mock_run.call_args.args[0] and result.output_path == "/fresh/output.jpg"


def test_resume_without_a_path_keeps_the_old_wording(tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=a-real-token\n")
    runner = RecipeRunner("claude", SKILL_PATH, str(secrets_file))

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"session_id": "s", "result": json.dumps({"output_path": "/x.jpg"})})
        stderr = ""

    with patch("app.recipe_runner.subprocess.run", return_value=FakeProc()) as mock_run:
        runner.resume("sid-abc", "crop tighter")
    assert "the same output path as before" in mock_run.call_args.args[0][2]
