"""Runs the photo-mat-recipe skill via headless Claude Code.

This is the one piece of the pipeline this PR could not exercise
end-to-end: it needs a real Claude Pro login inside the container and a
real Immich instance to download source photos from, neither of which
exist in a sandbox. The subprocess contract below is written to match
Claude Code's documented non-interactive mode (`claude -p`, `--resume
<session-id>`, `--output-format json`); it has not been run against the
real CLI. Treat this module as unverified until it's been exercised once
for real.

Auth: this container authenticates against a Claude Pro subscription via
a long-lived (~1 year) OAuth token from `claude setup-token`, not an API
key -- confirmed against the current Claude Code docs
(https://code.claude.com/docs/en/authentication, "Generate a long-lived
token"). That command opens the same browser-approval screen as `/login`
and prints the resulting token straight to the terminal; it does not
save it anywhere, so whoever runs it copies the token into
CLAUDE_CODE_OAUTH_TOKEN (here: the bind-mounted file `resolve_oauth_token`
reads). It can be run from any machine with a browser, including inside
this container's own shell via `docker exec -it` -- the docs confirm
containers/SSH/WSL2 fall back to a short code you paste back into the
terminal when the browser can't redirect to a local callback port, so no
port-forwarding is needed either way. Whatever method is normally used to
sign into claude.ai (Google SSO, passkey, email+password) works
identically in that browser step; the CLI only cares that the browser
session completes.

One real gotcha from the docs: if ANTHROPIC_API_KEY is set anywhere in
this container's environment, Claude Code prefers it over the
subscription token and silently switches to metered API billing instead
of the Pro allocation. Never set that var here.

What's still unverified in this sandbox: the actual CLI invocation
shape below (`claude -p ...`) has not been run against a real token,
since no sandbox here can complete a browser login.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

from .secrets import resolve_secret

log = logging.getLogger(__name__)

ENV_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def resolve_oauth_token(token_file: str) -> str | None:
    """The bind-mounted file always wins over the env var, so rotating a
    token is "overwrite the file", never "edit compose and restart". Thin
    wrapper over secrets.resolve_secret -- see that module for why this
    isn't duplicated per-credential."""
    return resolve_secret(token_file, os.environ.get(ENV_TOKEN_VAR))


def format_auth_instructions(token_file: str) -> str:
    return (
        "============================================================\n"
        "immich-photo-pipeline: no working Claude Code session.\n"
        "------------------------------------------------------------\n"
        "This container authenticates against your Claude Pro\n"
        "subscription with a long-lived (~1 year) OAuth token, not an\n"
        "API key -- and setting ANTHROPIC_API_KEY anywhere in this\n"
        "container's environment would silently switch billing to\n"
        "metered API usage instead, so never set that var here.\n"
        "\n"
        "  1. Get a token. Run this from any machine with a browser,\n"
        "     OR from inside this container's own shell:\n"
        "\n"
        "         docker exec -it <this container> claude setup-token\n"
        "\n"
        "     This opens the same browser-approval screen as a normal\n"
        "     /login -- however you normally sign into claude.ai\n"
        "     (Google, passkey, email, whatever) works the same way\n"
        "     here. If the browser can't redirect back here (common\n"
        "     for containers/SSH/WSL2), it shows a short code instead:\n"
        "     open the approval URL on your phone or any other\n"
        "     device, approve it there, then paste the code back into\n"
        "     this terminal.\n"
        "\n"
        "  2. The command prints the token directly to the terminal --\n"
        "     it is NOT saved anywhere automatically. Copy it and save\n"
        "     it to a file, e.g.:\n"
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
        "Tokens last about a year; regenerate and overwrite the same\n"
        "file when one expires.\n"
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
