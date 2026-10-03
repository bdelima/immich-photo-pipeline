"""Runs the photo-mat-recipe skill via headless Claude Code.

This is the one piece of the pipeline this PR could not exercise
end-to-end: it needs a real Claude Pro login inside the container and a
real Immich instance to download source photos from, neither of which
exist in a sandbox. The subprocess contract below is written to match
Claude Code's documented non-interactive mode (`claude -p`, `--resume
<session-id>`, `--output-format json`); it has not been run against the
real CLI. Treat this module as unverified until it's been exercised once
for real.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RecipeResult:
    status: str  # "done" | "needs_clarification"
    output_path: str | None = None
    question: str | None = None
    session_id: str | None = None


class RecipeRunner:
    def __init__(self, claude_binary: str, skill_path: str):
        self._claude_binary = claude_binary
        self._skill_path = skill_path

    def run_single(self, source_path: str, output_path: str, note: str | None = None) -> RecipeResult:
        prompt = (
            f"Apply the photo-mat-recipe skill to {source_path}. "
            f"Write the finished image to {output_path}."
        )
        if note:
            prompt += f" Additional instruction from the reviewer: {note}"
        return self._invoke(prompt)

    def run_collage(self, source_paths: list[str], output_path: str, note: str | None = None) -> RecipeResult:
        joined = ", ".join(source_paths)
        prompt = (
            f"Apply the photo-mat-recipe skill's multi-photo collage rules to "
            f"these photos, in this order: {joined}. Write the finished "
            f"collage to {output_path}."
        )
        if note:
            prompt += f" Additional instruction from the reviewer: {note}"
        return self._invoke(prompt)

    def resume(self, session_id: str, answer: str) -> RecipeResult:
        return self._invoke(answer, resume=session_id)

    def _invoke(self, prompt: str, resume: str | None = None) -> RecipeResult:
        cmd = [self._claude_binary, "-p", prompt, "--output-format", "json"]
        if resume:
            cmd += ["--resume", resume]
        else:
            cmd += ["--skill", self._skill_path]
        log.info("running recipe: %s", cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"claude invocation failed ({proc.returncode}): {proc.stderr[:1000]}")
        payload = json.loads(proc.stdout)
        if payload.get("needs_clarification"):
            return RecipeResult(
                status="needs_clarification",
                question=payload.get("question"),
                session_id=payload.get("session_id"),
            )
        return RecipeResult(
            status="done",
            output_path=payload.get("output_path"),
            session_id=payload.get("session_id"),
        )
