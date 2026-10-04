"""Runs the photo-mat-recipe skill via headless Claude Code.

Verified live against a real Claude Pro session on 2026-10-04 (the
earlier revision of this module was written against the documented
non-interactive contract only, never run for real -- three of its
assumptions turned out wrong, all confirmed and fixed here):

1. There is no `--skill <path>` CLI flag. `claude --help` on the
   installed CLI (2.1.289) has no such option; passing it is a hard
   `error: unknown option '--skill'` on every invocation. Skills are
   slash commands instead, discovered from a `.claude/skills/<name>/`
   folder relative to the subprocess's working directory (confirmed:
   with the skill copied to `.claude/skills/photo-mat-recipe/` and the
   CLI launched with that directory as `cwd`, `claude -p
   "/photo-mat-recipe ..."` picks it up and actually follows the
   recipe's instructions; at the old bare `photo-mat-recipe/` path, the
   model replied "The `/photo-mat-recipe` command isn't installed in
   this session" and suggested adding it "as an organization plugin or
   a project skill" -- i.e. exactly the `.claude/skills/` layout used
   below).
2. `--output-format json`'s top-level JSON is Claude Code's own generic
   wrapper (`session_id`, `is_error`, `result`, ...) -- never the
   recipe-specific fields (`needs_clarification`, `output_path`,
   `question`) the old code expected to find at the top level. The
   model's actual answer is plain text inside `result`. Confirmed live:
   asking the CLI to "reply with ONLY this exact JSON and no other
   text: {...}" makes it put exactly that JSON string (nothing else)
   into `result` for a short, trivial prompt -- so the fix is to put
   the output contract in the prompt ourselves and parse `result` as a
   second, nested JSON document, not to expect the outer wrapper to
   carry it. Confirmed live AGAIN, the hard way, on the first real
   recipe run against an actual photo: compliance isn't perfect once a
   real multi-step task sits between the instruction and the reply --
   the model finished a real recipe run correctly but still prefaced
   its required JSON with a one-line summary ("Checked the result: the
   mat, bevel and subject all look right. Finishing up now.\n\n{...}"),
   despite the explicit "ONLY this exact JSON" instruction. `_invoke`
   therefore extracts the trailing `{...}` block from `result` rather
   than requiring the whole string to be pure JSON -- see
   `_extract_json_object`.
3. Headless `-p` mode has no human to answer a permission prompt, so
   any tool call needing one (confirmed: a plain `Bash` call) is
   silently denied (`permission_denials` in the JSON output) rather
   than asked about -- which would have blocked the recipe from ever
   actually running Python/Pillow/OpenCV against a real photo.
   `--permission-mode bypassPermissions` fixes this; confirmed live
   that a Bash call succeeds with it and is denied without it. Used on
   every invocation in this module, including the trivial classify/
   auth-check calls, since it's a no-op when nothing needs a tool and
   it's the one way headless mode can use a tool at all.

Auth: this container authenticates against a Claude Pro subscription via
a long-lived (~1 year) OAuth token from `claude setup-token`, not an API
key -- confirmed against the current Claude Code docs
(https://code.claude.com/docs/en/authentication, "Generate a long-lived
token"). That command opens the same browser-approval screen as `/login`
and prints the resulting token straight to the terminal; it does not
save it anywhere, so whoever runs it copies the token into a
CLAUDE_CODE_OAUTH_TOKEN= line in the shared secrets file
`resolve_oauth_token` reads (see app/secrets.py). It can be run from any
machine with a browser, including inside
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

Now confirmed end to end against a real photo: the first live recipe
run processed an actual image successfully (and is what surfaced the
preamble behavior in point 2). Not independently confirmed: behavior of
`--resume` for a clarification answer against a real session, which
rests on the documented contract only.

Reviewer-taught rules (app/rules.py) reach the recipe two ways, both as
prompt text rather than edits to the skill: active rules are appended to
every run as a delimited preferences block, and a revision run is also
told it may add an optional `lesson` (plus `lesson_scope`) to its JSON
reply, which the pipeline turns into a question to the reviewer rather than
saving anything itself.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass

from .rules import SCOPES, rules_prompt_block
from .secrets import resolve_secret

log = logging.getLogger(__name__)

ENV_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Headless `-p` mode has nobody to answer a tool-permission prompt, so
# anything needing one is silently denied instead (see module docstring,
# point 3) -- confirmed live that this flag is what lets a Bash call
# through. Applied to every invocation in this module; it's a no-op
# when nothing needs a tool.
_PERMISSION_MODE = "bypassPermissions"


def resolve_oauth_token(secrets_file: str) -> str | None:
    """A CLAUDE_CODE_OAUTH_TOKEN= line in the shared secrets file always
    wins over the env var, so rotating the token is "edit that one line",
    never "edit compose and restart". Thin wrapper over
    secrets.resolve_secret -- see that module for why this isn't
    duplicated per-credential."""
    return resolve_secret(secrets_file, ENV_TOKEN_VAR, os.environ.get(ENV_TOKEN_VAR))


def _skill_name(skill_path: str) -> str:
    """The slash command name Claude Code exposes a discovered skill
    under is just its folder's name (confirmed live: a skill folder
    named `photo-mat-recipe` is invoked as `/photo-mat-recipe`)."""
    return os.path.basename(os.path.normpath(skill_path))


def _skill_root(skill_path: str) -> str:
    """Claude Code discovers a skill from a `.claude/skills/<name>/`
    folder relative to the subprocess's cwd (confirmed live -- see
    module docstring, point 1), so this walks `skill_path` back up past
    `<name>/skills/.claude` to the project root the CLI needs to be
    launched from. If `skill_path` isn't actually shaped that way (a
    misconfigured RECIPE_SKILL_PATH), falls back to its parent dir --
    discovery will then fail with a clear "command isn't installed"
    result rather than this raising."""
    normalized = os.path.normpath(skill_path)
    parents = normalized.split(os.sep)
    if len(parents) >= 3 and parents[-2] == "skills" and parents[-3] == ".claude":
        return os.sep.join(parents[:-3]) or os.sep
    return os.path.dirname(normalized) or "."


def _extract_json_object(text: str) -> dict:
    """Parses the required JSON reply out of a model response that may
    not be PURELY that JSON, despite being told to reply with "ONLY"
    it -- confirmed live that compliance slips once a real task (not
    just a trivial one) sits between the instruction and the reply (see
    module docstring, point 2: a real recipe run prefaced its JSON with
    a one-line summary first). Tries the whole string first (the common
    case), then falls back to the span from the first `{` to the last
    `}` in it. Raises ValueError if neither works, which the caller turns into a
    clear RuntimeError rather than silently misparsing."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in reply")
    return json.loads(match.group(0))


def format_auth_instructions(secrets_file: str) -> str:
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
        "     it is NOT saved anywhere automatically. Add it as a line\n"
        "     in the shared secrets file this container reads (creating\n"
        "     the file if it doesn't exist yet):\n"
        "\n"
        f"         echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> {secrets_file}\n"
        "\n"
        f"  3. Make sure that file is bind-mounted into this container at\n"
        f"     {secrets_file} (see docker-compose.example.yml),\n"
        "     readable by uid 1000. Other keys already in that file\n"
        "     (IMMICH_API_KEY, IMMICH_EXTRA_API_KEY, ...) are left alone\n"
        "     -- only the CLAUDE_CODE_OAUTH_TOKEN= line matters here.\n"
        "\n"
        "This is rechecked periodically, so once the line is in place\n"
        "the pipeline recovers on its own -- no restart needed. Until\n"
        "then /healthz reports unhealthy and recipe runs are skipped.\n"
        "Tokens last about a year; regenerate and replace that same\n"
        "line when one expires.\n"
        "============================================================"
    )


@dataclass
class RecipeResult:
    status: str  # "done" | "needs_clarification"
    output_path: str | None = None
    question: str | None = None
    session_id: str | None = None
    # A general rule the recipe thinks is worth remembering, offered only
    # after a revision; the pipeline asks the reviewer before saving it.
    lesson: str | None = None
    lesson_scope: str = "all"


@dataclass
class CommentIntent:
    intent: str  # "delete" | "revise" | "teach"
    # Only set for "teach": the generalized rule and which runs it applies to.
    rule: str | None = None
    scope: str = "all"


class RecipeRunner:
    def __init__(self, claude_binary: str, skill_path: str, secrets_file: str):
        self._claude_binary = claude_binary
        self._skill_path = skill_path
        self._secrets_file = secrets_file
        self._skill_name = _skill_name(skill_path)
        self._skill_root = _skill_root(skill_path)

    def run_single(
        self, source_path: str, output_path: str, note: str | None = None,
        rules: list[str] | None = None,
    ) -> RecipeResult:
        prompt = f"Process {source_path} and write the finished image to {output_path}."
        return self._invoke(self._finish_prompt(prompt, output_path, note, rules))

    def run_collage(
        self, source_paths: list[str], output_path: str, note: str | None = None,
        rules: list[str] | None = None,
    ) -> RecipeResult:
        joined = ", ".join(source_paths)
        prompt = (
            f"Apply the multi-photo collage rules to these photos, in this "
            f"order: {joined}. Write the finished collage to {output_path}."
        )
        return self._invoke(self._finish_prompt(prompt, output_path, note, rules))

    @staticmethod
    def _finish_prompt(prompt: str, output_path: str, note: str | None, rules: list[str] | None) -> str:
        """Appends, in order: the standing reviewer preferences (if any),
        this run's reviewer note (if any), the reply contract, and -- only
        for a revision, since that's when there's feedback to learn from --
        the optional `lesson` contract."""
        prompt += rules_prompt_block(rules or [])
        if note:
            prompt += f" Additional instruction from the reviewer: {note}"
        prompt += " " + _OUTPUT_CONTRACT.format(output_path=output_path)
        if note:
            prompt += " " + _LESSON_CONTRACT
        return prompt

    def resume(self, session_id: str, answer: str) -> RecipeResult:
        prompt = answer + " " + _OUTPUT_CONTRACT.format(output_path="the same output path as before")
        return self._invoke(prompt, resume=session_id)

    def classify_comment(self, comment_text: str) -> CommentIntent:
        """Asks Claude what a reviewer's comment means: "delete" (remove the
        photo outright), "teach" (an explicit request that something apply
        to FUTURE photos too, which the pipeline saves as a rule), or
        "revise" (just change this image). This replaces a hand-written
        regex that could only catch a short, literal list of phrasings.
        No skill invocation: this is a plain classification prompt, not a
        photo-mat-recipe run.

        Defaults to "revise" on any failure, timeout, or unparseable
        response, and whenever "teach" comes back without a usable rule:
        misreading a comment as "revise" at worst produces a confused
        reply the reviewer can retry, while misreading a revision as
        "delete" would destroy the asset and as "teach" would change every
        future photo. "teach" is deliberately conservative -- the prompt
        requires an explicit cue like "always" or "from now on".

        The model's answer is JSON nested inside the CLI's own `result`
        wrapper field, not top-level -- see module docstring, point 2."""
        prompt = (
            "A reviewer left this comment on a photo sitting in a review "
            f"queue: {comment_text!r}\n\n"
            "Decide which ONE of these the comment is:\n"
            "- delete: asking to delete/remove/discard/trash this photo "
            "entirely.\n"
            "- teach: asking for something to apply to FUTURE photos as well "
            "as this one. Choose this ONLY when the reviewer explicitly says "
            "so, with a cue such as always, never, from now on, next time, "
            "going forward, in general, every time, or remember. A note "
            "about only this photo is not teach.\n"
            "- revise: anything else -- a note on how to change this image "
            "(cropping, color, composition, matting, ...).\n"
            "When in doubt, choose revise. "
            "Reply with ONLY one JSON object, no other text: "
            '{"intent": "delete"} or {"intent": "revise"} or '
            '{"intent": "teach", "rule": "<the rule as one general '
            'sentence>", "scope": "all"} where scope is "collage" if the '
            'rule only concerns multi-photo collages, "single" if it only '
            'concerns single-photo images, otherwise "all".'
        )
        token = resolve_oauth_token(self._secrets_file)
        env = os.environ.copy()
        if token:
            env[ENV_TOKEN_VAR] = token
        cmd = [self._claude_binary, "-p", prompt, "--output-format", "json",
               "--permission-mode", _PERMISSION_MODE]
        revise = CommentIntent("revise")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.exception("comment classification could not run; defaulting to revise")
            return revise
        if proc.returncode != 0:
            log.warning(
                "comment classification exited %s; defaulting to revise: %s",
                proc.returncode, (proc.stderr or "").strip()[:300],
            )
            return revise
        try:
            outer = json.loads(proc.stdout)
            payload = _extract_json_object(outer.get("result", ""))
            intent = payload.get("intent")
            if intent == "delete":
                return CommentIntent("delete")
            if intent == "teach":
                rule = payload.get("rule")
                if isinstance(rule, str) and rule.strip():
                    scope = payload.get("scope")
                    return CommentIntent("teach", rule=rule.strip(), scope=scope if scope in SCOPES else "all")
                log.warning("comment classified as teach but with no usable rule; treating as revise")
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            log.warning("comment classification returned unparseable output; defaulting to revise")
        return revise

    def check_auth(self) -> tuple[bool, str | None]:
        """A cheap, trivial invocation used purely to confirm a session can
        be established -- not a real recipe run. Only the exit code is
        checked, so this doesn't depend on the result-JSON-nesting
        details that affect the other methods."""
        token = resolve_oauth_token(self._secrets_file)
        if not token:
            return False, f"no {ENV_TOKEN_VAR}= line in {self._secrets_file}, and no {ENV_TOKEN_VAR} env var"
        env = os.environ.copy()
        env[ENV_TOKEN_VAR] = token
        cmd = [self._claude_binary, "-p", "Reply with OK.", "--output-format", "json",
               "--permission-mode", _PERMISSION_MODE]
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
        token = resolve_oauth_token(self._secrets_file)
        env = os.environ.copy()
        if token:
            env[ENV_TOKEN_VAR] = token
        if resume:
            full_prompt = prompt
        else:
            full_prompt = f"/{self._skill_name} {prompt}"
        cmd = [self._claude_binary, "-p", full_prompt, "--output-format", "json",
               "--permission-mode", _PERMISSION_MODE]
        if resume:
            cmd += ["--resume", resume]
        log.info("running recipe: %s", cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env, cwd=self._skill_root)
        if proc.returncode != 0:
            raise RuntimeError(f"claude invocation failed ({proc.returncode}): {proc.stderr[:1000]}")
        outer = json.loads(proc.stdout)
        session_id = outer.get("session_id")
        raw_result = outer.get("result", "")
        try:
            payload = _extract_json_object(raw_result)
        except (json.JSONDecodeError, ValueError, TypeError):
            raise RuntimeError(
                f"recipe did not reply with the required JSON contract: {raw_result[:500]!r}"
            )
        if payload.get("needs_clarification"):
            return RecipeResult(
                status="needs_clarification",
                question=payload.get("question"),
                session_id=session_id,
            )
        lesson = payload.get("lesson")
        lesson_scope = payload.get("lesson_scope")
        return RecipeResult(
            status="done",
            output_path=payload.get("output_path"),
            session_id=session_id,
            lesson=lesson.strip() if isinstance(lesson, str) and lesson.strip() else None,
            lesson_scope=lesson_scope if lesson_scope in SCOPES else "all",
        )


_OUTPUT_CONTRACT = (
    'When finished, reply with ONLY this exact JSON and no other text: '
    '{{"output_path": "{output_path}"}}. If you need more information '
    'before you can proceed, reply with ONLY this exact JSON and no '
    'other text instead: {{"needs_clarification": true, "question": '
    '"<your question>"}}'
)

# Appended to revision prompts only. No braces on purpose: this is joined
# onto text that has already been through str.format.
_LESSON_CONTRACT = (
    'If this feedback reveals a general rule that should apply to FUTURE '
    'photos too -- not just a fix for this one -- add two fields to that '
    'same JSON object: "lesson" (the rule, as one general sentence) and '
    '"lesson_scope" (one of "all", "single" or "collage"). Omit both when '
    'the feedback was specific to this photo.'
)
