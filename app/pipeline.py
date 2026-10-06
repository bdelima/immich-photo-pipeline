"""The poll-cycle orchestration described in the design doc: two entry
queues, Review, and promotion into whichever managed album a reply names.

The decision functions (`plan_*`) are pure — they take plain data in and
return a plan of what to do, with no Immich or filesystem calls — so they
can be unit tested without a live Immich instance. `Pipeline` executes
those plans against the real clients.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Literal

from .config import Config
from .immich_client import Asset, Comment, ImmichClient, ImmichError
from .recipe_runner import CommentContext, CommentIntent, RecipeResult, RecipeRunner
from .rules import SCOPE_LABELS, RulesFull, RulesStore
from .sharing import ensure_shared
from .state import ImageState, PipelineState, StateStore

log = logging.getLogger(__name__)

PORTRAIT = "PORTRAIT"

# How many earlier reviewer instructions are replayed to the recipe on each
# revision (the most recent ones), and how much of each is kept. Bounds the
# prompt for a photo that has been adjusted many times.
MAX_REVISION_NOTES = 15
MAX_REVISION_NOTE_CHARS = 400

# Replies handled without a Claude call: "forget r7" retires a rule, and
# yes/no answers a proposed rule. Whole-comment matches only, so a longer
# comment that happens to start with "yes" is treated as a normal revision.
_FORGET_RE = re.compile(r"^\s*(?:forget|undo|retire)\s+(r\d+)\s*[.!]*\s*$", re.IGNORECASE)
_YES_RE = re.compile(
    r"^\s*(?:yes|y|yep|yeah|yup|sure|ok|okay|please do|do it|"
    r"remember (?:it|that|this)|save (?:it|that|this))\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(?:no|n|nope|nah|don'?t|do not|drop it|discard(?: it)?|never ?mind)\s*[.!]*\s*$",
    re.IGNORECASE,
)


# ---- pure decision helpers (unit-testable without Immich) ----------------


def is_portrait(asset: Asset) -> bool:
    orientation = (asset.exif_orientation or "").strip()
    # EXIF orientation 6/8 mean the stored dims are landscape but the
    # displayed image is rotated to portrait; 1 is upright. Immich's
    # exifInfo also carries width/height, but orientation alone is enough
    # once exif_transpose-equivalent handling has already happened
    # upstream in Immich's own thumbnailing. Treat unknown as not-portrait
    # (conservative: don't block solo wallpaper processing on a guess).
    return orientation in {"6", "8", "-90", "90"}


@dataclass
class CollagePlan:
    action: Literal["wait", "group"]
    asset_ids: list[str]


def plan_collage_maker(assets: list[Asset]) -> CollagePlan:
    """Collage Maker always holds a lone portrait; two or more get grouped.
    3-up preferred per the recipe's own guidance, 2-up otherwise."""
    if len(assets) >= 2:
        group = assets[:3] if len(assets) >= 3 else assets
        return CollagePlan(action="group", asset_ids=[a.id for a in group])
    return CollagePlan(action="wait", asset_ids=[a.id for a in assets])


def new_comments(comments: list[Comment], acted_on: set[str]) -> list[Comment]:
    """Comments not yet acted on, excluding the pipeline's own posts (an
    acknowledgment or question the pipeline posted must never be read back
    as an instruction to itself)."""
    return [c for c in comments if c.id not in acted_on and not c.is_own]


# ---- execution ------------------------------------------------------------


class Pipeline:
    def __init__(
        self,
        config: Config,
        immich: ImmichClient,
        recipe: RecipeRunner,
        store: StateStore,
        extra_clients: list[ImmichClient] = (),
        rules: RulesStore | None = None,
        share_user_ids: list[str] = (),
    ):
        self.cfg = config
        self.immich = immich
        self.recipe = recipe
        self.store = store
        # One ImmichClient per extra household API key (IMMICH_EXTRA_API_KEYS),
        # tried in order after the primary when removing an entry-queue
        # original -- see _clear_from_entry_queue for why.
        self.extra_clients = list(extra_clients)
        # Reviewer-taught rules (app/rules.py). Optional so the pipeline
        # still runs, rule-free, without one.
        self.rules = rules
        # Accounts every album the pipeline creates is shared with (see
        # sharing.py); empty means don't share.
        self.share_user_ids = list(share_user_ids)

    def run_once(self) -> None:
        # Exclusive for the whole cycle so a concurrent one-off import
        # (app/importer.py, a separate process) can't be overwritten by
        # this cycle's save -- see StateStore.exclusive.
        with self.store.exclusive():
            state = self.store.load()
            try:
                self._flow1_wallpaper(state)
                self._flow1_collage(state)
                self._flow2_review(state)
                self._flow3_managed(state)
                self._reap_deleted(state)
            finally:
                # Save even when a step fails: what earlier steps already
                # did on Immich (comments answered, albums chosen) must be
                # remembered, or the next cycle would redo it.
                self.store.save(state)

    # Flow 1a — Wallpaper Maker: process solo, immediately.
    def _flow1_wallpaper(self, state: PipelineState) -> None:
        assets = self.immich.list_album_assets(self.cfg.wallpaper_album_id)
        tracked_sources = {sid for s in state.images.values() for sid in s.source_asset_ids}
        for asset in assets:
            if asset.id in tracked_sources:
                continue
            try:
                log.info("wallpaper: processing %s solo", asset.id)
                self._process_and_land_in_review(state, [asset], collage=False)
            except Exception:
                log.exception("failed processing wallpaper asset %s; leaving it for next cycle", asset.id)

    # Flow 1b — Collage Maker: hold a singleton, group 2+.
    def _flow1_collage(self, state: PipelineState) -> None:
        assets = self.immich.list_album_assets(self.cfg.collage_album_id)
        tracked_sources = {sid for s in state.images.values() for sid in s.source_asset_ids}
        untracked = [a for a in assets if a.id not in tracked_sources]
        plan = plan_collage_maker(untracked)
        if plan.action == "wait":
            if plan.asset_ids:
                self._ask_lone_portrait_question(state, plan.asset_ids[0])
            return
        by_id = {a.id: a for a in untracked}
        selected = [by_id[aid] for aid in plan.asset_ids]
        try:
            log.info("collage: grouping %s", plan.asset_ids)
            self._process_and_land_in_review(state, selected, collage=True)
        except Exception:
            log.exception("failed processing collage group %s; leaving it for next cycle", plan.asset_ids)

    def _ask_lone_portrait_question(self, state: PipelineState, asset_id: str) -> None:
        lineage_id = asset_id
        existing = state.images.get(lineage_id)
        if existing and existing.awaiting_clarification:
            return  # already asked; the comment-diff loop below handles the reply
        question = (
            "Waiting for another portrait to pair with this one for a "
            "collage. Reply here to force it through solo (e.g. \"do the "
            "best you can cropping for 16:9\"), or just drop a second "
            "portrait into Collage Maker alongside it."
        )
        self.immich.post_comment(question, album_id=self.cfg.collage_album_id, asset_id=asset_id)
        state.images[lineage_id] = ImageState(
            source_asset_ids=[asset_id], home="collage_maker_wait", awaiting_clarification=True,
        )

    def _process_and_land_in_review(self, state: PipelineState, source_assets: list[Asset], collage: bool) -> None:
        source_ids = [a.id for a in source_assets]
        lineage_id = source_ids[0]
        tmp_dir = tempfile.mkdtemp(prefix="pipeline-")
        try:
            src_paths = [self._download(sid, tmp_dir) for sid in source_ids]
            out_path = os.path.join(tmp_dir, "output.jpg")
            rules = self._rules_for("collage" if collage else "single")
            result = self.recipe.run_collage(src_paths, out_path, rules=rules) if collage \
                else self.recipe.run_single(src_paths[0], out_path, rules=rules)
            if result.status == "needs_clarification":
                entry_album = self.cfg.collage_album_id if collage else self.cfg.wallpaper_album_id
                self.immich.post_comment(result.question or "Need more information to proceed.",
                                          album_id=entry_album, asset_id=lineage_id)
                state.images[lineage_id] = ImageState(
                    source_asset_ids=source_ids, home="awaiting_clarification",
                    awaiting_clarification=True, claude_session_id=result.session_id,
                )
                return
            new_asset_id = self.immich.upload_asset(result.output_path, f"{lineage_id}.jpg")
            self.immich.add_assets_to_album(self.cfg.review_album_id, [new_asset_id])
            source_album = self.cfg.collage_album_id if collage else self.cfg.wallpaper_album_id
            self._clear_from_entry_queue(source_album, source_assets)
            state.images[lineage_id] = ImageState(
                source_asset_ids=source_ids, current_asset_id=new_asset_id, home="review",
            )
        finally:
            _cleanup(tmp_dir)

    def _clear_from_entry_queue(self, album_id: str, assets: list[Asset]) -> None:
        """Immich restricts removing an asset from an album to whoever
        added it -- not just this account, since the originals here are
        normally added by whoever's phone they came from, not the
        pipeline. Confirmed as intentional in Immich, not a bug, and true
        even for the album's own owner or an admin API key
        (github.com/immich-app/immich/discussions/6804,
        github.com/immich-app/immich/discussions/31692).

        So: try the primary API key first, then each configured household
        account's key in turn (IMMICH_EXTRA_API_KEYS) -- one of them
        should belong to whoever actually added it. Only if every
        configured account fails does this fall back to leaving a comment
        asking the human to delete it themselves; `tracked_sources`
        upstream means a leftover original never gets reprocessed either
        way, so this is cosmetic, not a correctness issue."""
        for asset in assets:
            if self._try_remove_from_album(album_id, asset.id):
                continue
            log.warning(
                "no configured Immich account could remove asset %s (owner %s) from album %s",
                asset.id, asset.owner_id or "unknown", album_id,
            )
            try:
                self.immich.post_comment(
                    "Processed and moved to Review -- none of this pipeline's "
                    "configured accounts has permission to remove this "
                    "original from here (Immich restricts that to whoever "
                    "added it), so it's safe to delete yourself whenever "
                    "you'd like.",
                    album_id=album_id, asset_id=asset.id,
                )
            except ImmichError:
                log.exception("could not post cleanup comment on %s", asset.id)

    def _try_remove_from_album(self, album_id: str, asset_id: str) -> bool:
        for client in (self.immich, *self.extra_clients):
            try:
                client.remove_assets_from_album(album_id, [asset_id])
                return True
            except ImmichError:
                continue
        return False

    def _download(self, asset_id: str, tmp_dir: str) -> str:
        """Downloads the asset's original bytes into tmp_dir -- see
        ImmichClient.download_asset_original for the actual API call and
        why it needs no fallback to extra_clients (unlike removal)."""
        return self.immich.download_asset_original(asset_id, tmp_dir)

    # Flow 2 — Review: like promotes (after naming an album), comment revises.
    def _flow2_review(self, state: PipelineState) -> None:
        review_assets = {a.id: a for a in self.immich.list_album_assets(self.cfg.review_album_id)}
        for lineage_id, img in list(state.images.items()):
            if img.home != "review" or not img.current_asset_id:
                continue
            asset = review_assets.get(img.current_asset_id)
            if asset is None:
                continue
            try:
                comments = self.immich.list_comments(album_id=self.cfg.review_album_id, asset_id=asset.id)
                acted = set(img.acted_comment_ids)
                fresh = new_comments(comments, acted)
                if not img.awaiting_clarification:
                    new_likes = self._new_like_ids(img, asset.id)
                    if asset.is_favorite or new_likes:
                        log.info(
                            "review: like on %s (%s) -> asking which album",
                            asset.id, "thumbs-up" if new_likes else "favorite flag",
                        )
                        img.acted_like_ids.extend(new_likes)
                        self._ask_which_album(state, lineage_id, asset.id)
                        continue
                for comment in fresh:
                    if self._handle_fresh_comment(
                        state, lineage_id, img, asset, comment,
                        album_id=self.cfg.review_album_id, in_review=True,
                    ):
                        break
            except Exception:
                log.exception("failed handling Review item %s; leaving it for next cycle", lineage_id)

    def _new_like_ids(self, img: ImageState, asset_id: str) -> list[str]:
        """Thumbs-up activities on this photo in Review that haven't been
        acted on yet. A lookup failure just means "no likes this cycle" so
        comments on the photo are still handled."""
        try:
            ids = self.immich.list_like_ids(album_id=self.cfg.review_album_id, asset_id=asset_id)
        except ImmichError:
            log.exception("could not look up likes on %s", asset_id)
            return []
        seen = set(img.acted_like_ids)
        return [i for i in ids if i not in seen]

    def _handle_fresh_comment(
        self, state: PipelineState, lineage_id: str, img: ImageState, asset: Asset,
        comment: Comment, *, album_id: str, in_review: bool,
    ) -> bool:
        """One reviewer comment on a Review or managed-album item. Returns
        True when the item was deleted and the caller should stop looking at
        its comments (a failed delete also stops, and is retried next cycle
        -- unchanged from before this was factored out)."""
        log.info("comment %s on %s: %r", comment.id, asset.id, comment.text)
        if self._try_rule_commands(img, asset.id, comment, album_id):
            return False
        ctx = self._comment_context(state, img, asset.id, album_id, in_review)
        verdict = self.recipe.interpret_comment(comment.text, ctx)
        if verdict is None:
            # Couldn't interpret it at all (Claude unreachable, unusable
            # reply). Leave the comment unhandled so the next cycle retries,
            # rather than guess: a wrong guess can delete or create things.
            log.warning("comment %s could not be interpreted; will retry next cycle", comment.id)
            return False
        intent = verdict.intent
        log.info("comment %s interpreted as %s", comment.id, intent)
        if intent == "delete":
            self._delete_reviewed_asset(state, lineage_id, asset.id, album_id=album_id)
            return True
        if intent == "none":
            img.acted_comment_ids.append(comment.id)
            return False
        if intent == "unclear":
            self._say(album_id, asset.id, verdict.question or "I wasn't sure what you meant. Could you say it another way?")
            img.acted_comment_ids.append(comment.id)
            return False
        if intent == "album":
            if in_review and self._is_reserved_album_name(verdict.album):
                self._say(
                    album_id, asset.id,
                    f"\"{verdict.album}\" is one of this pipeline's own albums. Which other album should this go in?",
                )
            elif in_review:
                exists = self._find_album(state, verdict.album) is not None
                shared = self._promote_to_album(state, lineage_id, asset.id, verdict.album)
                # Comments belong to one photo in one album, so the photo
                # leaving Review leaves its thread behind. Say it where the
                # photo is now, which is where the reviewer will look.
                dest = state.watched_albums.get(img.home)
                if dest:
                    self._ack(
                        img, dest, asset.id,
                        f"Moved here from Review: this is now in the album \"{img.home}\"" + ("." if exists else " (I created it).")
                        + (" I couldn't share this album with the household (the pipeline account may not own it), "
                           "so likes and comments won't show for others until it is shared by hand in Immich."
                           if shared is False else ""),
                    )
            else:
                self._say(
                    album_id, asset.id,
                    "To move this to a different album, unlike it first so it goes back to Review, then like it again.",
                )
            img.acted_comment_ids.append(comment.id)
            return False
        if intent == "forget":
            self._forget_rule(album_id, asset.id, verdict.rule_id)
            img.acted_comment_ids.append(comment.id)
            return False
        if intent in ("yes", "no"):
            self._answer_proposal(album_id, asset.id, yes=intent == "yes")
            img.acted_comment_ids.append(comment.id)
            return False
        if intent == "undo":
            steps = min(verdict.steps, len(img.revision_notes))
            if steps < 1:
                self._say(
                    album_id, asset.id,
                    "I don't have any changes recorded on this photo to undo (I only track changes "
                    "made since the last update). To go back, ask for the specific change reversed.",
                )
            else:
                undone = img.revision_notes[-steps:]
                self._ack(
                    img, album_id, asset.id,
                    f"Got it: undoing {_short('; '.join(undone))}. "
                    "This takes a minute or two; I'll post the result on the new version of the photo.",
                )
                self._reprocess(
                    state, lineage_id, asset.id, comment.text, target_album=album_id, undo_steps=steps,
                )
            img.acted_comment_ids.append(comment.id)
            return False
        if intent == "teach":
            self._save_taught_rule(lineage_id, asset.id, album_id, verdict)
        # A reply to the recipe's own question about this photo is the
        # answer to it, not a new request and not an album name.
        answering = intent == "answer" and bool(img.clarification_note)
        if not img.imported:
            self._ack(
                img, album_id, asset.id,
                ("Got it: applying your answer and re-running. " if answering else f"Got it: revising ({_short(comment.text)}). ")
                + "This takes a minute or two; I'll post the result on the new version of the photo.",
            )
        self._reprocess(
            state, lineage_id, asset.id, comment.text, target_album=album_id,
            answering=answering,
        )
        img.acted_comment_ids.append(comment.id)
        return False

    def _comment_context(
        self, state: PipelineState, img: ImageState, asset_id: str, album_id: str, in_review: bool,
    ) -> CommentContext:
        """What the interpreter should know about this photo's situation."""
        awaiting = None
        # State saved before awaiting_album existed has only the old flag,
        # and then it always meant the album question.
        if img.awaiting_album or (img.awaiting_clarification and not img.clarification_note):
            awaiting = "album"
        elif img.awaiting_clarification:
            awaiting = "clarification"
        rules: list[tuple[str, str]] = []
        proposal = None
        if self.rules is not None:
            try:
                rules = [(r.id, r.text) for r in self.rules.all() if r.status == "active"]
                pending = self.rules.pending_proposal_for(asset_id)
                proposal = (pending.id, pending.text) if pending else None
            except Exception:
                log.exception("could not read reviewer rules for the interpreter")
        return CommentContext(
            where="review" if in_review else next(
                (name for name, aid in state.watched_albums.items() if aid == album_id), "an album"),
            awaiting=awaiting,
            request=img.clarification_note,
            question=img.clarification_question,
            albums=self._album_names(state),
            rules=rules,
            proposal=proposal,
            history=list(img.revision_notes),
        )

    def _reserved_album_ids(self) -> set[str]:
        """The pipeline's own queues; a photo is never promoted into these."""
        names = ("collage_album_id", "wallpaper_album_id", "review_album_id", "live_album_id")
        return {v for v in (getattr(self.cfg, n, "") for n in names) if v}

    def _album_names(self, state: PipelineState) -> list[str]:
        """Albums a photo can be promoted to: the ones the pipeline already
        manages, then any others in Immich (made by hand, say), minus the
        pipeline's own queues. A lookup failure just means the managed ones."""
        names = list(state.watched_albums)
        try:
            reserved = self._reserved_album_ids()
            seen = {n.casefold() for n in names}
            for album in self.immich.list_albums():
                name = album.get("albumName") or ""
                if name and album.get("id") not in reserved and name.casefold() not in seen:
                    names.append(name)
                    seen.add(name.casefold())
        except ImmichError:
            log.exception("could not list Immich albums for the interpreter")
        return names

    def _find_album(self, state: PipelineState, name: str) -> tuple[str, str] | None:
        """(actual name, id) of the album `name` refers to, ignoring case:
        one the pipeline already manages, else an existing Immich album.
        None means there is no such album and a new one is warranted. An
        Immich error propagates: guessing "no such album" would create a
        duplicate of one that exists."""
        wanted = name.casefold()
        for known, album_id in state.watched_albums.items():
            if known.casefold() == wanted:
                return known, album_id
        reserved = self._reserved_album_ids()
        for album in self.immich.list_albums():
            if (album.get("albumName") or "").casefold() == wanted and album.get("id") not in reserved:
                return album["albumName"], album["id"]
        return None

    def _is_reserved_album_name(self, name: str) -> bool:
        names = ("collage_album_name", "wallpaper_album_name", "review_album_name", "live_album_name")
        return name.casefold() in {getattr(self.cfg, n, "").casefold() for n in names if getattr(self.cfg, n, "")}

    # ---- reviewer-taught rules (see app/rules.py) -------------------------

    def _say(self, album_id: str, asset_id: str, text: str) -> None:
        try:
            self.immich.post_comment(text, album_id=album_id, asset_id=asset_id)
        except ImmichError:
            log.exception("could not post comment on %s", asset_id)

    def _ack(self, img: ImageState, album_id: str, asset_id: str, text: str) -> None:
        """Tells the reviewer, on the photo, what the pipeline understood and
        is about to do, so they aren't left guessing while a slow step (a
        recipe run takes a minute or more) is under way."""
        try:
            posted = self.immich.post_comment(text, album_id=album_id, asset_id=asset_id)
        except ImmichError:
            log.exception("could not post acknowledgment on %s", asset_id)
            return
        self._mark_own(img, posted)

    def _rules_for(self, kind: str) -> list[str]:
        """Active rule texts for a run of `kind` ("single" or "collage").
        A rules file that can't be read must not stop photos being
        processed, so any failure just means no rules this run."""
        if self.rules is None:
            return []
        try:
            return self.rules.active_texts(kind)
        except Exception:
            log.exception("could not read reviewer rules; processing without them")
            return []

    def _try_rule_commands(self, img: ImageState, asset_id: str, comment: Comment, album_id: str) -> bool:
        """Handles "forget rN" and yes/no replies to a proposed rule without
        a Claude call. Returns True (and marks the comment acted on) when the
        comment was one of those."""
        if self.rules is None:
            return False
        forget = _FORGET_RE.match(comment.text)
        if forget:
            log.info("comment %s: forget %s", comment.id, forget.group(1))
            self._forget_rule(album_id, asset_id, forget.group(1).lower())
            img.acted_comment_ids.append(comment.id)
            return True
        if img.awaiting_clarification:
            return False  # a yes/no here would be ambiguous with the album question
        pending = self.rules.pending_proposal_for(asset_id)
        if pending is None:
            return False
        if _YES_RE.match(comment.text):
            yes = True
        elif _NO_RE.match(comment.text):
            yes = False
        else:
            return False
        log.info("comment %s: %s to proposed rule %s", comment.id, "yes" if yes else "no", pending.id)
        self._answer_proposal(album_id, asset_id, yes=yes)
        img.acted_comment_ids.append(comment.id)
        return True

    def _forget_rule(self, album_id: str, asset_id: str, rule_id: str | None) -> None:
        if self.rules is not None and rule_id and self.rules.set_status(rule_id, "retired"):
            self._say(album_id, asset_id, f"Retired rule {rule_id}.")
        else:
            self._say(album_id, asset_id, f"I don't have a rule {rule_id}.")

    def _answer_proposal(self, album_id: str, asset_id: str, *, yes: bool) -> None:
        """Applies a yes/no to the rule proposed on this photo."""
        pending = self.rules.pending_proposal_for(asset_id) if self.rules is not None else None
        if pending is None:
            return
        if not yes:
            self.rules.set_status(pending.id, "retired")
            self._say(album_id, asset_id, "OK, I won't remember that.")
            return
        try:
            self.rules.set_status(pending.id, "active")
        except RulesFull as exc:
            self._say(album_id, asset_id, f"I couldn't save that as a rule: {exc}. Retire one first (reply \"forget rN\" on any photo, or use the web UI).")
        else:
            self._say(album_id, asset_id, f"Saved rule {pending.id} ({SCOPE_LABELS[pending.scope]}). Reply \"forget {pending.id}\" to undo.")

    def _save_taught_rule(self, lineage_id: str, asset_id: str, album_id: str, verdict: CommentIntent) -> None:
        """Saves a rule the reviewer explicitly taught, and says exactly what
        was saved (the model's wording of it may differ from the comment) so
        a wrong rule is obvious and one reply away from undone."""
        if self.rules is None or not verdict.rule:
            return
        try:
            rule = self.rules.add(
                verdict.rule, verdict.scope, status="active", origin="comment",
                lineage_id=lineage_id, source_asset_id=asset_id,
            )
        except RulesFull as exc:
            self._say(album_id, asset_id, f"I couldn't save that as a rule: {exc}. Retire one first (reply \"forget rN\" on any photo, or use the web UI).")
        except ValueError as exc:
            self._say(album_id, asset_id, f"I couldn't save that as a rule: {exc}.")
        else:
            self._say(
                album_id, asset_id,
                f"Saved rule {rule.id} ({SCOPE_LABELS[rule.scope]}): \"{rule.text}\". "
                f"Reply \"forget {rule.id}\" to undo.",
            )

    def _propose_lesson(self, lineage_id: str, asset_id: str, album_id: str, result: RecipeResult) -> None:
        """The recipe offered a general rule after a revision: park it as a
        proposal and ask. Nothing is injected into future runs until the
        reviewer replies yes."""
        if self.rules is None or not result.lesson:
            return
        try:
            rule = self.rules.add(
                result.lesson, result.lesson_scope, status="proposed", origin="proposed",
                lineage_id=lineage_id, source_asset_id=asset_id,
            )
        except ValueError:
            log.info("ignoring unusable proposed lesson %r", result.lesson)
            return
        self._say(
            album_id, asset_id,
            f"Should I remember this for future {SCOPE_LABELS[rule.scope]}? \"{rule.text}\" "
            f"Reply \"yes\" to save it as rule {rule.id}, or \"no\" to drop it.",
        )

    @staticmethod
    def _mark_own(img: ImageState, comment_id: str | None) -> None:
        """Records a comment this pipeline just posted as already handled.
        Belt and braces next to Comment.is_own: even if author detection
        ever failed, the pipeline's own notes and questions can never be
        read back as reviewer instructions."""
        if comment_id:
            img.acted_comment_ids.append(comment_id)

    def _ask_which_album(self, state: PipelineState, lineage_id: str, asset_id: str) -> None:
        names = ", ".join(state.watched_albums) or "(none yet)"
        question = (
            f"Which album should this be promoted to? Currently watched: "
            f"{names}. Reply with one of those names, or a new name to "
            f"create one."
        )
        posted = self.immich.post_comment(question, album_id=self.cfg.review_album_id, asset_id=asset_id)
        self._mark_own(state.images[lineage_id], posted)
        state.images[lineage_id].awaiting_clarification = True
        state.images[lineage_id].awaiting_album = True

    def _delete_reviewed_asset(self, state: PipelineState, lineage_id: str, asset_id: str, *, album_id: str) -> None:
        """A reviewer who isn't the admin Immich account -- the normal
        case; see RecipeRunner.interpret_comment, which decides
        whether a comment means this -- can't delete an asset themselves
        from the Immich UI, so this lets them ask the pipeline to do it
        via comment instead. No extra_clients fallback needed here, unlike
        _clear_from_entry_queue: everything in Review or a managed album
        is this account's own upload (the recipe's output, not a phone
        import), so the primary API key already owns it outright."""
        try:
            self.immich.delete_assets([asset_id])
        except ImmichError:
            log.exception("failed to delete asset %s on reviewer delete request", asset_id)
            try:
                self.immich.post_comment(
                    "Couldn't delete this -- check the pipeline logs.",
                    album_id=album_id, asset_id=asset_id,
                )
            except ImmichError:
                log.exception("could not post delete-failure comment on %s", asset_id)
            return
        log.info("deleted %s on reviewer request", asset_id)
        state.images.pop(lineage_id, None)

    def _promote_to_album(self, state: PipelineState, lineage_id: str, asset_id: str, album_name: str) -> bool:
        """Moves the photo into the named album and returns whether that
        album is shared with the household. Every managed album gets the same
        sharing, whether the pipeline created it or adopted one made by hand:
        Immich only shows likes and comments on a shared album with activity
        on, so an unshared one makes a thumbs-up vanish."""
        found = self._find_album(state, album_name)
        if found:
            # An album that already exists -- one the pipeline manages, or
            # one made by hand in Immich -- is used as is, never duplicated.
            album_name, album_id = found
            if album_name not in state.watched_albums:
                state.watched_albums[album_name] = album_id
                log.info("adopted existing album %r (%s)", album_name, album_id)
        else:
            album_id = self.immich.create_album(album_name)
            state.watched_albums[album_name] = album_id
            log.info("created album %r (%s)", album_name, album_id)
        shared = ensure_shared(self.immich, album_id, self.share_user_ids)
        # Managed albums keep a photo only while its favorite flag is on
        # (_flow3_managed pulls an unfavorited one back to Review). A like
        # given as a thumbs-up activity doesn't set that flag, so set it
        # here; the pipeline account owns the asset, so it can.
        self.immich.set_favorite(asset_id, True)
        self.immich.add_assets_to_album(album_id, [asset_id])
        self.immich.remove_assets_from_album(self.cfg.review_album_id, [asset_id])
        img = state.images[lineage_id]
        img.home = album_name
        img.awaiting_clarification = False
        img.awaiting_album = False
        log.info("promoted %s to album %r", asset_id, album_name)
        return shared

    # Flow 3 — a managed album: comment revises in place, unlike pulls to Review.
    def _flow3_managed(self, state: PipelineState) -> None:
        listings: dict[str, dict[str, Asset] | None] = {}
        for lineage_id, img in list(state.images.items()):
            if img.home in ("review", "awaiting_clarification", "collage_maker_wait") or not img.current_asset_id:
                continue
            album_id = state.watched_albums.get(img.home)
            if not album_id:
                continue
            try:
                if album_id not in listings:
                    listings[album_id] = self._list_managed_album(state, img.home, album_id)
                assets = listings[album_id]
                if assets is None:
                    # The album was deleted in Immich and is dropped from
                    # the managed list; this photo is back in Review (if
                    # it still exists) -- see _list_managed_album.
                    continue
                asset = assets.get(img.current_asset_id)
                if asset is None:
                    continue
                if not asset.is_favorite:
                    self.immich.add_assets_to_album(self.cfg.review_album_id, [asset.id])
                    self.immich.remove_assets_from_album(album_id, [asset.id])
                    previous = img.home
                    img.home = "review"
                    log.info("%s was unfavorited in %r -> moved back to Review", asset.id, album_id)
                    self._ack(
                        img, self.cfg.review_album_id, asset.id,
                        f"Moved back to Review: you unliked it in \"{previous}\". Like it again to choose an album.",
                    )
                    continue
                comments = self.immich.list_comments(album_id=album_id, asset_id=asset.id)
                fresh = new_comments(comments, set(img.acted_comment_ids))
                for comment in fresh:
                    if self._handle_fresh_comment(
                        state, lineage_id, img, asset, comment,
                        album_id=album_id, in_review=False,
                    ):
                        break
            except Exception:
                log.exception("failed handling managed-album item %s; leaving it for next cycle", lineage_id)

    def _list_managed_album(self, state: PipelineState, name: str, album_id: str) -> dict[str, Asset] | None:
        """The assets in a managed album, by id. None when the album no
        longer exists in Immich (deleted by hand): it is dropped from the
        managed list and its photos go back to Review rather than failing
        every poll cycle. Any other failure (Immich down, a network blip)
        propagates, so a hiccup is never mistaken for a deleted album."""
        try:
            return {a.id: a for a in self.immich.list_album_assets(album_id)}
        except ImmichError as exc:
            if not self._album_is_gone(album_id):
                raise
            log.warning("managed album %r (%s) no longer exists in Immich (%s); dropping it", name, album_id, exc)
        state.watched_albums.pop(name, None)
        for lineage_id, img in list(state.images.items()):
            if img.home != name:
                continue
            try:
                if img.current_asset_id:
                    self.immich.add_assets_to_album(self.cfg.review_album_id, [img.current_asset_id])
                img.home = "review"
                img.awaiting_clarification = False
                img.awaiting_album = False
                log.info("%s was in the deleted album %r -> back in Review", img.current_asset_id, name)
            except ImmichError:
                log.exception("could not return %s to Review after album %r was deleted", lineage_id, name)
        return None

    def _album_is_gone(self, album_id: str) -> bool:
        """True only when Immich says the album doesn't exist (or isn't
        visible to this account, which Immich reports the same way)."""
        try:
            self.immich.get_album(album_id)
        except ImmichError as exc:
            return "not found" in str(exc).lower() or "-> 404" in str(exc)
        return False

    def _reprocess(
        self, state: PipelineState, lineage_id: str, old_asset_id: str, note: str, target_album: str,
        answering: bool = False, undo_steps: int = 0,
    ) -> None:
        img = state.images[lineage_id]
        instruction = note
        # An undo re-runs the recipe with the adjustments minus the last
        # `undo_steps` of them, so the photo comes out as it did before.
        kept = img.revision_notes[:-undo_steps] if undo_steps else img.revision_notes
        undone = img.revision_notes[len(kept):]
        if answering:
            instruction = (
                f"{img.clarification_note} (You asked: {img.clarification_question} "
                f"The reviewer answered: {note})"
            )
        if img.imported:
            # No original exists to reprocess from: the "source" is the
            # already-matted image itself, and re-running the recipe on it
            # would double-mat it and then delete the good copy. Callers
            # mark the comment as acted on, so this is said once.
            posted = self.immich.post_comment(
                "This photo was imported already finished, so there's no "
                "original to revise it from. You can still like/unlike it to "
                "move it between albums, or comment \"delete this\" to remove it.",
                album_id=target_album, asset_id=old_asset_id,
            )
            self._mark_own(img, posted)
            return
        if undo_steps:
            log.info("undoing %d adjustment(s) on %s: %r", len(undone), old_asset_id, undone)
        else:
            log.info("revising %s: %r", old_asset_id, instruction)
        tmp_dir = tempfile.mkdtemp(prefix="pipeline-")
        try:
            src_paths = [self._download(sid, tmp_dir) for sid in img.source_asset_ids]
            out_path = os.path.join(tmp_dir, "output.jpg")
            rules = self._rules_for("collage" if len(src_paths) > 1 else "single")
            prompt_note = _replay_note(kept) if undo_steps else _note_with_history(img.revision_notes, instruction)
            result = self.recipe.run_collage(src_paths, out_path, note=prompt_note, rules=rules) if len(src_paths) > 1 \
                else self.recipe.run_single(src_paths[0], out_path, note=prompt_note, rules=rules)
            if result.status == "needs_clarification":
                posted = self.immich.post_comment(result.question or "Need more information to proceed.",
                                                   album_id=target_album, asset_id=old_asset_id)
                self._mark_own(img, posted)
                img.awaiting_clarification = True
                img.awaiting_album = False
                img.clarification_note = f"Undo: {'; '.join(undone)}" if undo_steps else instruction
                img.clarification_question = result.question
                img.claude_session_id = result.session_id
                log.info("revising %s needs clarification; asked on the photo", old_asset_id)
                return
            new_asset_id = self.immich.upload_asset(result.output_path, f"{lineage_id}.jpg")
            self.immich.add_assets_to_album(target_album, [new_asset_id])
            if img.home != "review":
                self.immich.set_favorite(new_asset_id, True)
            if new_asset_id != old_asset_id:
                # Immich answers an upload of identical bytes with the id of
                # the copy it already has. Deleting "the old one" then would
                # delete the only copy (and a force delete skips the trash),
                # so only remove the previous version when the upload really
                # produced a new asset -- and into the trash, not for good,
                # so a revision someone didn't like can still be recovered.
                self.immich.delete_assets([old_asset_id], force=False)
            applied = (
                f"Undid: {_short('; '.join(undone))}. Reply \"undo\" again to take back the one before."
                if undo_steps else f"Applied: {note}"
            )
            posted = self.immich.post_comment(applied, album_id=target_album, asset_id=new_asset_id)
            self._mark_own(img, posted)
            img.current_asset_id = new_asset_id
            img.revision_notes = list(kept) if undo_steps else (
                img.revision_notes + [instruction[:MAX_REVISION_NOTE_CHARS]])[-MAX_REVISION_NOTES:]
            img.awaiting_clarification = False
            img.awaiting_album = False
            img.clarification_note = img.clarification_question = None
            log.info("revised %s -> %s (%d adjustment(s) so far)", old_asset_id, new_asset_id, len(img.revision_notes))
            if not undo_steps:
                self._propose_lesson(lineage_id, new_asset_id, target_album, result)
        finally:
            _cleanup(tmp_dir)

    # Drop tracking for anything manually deleted from everywhere.
    def _reap_deleted(self, state: PipelineState) -> None:
        all_current_ids: set[str] = set()
        try:
            all_current_ids |= {a.id for a in self.immich.list_album_assets(self.cfg.review_album_id)}
            for album_id in state.watched_albums.values():
                all_current_ids |= {a.id for a in self.immich.list_album_assets(album_id)}
        except ImmichError:
            # An incomplete listing would make photos look deleted and
            # drop them from tracking, so skip reaping this cycle.
            log.exception("could not list every album; skipping the cleanup of deleted photos this cycle")
            return
        for lineage_id, img in list(state.images.items()):
            if img.current_asset_id and img.current_asset_id not in all_current_ids:
                if img.home not in ("collage_maker_wait", "awaiting_clarification"):
                    del state.images[lineage_id]


def _note_with_history(history: list[str], instruction: str) -> str:
    """The instruction for a recipe run, with the adjustments already made
    to this photo in front of it. Each run starts again from the original
    photo(s), so earlier adjustments have to be restated or they are lost;
    an instruction like "swap the first two" only makes sense relative to
    the arrangement the earlier ones produced. With no history the
    instruction is passed through unchanged."""
    if not history:
        return instruction
    steps = " ".join(f"{i}) {note}" for i, note in enumerate(history, 1))
    return (
        "This image has already been adjusted at the reviewer's request, in this order, "
        f"each on top of the previous: {steps} "
        "Reproduce all of those adjustments (a rearranged or swapped layout stays as "
        "arranged unless a later step changes it), then apply this new request on top of "
        f"the result: {instruction}"
    )


def _replay_note(history: list[str]) -> str | None:
    """The instruction for a re-run that should reproduce exactly the
    adjustments in `history` and nothing more (an undo). None when there is
    nothing to replay: the photo is processed as it was the first time."""
    if not history:
        return None
    steps = " ".join(f"{i}) {note}" for i, note in enumerate(history, 1))
    return (
        "This image is being redone at the reviewer's request with these adjustments, in this "
        f"order, each on top of the previous: {steps} "
        "Apply exactly those and nothing else."
    )


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _cleanup(tmp_dir: str) -> None:
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
