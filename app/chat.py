"""The per-photo chat: how a reviewer asks for changes to a photo's image.

A reviewer types a message under a photo. `enqueue` records it (state
"queued") and the worker later calls `run_job`, which asks Claude what the
message means (see RecipeRunner.interpret_comment) and acts on it:

  revise          re-run the recipe from the originals with every earlier
                  instruction replayed, and save the result as a new revision
  answer          reply to a question the recipe asked about the photo
  undo            take back the last adjustment(s), as a revert (no Claude
                  call beyond the interpretation)
  teach / forget  save or retire a standing rule for future photos
  yes / no        accept or dismiss a rule the recipe proposed
  anything else   a short reply (moving, deleting, promoting and trashing are
                  buttons, not chat)

Messages are handled strictly in the order they were sent, one at a time per
photo, and the whole conversation is kept on the photo (`Photo.chat`).

Credits: every revision costs Claude plan credits, so a message that fails is
not retried by itself. The reviewer sees that it failed and sends it again.

Nothing here talks to Immich. A new revision only changes the library; the
poll cycle publishes it (see publish.py).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

from .library import (
    CHAT_DONE, CHAT_FAILED, CHAT_QUEUED, CHAT_WORKING,
    INBOX, REVIEW, STATUS_AWAITING_ANSWER, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY, STATUS_WAITING,
    ChatMessage, LibraryStore, Photo, Revision,
)
from .recipe_runner import CommentContext, RecipeResult, RecipeRunner
from .replay import bounded_history, note_with_history
from .revert import RevertError, revert_to
from .revisions import RevisionStore
from .rules import RulesFull, RulesStore

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
# How many of a photo's messages may be waiting at once.
MAX_QUEUED = 10
# How much of the conversation is kept. Messages still waiting are never cut.
MAX_TRANSCRIPT = 100


class ChatError(Exception):
    """A message that can't be accepted. `status` is the HTTP status to use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Conflict(Exception):
    """The photo changed while its image was being made."""


# ---- what a photo allows ------------------------------------------------------


def blocked_reason(photo: Photo) -> str | None:
    """Why this photo's image can't be changed through chat, or None."""
    if photo.trashed:
        return "It is in the trash. Restore it to work on it."
    if photo.media_type == "video":
        return "Videos are kept exactly as they were added; they can't be modified."
    if photo.imported:
        return "This photo was already finished when it was imported, so it can't be modified."
    if photo.status == STATUS_PROCESSING:
        return "It is still being processed."
    if photo.status == STATUS_WAITING:
        return "It is waiting for a partner photo."
    if photo.status == STATUS_FAILED:
        return "Processing failed, so there is nothing to change yet."
    return None


# ---- the transcript -------------------------------------------------------------


def _trim(photo: Photo) -> None:
    extra = len(photo.chat) - MAX_TRANSCRIPT
    if extra <= 0:
        return
    keep: list[ChatMessage] = []
    for message in photo.chat:
        is_open = message.role == "user" and message.state in (CHAT_QUEUED, CHAT_WORKING)
        if extra > 0 and not is_open:
            extra -= 1
            continue
        keep.append(message)
    photo.chat = keep


def _append(photo: Photo, role: str, text: str, state: str) -> ChatMessage:
    message = ChatMessage(id=photo.next_message_id(), role=role, text=text, state=state)
    photo.chat.append(message)
    _trim(photo)
    return message


def clean_text(text) -> str:
    if not isinstance(text, str):
        raise ChatError("the message must be text")
    cleaned = " ".join("".join(c if c.isprintable() or c in "\n\t" else " " for c in text).split())
    if not cleaned:
        raise ChatError("the message is empty")
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise ChatError(f"messages are at most {MAX_MESSAGE_CHARS} characters; this one is {len(cleaned)}")
    return cleaned


def enqueue(store: LibraryStore, photo_id: str, text) -> int:
    """Records a reviewer's message, queued for the worker. Returns its id.
    Raises ChatError if the photo can't be changed or the message is not
    acceptable."""
    cleaned = clean_text(text)

    def add(library) -> int:
        photo = library.photos.get(photo_id)
        if photo is None:
            raise ChatError("no such photo", 404)
        why = blocked_reason(photo)
        if why:
            raise ChatError(why)
        if len(photo.open_messages()) >= MAX_QUEUED:
            raise ChatError("Too many messages are waiting; let it catch up first.", 429)
        return _append(photo, "user", cleaned, CHAT_QUEUED).id

    return store.update(add)


def _say(store: LibraryStore, photo_id: str, text: str) -> None:
    try:
        store.update_photo(photo_id, lambda p: _append(p, "claude", text, CHAT_DONE))
    except KeyError:
        pass


def _set_state(store: LibraryStore, photo_id: str, message_id: int, state: str, *, only_if: str | None = None) -> None:
    def apply(photo: Photo) -> None:
        for m in photo.chat:
            if m.id == message_id and (only_if is None or m.state == only_if):
                m.state = state

    try:
        store.update_photo(photo_id, apply)
    except KeyError:
        pass


def next_open_message(photo: Photo) -> ChatMessage | None:
    open_messages = photo.open_messages()
    return open_messages[0] if open_messages else None


# ---- running one message ----------------------------------------------------------


def run_job(
    store: LibraryStore,
    revisions: RevisionStore,
    recipe: RecipeRunner,
    rules: RulesStore | None,
    photo_id: str,
) -> bool:
    """Handles the oldest waiting message of a photo. Returns True if the
    photo's image or state changed in a way the rest of the pipeline needs to
    act on (a new revision to publish, a first result to publish)."""
    photo = store.load().photos.get(photo_id)
    if photo is None:
        return False
    message = next_open_message(photo)
    if message is None:
        return False
    _set_state(store, photo_id, message.id, CHAT_WORKING)
    try:
        changed = _handle(store, revisions, recipe, rules, photo_id, message)
    except Exception as exc:
        log.exception("chat message %s of %s failed", message.id, photo_id)
        _set_state(store, photo_id, message.id, CHAT_FAILED)
        reason = str(exc) or exc.__class__.__name__
        _say(store, photo_id, f"That didn't work: {reason[:300]}. You can send it again.")
        return False
    # (a handler that gave up has already marked the message failed)
    _set_state(store, photo_id, message.id, CHAT_DONE, only_if=CHAT_WORKING)
    return changed


def _fail(store: LibraryStore, photo_id: str, message: ChatMessage, reply: str) -> bool:
    _set_state(store, photo_id, message.id, CHAT_FAILED)
    _say(store, photo_id, reply)
    return False


def _albums(library) -> list[str]:
    homes = {p.home for p in library.photos.values() if not p.trashed} - {INBOX, REVIEW}
    return sorted(set(library.albums) | homes)


def _handle(
    store: LibraryStore, revisions: RevisionStore, recipe: RecipeRunner, rules: RulesStore | None,
    photo_id: str, message: ChatMessage,
) -> bool:
    library = store.load()
    photo = library.photos.get(photo_id)
    if photo is None:
        return False
    if photo.trashed:
        return _fail(store, photo_id, message, "This photo is in the trash, so I left your message alone.")
    awaiting = photo.status == STATUS_AWAITING_ANSWER
    first_run = photo.current_revision() is None

    proposal = None
    active_rules: list[tuple[str, str]] = []
    if rules is not None:
        try:
            found = rules.pending_proposal_for(photo.id)
            proposal = (found.id, found.text) if found else None
            active_rules = [(r.id, r.text) for r in rules.all() if r.status == "active"]
        except Exception:
            log.exception("could not read the rules for a chat message")

    ctx = CommentContext(
        where=photo.home,
        awaiting="clarification" if awaiting else None,
        request=photo.pending_instruction if awaiting else None,
        question=photo.question if awaiting else None,
        albums=_albums(library),
        rules=active_rules,
        proposal=proposal,
        history=photo.instructions(),
    )
    intent = recipe.interpret_comment(message.text, ctx)
    if intent is None:
        return _fail(
            store, photo_id, message,
            "I couldn't work out what that meant just now. Please try again.",
        )

    kind = intent.intent
    if awaiting and kind == "revise" and first_run:
        kind = "answer"   # there is no image yet, so guidance is the answer
    if kind == "answer" and not awaiting:
        kind = "revise"   # nothing was asked; treat it as a request

    if kind == "revise":
        return _revise(store, revisions, recipe, rules, photo, message)
    if kind == "answer":
        return _answer(store, revisions, recipe, rules, photo, message)
    if kind == "undo":
        return _undo(store, revisions, photo, intent.steps)
    if kind == "teach":
        return _teach(store, rules, photo, intent)
    if kind == "forget":
        return _forget(store, rules, photo, intent)
    if kind in ("yes", "no"):
        return _confirm(store, rules, photo, kind == "yes")
    if kind in ("album", "delete"):
        _say(store, photo_id, "Moving and deleting are done with the buttons above, not in chat.")
        return False
    if kind == "unclear":
        _say(store, photo_id, intent.question or "I'm not sure what you'd like. Could you say it another way?")
        return False
    _say(store, photo_id, "OK.")
    return False


# ---- revising ------------------------------------------------------------------------


def _source_paths(photo: Photo, revisions: RevisionStore) -> list[str]:
    paths = []
    for source in photo.sources:
        if not source.file or not revisions.exists(source.file):
            raise RuntimeError("the original photo is not in the store, so it can't be reprocessed")
        paths.append(revisions.path(source.file))
    if not paths:
        raise RuntimeError("this photo has no originals to work from")
    return paths


def _rules_for(rules: RulesStore | None, photo: Photo) -> list[str]:
    if rules is None:
        return []
    try:
        return rules.active_texts("collage" if photo.kind == "collage" else "single")
    except Exception:
        log.exception("could not read reviewer rules; revising without them")
        return []


def _revise(
    store: LibraryStore, revisions: RevisionStore, recipe: RecipeRunner, rules: RulesStore | None,
    photo: Photo, message: ChatMessage,
) -> bool:
    paths = _source_paths(photo, revisions)
    active = _rules_for(rules, photo)
    note = note_with_history(bounded_history(photo.instructions()), message.text)
    tmp = tempfile.mkdtemp(prefix="pipeline-chat-")
    try:
        out = os.path.join(tmp, "output.jpg")
        if photo.kind == "collage":
            result = recipe.run_collage(paths, out, note=note, rules=active)
        else:
            result = recipe.run_single(paths[0], out, note=note, rules=active)
        if result.status == "needs_clarification":
            def ask(p: Photo) -> None:
                p.status = STATUS_AWAITING_ANSWER
                p.question = result.question or "Need more information to proceed."
                p.session_id = result.session_id
                p.pending_instruction = message.text
                _append(p, "claude", p.question, CHAT_DONE)

            store.update_photo(photo.id, ask)
            return False
        return _save_result(store, revisions, rules, photo, result, message.text, active)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _save_result(
    store: LibraryStore, revisions: RevisionStore, rules: RulesStore | None, photo: Photo,
    result: RecipeResult, instruction: str | None, active: list[str],
) -> bool:
    """Stores a finished image as the photo's next revision (or, for a photo
    that has none yet, its first) and tells the reviewer."""
    if not result.output_path or not os.path.isfile(result.output_path):
        raise RuntimeError("the recipe finished but did not produce an image")
    expected_current = photo.current
    first = photo.current_revision() is None
    n = photo.next_revision_number()
    rel, digest = revisions.save_revision(photo.id, n, result.output_path)

    def record(p: Photo) -> None:
        if p.trashed or p.current != expected_current or p.next_revision_number() != n:
            raise _Conflict()
        p.revisions.append(Revision(
            n=n, parent=p.current, file=rel, instruction=None if first else instruction,
            rules=list(active), session_id=result.session_id, sha256=digest, origin="processed",
        ))
        p.current = n
        p.question = None
        p.pending_instruction = None
        if first:
            # The first result: the worker publishes it and takes the
            # originals out of the entry queue, as for any new photo.
            p.home = REVIEW
            p.status = STATUS_PROCESSING
        else:
            p.status = STATUS_READY
        _append(p, "claude", "Done. It's ready for review." if first else f"Done. This is step {p.step_of(n)}.", CHAT_DONE)

    try:
        store.update_photo(photo.id, record)
    except _Conflict:
        try:
            os.unlink(revisions.path(rel))
        except OSError:
            pass
        raise RuntimeError("the photo changed while it was being made; nothing was saved") from None
    except BaseException:
        try:
            os.unlink(revisions.path(rel))
        except OSError:
            pass
        raise

    if result.lesson and rules is not None:
        _propose(store, rules, photo, result)
    return True


def _propose(store: LibraryStore, rules: RulesStore, photo: Photo, result: RecipeResult) -> None:
    """Offers the lesson the recipe drew from a revision as a rule. It only
    becomes active when the reviewer accepts it."""
    try:
        rules.add(
            result.lesson, result.lesson_scope, status="proposed", origin="proposed",
            lineage_id=photo.id, source_asset_id=photo.id,
        )
    except (ValueError, RulesFull):
        return
    except Exception:
        log.exception("could not save a proposed rule")
        return
    _say(store, photo.id, f"Should I do this on future photos too? “{result.lesson}” You can accept or dismiss it below.")


# ---- answering a question -----------------------------------------------------------


def _answer(
    store: LibraryStore, revisions: RevisionStore, recipe: RecipeRunner, rules: RulesStore | None,
    photo: Photo, message: ChatMessage,
) -> bool:
    if not photo.session_id:
        raise RuntimeError("there is no open question to answer")
    first = photo.current_revision() is None
    instruction = photo.pending_instruction
    tmp = tempfile.mkdtemp(prefix="pipeline-chat-")
    try:
        out = os.path.join(tmp, "output.jpg")
        result = recipe.resume(photo.session_id, message.text, output_path=out)
        if result.status == "needs_clarification":
            def ask(p: Photo) -> None:
                p.question = result.question or "Need more information to proceed."
                p.session_id = result.session_id or p.session_id
                _append(p, "claude", p.question, CHAT_DONE)

            store.update_photo(photo.id, ask)
            return False
        return _save_result(store, revisions, rules, photo, result, instruction, _rules_for(rules, photo))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- undo, rules ----------------------------------------------------------------------


def _undo(store: LibraryStore, revisions: RevisionStore, photo: Photo, steps: int) -> bool:
    lineage = photo.lineage()
    if len(lineage) <= 1:
        _say(store, photo.id, "There's nothing to undo; this is the first version.")
        return False
    index = max(0, len(lineage) - 1 - max(1, steps))
    target = lineage[index]
    try:
        new = revert_to(store, revisions, photo.id, target.n)
    except RevertError as exc:
        raise RuntimeError(str(exc)) from exc
    if new is None:
        _say(store, photo.id, "It already looks like that.")
        return False
    step = store.load().photos[photo.id].step_of(new.n)
    back = photo.step_of(target.n)
    where = "the first version" if index == 0 else f"how it looked at step {back}"
    _say(store, photo.id, f"Undone. It is back to {where}, saved as step {step}.")
    return True


def _teach(store: LibraryStore, rules: RulesStore | None, photo: Photo, intent) -> bool:
    if rules is None:
        _say(store, photo.id, "Saving rules isn't available right now.")
        return False
    try:
        rule = rules.add(intent.rule or "", intent.scope, origin="comment", lineage_id=photo.id)
    except RulesFull as exc:
        _say(store, photo.id, f"I can't save that: {exc}. Retire one on the Rules page first.")
        return False
    except ValueError as exc:
        _say(store, photo.id, f"I can't save that as a rule: {exc}.")
        return False
    _say(store, photo.id, f"Saved for future photos: “{rule.text}”")
    return False


def _forget(store: LibraryStore, rules: RulesStore | None, photo: Photo, intent) -> bool:
    if rules is None or not intent.rule_id:
        _say(store, photo.id, "I couldn't tell which rule you meant. The Rules page lists them.")
        return False
    rule = rules.set_status(intent.rule_id, "retired")
    if rule is None:
        _say(store, photo.id, "I couldn't find that rule.")
    else:
        _say(store, photo.id, f"Forgotten: “{rule.text}”")
    return False


def _confirm(store: LibraryStore, rules: RulesStore | None, photo: Photo, accept: bool) -> bool:
    found = rules.pending_proposal_for(photo.id) if rules is not None else None
    if found is None:
        _say(store, photo.id, "There's no suggested rule waiting for an answer.")
        return False
    try:
        rules.set_status(found.id, "active" if accept else "retired")
    except RulesFull as exc:
        _say(store, photo.id, f"I can't save that: {exc}. Retire one on the Rules page first.")
        return False
    _say(store, photo.id, f"Saved for future photos: “{found.text}”" if accept else "OK, I won't use it.")
    return False
