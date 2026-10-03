"""Runs the photo-mat-recipe skill via headless Claude Code.

This is the one piece of the pipeline this PR could not exercise
end-to-end: it needs a real Claude Pro login inside the container and a
real Immich instance to download source photos from, neither of which
exist in a sandbox. The subprocess contract below is written to match
Claude Code's documented non-interactive mode (`claude -p`, `--resume
<session-id>`, `--output-format json`); it has not been run against the
real CLI. Treat this module as unverified until it's been exercised once
for real.

Auth works the same way: this container authenticates against a Claude
Pro subscription via a long-lived OAuth token (`claude setup-token`, run
once on a machine with a browser -- never inside this container), not an
API key. `resolve_oauth_token`/`check_auth` below are written from how
Claude Code documents that flow; the exact command and flags have not
been confirmed against a real token either.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

ENV_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def resolve_oauth_token(token_file: str) -> str | None:
    """The bind-mounted file always wins over the env var, so rotating a
    token is "overwrite the file", never "edit compose and restart"."""
    if token_file and os.path.isfile(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError as exc:
            log.warning("could not read %s: %s", token_file, exc)
            content = ""
        if content:
            return content
    env_token = os.environ.get(ENV_TOKEN_VAR, "").strip()
    return env_token or None


def format_auth_instructions(token_file: str) -> str:
    return (
        "============================================================\n"
        "immich-photo-pipeline: no working Claude Code session.\n"
        "------------------------------------------------------------\n"
        "This container authenticates against your Claude Pro\n"
        "subscription with a long-lived token, not an API key.\n"
        "\n"
        "  1. On a machine with a browser (NOT this container), with\n"
        "     the Claude Code CLI installed and logged into the Pro\n"
        "     account this pipeline should use, run:\n"
        "\n"
        "         claude setup-token\n"
        "\n"
        "  2. Save the token string it prints to a file, e.g.:\n"
        "\n"
        "         echo '<token>' > ./secrets/claude_oauth_token\n"
        "\n"
        f"  3. Bind-mount that file into this container at\n"
        f"     {token_file} (see docker-compose.example.yml),\n"
        "     readable by uid 1000.\n"
        "\n"
        "This is rechecked periodically, so once the file is in place\n"
        "the pipeline recovers on its own -- no restart needed. Until\n"
        "then /healthz reports unhealthy and recipe runs are skipped.\n"
        "============================================================"
    )


@dataclass
class RecipeResult:
    status: str  # "done" | "needs_clarification"
    output_path: str | None = None
    question: str | None = None
    session_id: str | None = None


class RecipeRunner:
    def __init__(self, claude_binary: str, skill_path: str, token_file: str):
        self._claude_binary = claude_binary
        self._skill_path = skill_path
        self._token_file = token_file

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

    def check_auth(self) -> tuple[bool, str | None]:
        """A cheap, trivial invocation used purely to confirm a session can
        be established -- not a real recipe run. NOT VERIFIED against the
        real CLI: the exact flags/output on a bad token are assumed, not
        confirmed, so the failure message quality may need adjusting once
        this runs for real."""
        token = resolve_oauth_token(self._token_file)
        if not token:
            return False, f"no token in {self._token_file} or {ENV_TOKEN_VAR}"
        env = os.environ.copy()
        env[ENV_TOKEN_VAR] = token
        cmd = [self._claude_binary, "-p", "Reply with OK.", "--output-format", "json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        except FileNotFoundError:
            return False, f"{self._claude_binary} not found on PATH"
        except subprocess.TimeoutExpired:
            return False, "claude invocation timed out"
        if proc.returncode != 0:
            return False, (proc.stderr or "").strip()[:500] or f"exit code {proc.returncode}"
        return True, None

    def _invoke(self, prompt: str, resume: str | None = None) -> RecipeResult:
        token = resolve_oauth_token(self._token_file)
        env = os.environ.copy()
        if token:
            env[ENV_TOKEN_VAR] = token
        cmd = [self._claude_binary, "-p", prompt, "--output-format", "json"]
        if resume:
            cmd += ["--resume", resume]
        else:
            cmd += ["--skill", self._skill_path]
        log.info("running recipe: %s", cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
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
