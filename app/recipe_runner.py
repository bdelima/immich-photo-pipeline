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
from dataclasses import dataclass, field

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
    """What a reviewer's comment means (see RecipeRunner.interpret_comment).

    intent is one of: "revise" (change this photo), "answer" (replies to the
    recipe's own question about the photo), "album" (names the album to put
    it in), "delete", "teach" (a standing preference for future photos),
    "forget" (retire a saved rule), "yes" / "no" (answers a proposed rule),
    "undo" (take back the last adjustment(s) to this photo),
    "unclear" (ask the reviewer to clarify) or "none" (nothing to do, e.g.
    "thanks")."""
    intent: str
    # "teach": the generalized rule and which runs it applies to.
    rule: str | None = None
    scope: str = "all"
    # "album": the album's name, resolved against the existing albums.
    album: str | None = None
    # "forget": the id of the rule to retire.
    rule_id: str | None = None
    # "unclear": the short question to ask back.
    question: str | None = None
    # "undo": how many of the most recent adjustments to take back.
    steps: int = 1


@dataclass
class CommentContext:
    """What the interpreter needs to know besides the comment itself."""
    # "review", or the name of the managed album the photo is in.
    where: str = "review"
    # The question the pipeline is waiting on: "album" (which album should
    # this go to?), "clarification" (the recipe asked about the photo), or
    # None.
    awaiting: str | None = None
    # For "clarification": the reviewer's original request and the question.
    request: str | None = None
    question: str | None = None
    albums: list[str] = field(default_factory=list)
    # Active rules as (id, text), so "stop doing the thin-bevel thing" can
    # be matched to one.
    rules: list[tuple[str, str]] = field(default_factory=list)
    # A rule the pipeline proposed on this photo and is waiting on, as (id, text).
    proposal: tuple[str, str] | None = None
    # The adjustments already applied to this photo at the reviewer's
    # request, oldest first (what "undo" would take back).
    history: list[str] = field(default_factory=list)


MAX_ALBUM_NAME_CHARS = 60


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

    def resume(self, session_id: str, answer: str, output_path: str | None = None) -> RecipeResult:
        """Answers a question the recipe asked. `output_path` is where the
        finished image should go; the directory used before the question may
        be gone by now, so a caller with a fresh one passes it here."""
        prompt = answer + " " + _OUTPUT_CONTRACT.format(output_path=output_path or "the same output path as before")
        return self._invoke(prompt, resume=session_id)

    def interpret_comment(self, comment_text: str, ctx: CommentContext | None = None) -> CommentIntent | None:
        """Asks Claude what a reviewer's comment means, given where the photo
        is and what (if anything) the pipeline just asked. One call replaces
        the old delete/revise/teach classifier and the rule that any reply
        to the "which album?" question was an album name: the same words
        ("yes, move it") mean different things depending on what was asked.

        Returns None when the comment could not be interpreted at all (the
        call failed or the reply was unusable). The caller leaves such a
        comment for the next cycle rather than guessing, because a wrong
        guess can delete a photo or create an album. A reply that parses
        but doesn't make sense in context degrades to "unclear" (ask back)
        or "revise", never to an action the context doesn't support.

        The model's answer is JSON nested inside the CLI's own `result`
        wrapper field, not top-level -- see module docstring, point 2."""
        ctx = ctx or CommentContext()
        prompt = _interpret_prompt(comment_text, ctx)
        token = resolve_oauth_token(self._secrets_file)
        env = os.environ.copy()
        if token:
            env[ENV_TOKEN_VAR] = token
        cmd = [self._claude_binary, "-p", prompt, "--output-format", "json",
               "--permission-mode", _PERMISSION_MODE]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.exception("comment interpretation could not run")
            return None
        if proc.returncode != 0:
            log.warning(
                "comment interpretation exited %s: %s",
                proc.returncode, (proc.stderr or "").strip()[:300],
            )
            return None
        try:
            outer = json.loads(proc.stdout)
            payload = _extract_json_object(outer.get("result", ""))
            return _intent_from_payload(payload, ctx)
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            log.warning("comment interpretation returned unparseable output")
            return None

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


def _text(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _intent_from_payload(payload: dict, ctx: CommentContext) -> CommentIntent:
    """Turns the model's JSON into a CommentIntent, refusing anything the
    context doesn't support (an album with no usable name, a rule id that
    doesn't exist, a yes with nothing proposed)."""
    intent = payload.get("intent")
    if intent == "delete":
        return CommentIntent("delete")
    if intent == "none":
        return CommentIntent("none")
    if intent == "teach":
        rule = _text(payload, "rule")
        if rule:
            scope = payload.get("scope")
            return CommentIntent("teach", rule=rule, scope=scope if scope in SCOPES else "all")
        log.warning("comment interpreted as teach but with no usable rule; treating as revise")
        return CommentIntent("revise")
    if intent == "album":
        name = _text(payload, "album")
        if name:
            name = name.strip("\"'` ")
        if name and len(name) <= MAX_ALBUM_NAME_CHARS and "\n" not in name:
            existing = {a.casefold(): a for a in ctx.albums}
            return CommentIntent("album", album=existing.get(name.casefold(), name))
        return CommentIntent("unclear", question="Which album should this go in? Reply with just the album's name.")
    if intent == "undo":
        steps = payload.get("steps")
        if isinstance(steps, str) and steps.strip().lower() in ("all", "everything"):
            steps = len(ctx.history)
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            steps = 1
        return CommentIntent("undo", steps=min(steps, max(len(ctx.history), 1)))
    if intent == "answer":
        return CommentIntent("answer" if ctx.awaiting == "clarification" else "revise")
    if intent == "forget":
        rule_id = (_text(payload, "rule_id") or "").lower()
        if rule_id in {rid for rid, _ in ctx.rules}:
            return CommentIntent("forget", rule_id=rule_id)
        return CommentIntent("unclear", question='Which saved rule should I forget? Reply "forget rN" with its number.')
    if intent in ("yes", "no"):
        return CommentIntent(intent) if ctx.proposal else CommentIntent("none")
    if intent == "unclear":
        question = _text(payload, "question") or "I wasn't sure what you meant. Could you say it another way?"
        return CommentIntent("unclear", question=question)
    return CommentIntent("revise")


def _interpret_prompt(comment_text: str, ctx: CommentContext) -> str:
    where = "the Review queue" if ctx.where == "review" else f"the managed album {ctx.where!r}"
    lines = [
        "You interpret comments that a household member writes on a photo in "
        "an automated photo-review pipeline. A pipeline posts edited photos "
        f"for review; this photo is in {where}. Decide what the comment "
        "means, using the state below. Comments are casual and may be "
        "sloppy, so judge meaning, not keywords.",
        "",
        "State:",
    ]
    if ctx.awaiting == "album":
        lines.append(
            "- The pipeline just asked: \"Which album should this be promoted to?\" "
            f"Existing albums: {', '.join(ctx.albums) or '(none yet)'}. A reply "
            "that names, or clearly points at, an album is the answer."
        )
    elif ctx.awaiting == "clarification":
        lines.append(
            "- The pipeline just asked the reviewer about the photo itself. "
            f"Their original request was: {ctx.request!r}. "
            f"The question was: {ctx.question!r}."
        )
    else:
        lines.append("- The pipeline is not waiting on any question.")
    if ctx.albums and ctx.awaiting != "album":
        lines.append(f"- Existing albums: {', '.join(ctx.albums)}.")
    if ctx.proposal:
        lines.append(
            f"- The pipeline proposed saving this rule and is waiting for "
            f"yes/no: {ctx.proposal[0]}: {ctx.proposal[1]!r}."
        )
    if ctx.history:
        lines.append(
            "- Adjustments already applied to this photo at the reviewer's request, "
            "oldest first: " + " ".join(f"{i}) {note}" for i, note in enumerate(ctx.history, 1))
        )
    else:
        lines.append("- No adjustments have been applied to this photo yet.")
    if ctx.rules:
        lines.append("- Saved rules: " + "; ".join(f"{rid}: {text!r}" for rid, text in ctx.rules) + ".")
    lines += [
        "",
        f"The comment: {comment_text!r}",
        "",
        "Choose exactly ONE intent:",
        "- answer: replies to the question about the photo above (only when "
        "that question is open and the comment answers it).",
        "- album: asks to put/send/move this photo in an album, or names "
        "one in reply to the album question. Give \"album\": the album's name "
        "(use an existing album's exact name when it clearly refers to one). "
        "Only for a photo in the Review queue.",
        "- delete: asks to delete/remove/discard/trash this photo entirely.",
        "- teach: asks for something to apply to FUTURE photos too, with an "
        "explicit cue (always, never, from now on, next time, going forward, "
        "in general, remember). Give \"rule\" (one general sentence) and "
        "\"scope\" (\"collage\", \"single\" or \"all\"). A note about only this "
        "photo is not teach.",
        "- forget: asks to stop following a saved rule. Give \"rule_id\".",
        "- yes / no: accepts / declines the proposed rule above (only when "
        "one is waiting).",
        "- undo: asks to undo / revert / take back the last change, put it back "
        "the way it was, or go back to the original (\"undo\", \"undo that\", "
        "\"put it back\", \"that was better before\", \"start over\"). Give "
        "\"steps\": how many of the adjustments above to take back (default 1; "
        "the word \"all\" for start over / back to the original). Not for "
        "retiring a saved rule (that is forget), and not when the comment "
        "describes a new change instead (that is revise).",
        "- revise: any instruction to change this image (crop, recenter, "
        "tilt, color, mat, ...), including when it arrives while a question "
        "is open but doesn't answer it.",
        "- none: needs no action (thanks, \"looks great\", chatter).",
        "- unclear: you cannot tell what is wanted, or two readings are both "
        "plausible and a wrong guess would matter (an unwanted album, a "
        "deletion). Give \"question\": one short question to ask the reviewer.",
        "",
        "Prefer unclear over guessing when the choice is between creating an "
        "album and editing the photo, or when a deletion is possible but not "
        "stated. Never choose album for something that reads as an edit "
        "instruction. Reply with ONLY one JSON object, no other text, e.g. "
        '{"intent": "revise"} or {"intent": "album", "album": "Holiday"} or '
        '{"intent": "unclear", "question": "..."}.',
    ]
    return "\n".join(lines)


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
