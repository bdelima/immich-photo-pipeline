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
from .recipe_runner import CommentIntent, RecipeResult, RecipeRunner
from .rules import SCOPE_LABELS, RulesFull, RulesStore
from .sharing import ensure_shared
from .state import ImageState, PipelineState, StateStore

log = logging.getLogger(__name__)

PORTRAIT = "PORTRAIT"

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


def is_approval_reply(text: str) -> str | None:
    """A reply to the 'which album' question. Returns the named album, or
    None if this doesn't look like an answer to that question (e.g. it's a
    revision comment instead, which the caller should treat as one)."""
    stripped = text.strip()
    return stripped if stripped else None


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
            self._flow1_wallpaper(state)
            self._flow1_collage(state)
            self._flow2_review(state)
            self._flow3_managed(state)
            self._reap_deleted(state)
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
        verdict = self.recipe.classify_comment(comment.text)
        intent = verdict.intent
        log.info("comment %s classified as %s", comment.id, intent)
        if intent == "teach" and img.awaiting_clarification:
            intent = "revise"  # a reply to the pipeline's own question, not a new rule
        if intent == "delete":
            self._delete_reviewed_asset(state, lineage_id, asset.id, album_id=album_id)
            return True
        # State saved before awaiting_album existed has only the old flag,
        # and then it always meant the album question.
        asking_album = img.awaiting_album or (img.awaiting_clarification and not img.clarification_note)
        if in_review and asking_album:
            answer = is_approval_reply(comment.text)
            if answer:
                log.info("comment %s taken as the album name %r", comment.id, answer)
                self._promote_to_album(state, lineage_id, asset.id, answer)
                img.acted_comment_ids.append(comment.id)
                return False
        if intent == "teach":
            self._save_taught_rule(lineage_id, asset.id, album_id, verdict)
        # A reply to the recipe's own question about this photo is the
        # answer to it, not a new request and not an album name.
        answering = img.awaiting_clarification and not asking_album
        self._reprocess(
            state, lineage_id, asset.id, comment.text, target_album=album_id,
            answering=bool(answering),
        )
        img.acted_comment_ids.append(comment.id)
        return False

    # ---- reviewer-taught rules (see app/rules.py) -------------------------

    def _say(self, album_id: str, asset_id: str, text: str) -> None:
        try:
            self.immich.post_comment(text, album_id=album_id, asset_id=asset_id)
        except ImmichError:
            log.exception("could not post comment on %s", asset_id)

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
            rule_id = forget.group(1).lower()
            log.info("comment %s: forget %s", comment.id, rule_id)
            if self.rules.set_status(rule_id, "retired"):
                self._say(album_id, asset_id, f"Retired rule {rule_id}.")
            else:
                self._say(album_id, asset_id, f"I don't have a rule {rule_id}.")
            img.acted_comment_ids.append(comment.id)
            return True
        if img.awaiting_clarification:
            return False  # a yes/no here would be ambiguous with the album question
        pending = self.rules.pending_proposal_for(asset_id)
        if pending is None:
            return False
        if _YES_RE.match(comment.text):
            log.info("comment %s: yes to proposed rule %s", comment.id, pending.id)
            try:
                self.rules.set_status(pending.id, "active")
            except RulesFull as exc:
                self._say(album_id, asset_id, f"I couldn't save that as a rule: {exc}. Retire one first (reply \"forget rN\" on any photo, or use the web UI).")
            else:
                self._say(album_id, asset_id, f"Saved rule {pending.id} ({SCOPE_LABELS[pending.scope]}). Reply \"forget {pending.id}\" to undo.")
        elif _NO_RE.match(comment.text):
            log.info("comment %s: no to proposed rule %s", comment.id, pending.id)
            self.rules.set_status(pending.id, "retired")
            self._say(album_id, asset_id, "OK, I won't remember that.")
        else:
            return False
        img.acted_comment_ids.append(comment.id)
        return True

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
        case; see RecipeRunner.classify_comment, which decides
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

    def _promote_to_album(self, state: PipelineState, lineage_id: str, asset_id: str, album_name: str) -> None:
        album_id = state.watched_albums.get(album_name)
        if not album_id:
            album_id = self.immich.create_album(album_name)
            state.watched_albums[album_name] = album_id
            ensure_shared(self.immich, album_id, self.share_user_ids)
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

    # Flow 3 — a managed album: comment revises in place, unlike pulls to Review.
    def _flow3_managed(self, state: PipelineState) -> None:
        for lineage_id, img in list(state.images.items()):
            if img.home in ("review", "awaiting_clarification", "collage_maker_wait") or not img.current_asset_id:
                continue
            album_id = state.watched_albums.get(img.home)
            if not album_id:
                continue
            assets = {a.id: a for a in self.immich.list_album_assets(album_id)}
            asset = assets.get(img.current_asset_id)
            if asset is None:
                continue
            try:
                if not asset.is_favorite:
                    self.immich.add_assets_to_album(self.cfg.review_album_id, [asset.id])
                    self.immich.remove_assets_from_album(album_id, [asset.id])
                    img.home = "review"
                    log.info("%s was unfavorited in %r -> moved back to Review", asset.id, album_id)
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

    def _reprocess(
        self, state: PipelineState, lineage_id: str, old_asset_id: str, note: str, target_album: str,
        answering: bool = False,
    ) -> None:
        img = state.images[lineage_id]
        instruction = note
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
        log.info("revising %s: %r", old_asset_id, instruction)
        tmp_dir = tempfile.mkdtemp(prefix="pipeline-")
        try:
            src_paths = [self._download(sid, tmp_dir) for sid in img.source_asset_ids]
            out_path = os.path.join(tmp_dir, "output.jpg")
            rules = self._rules_for("collage" if len(src_paths) > 1 else "single")
            result = self.recipe.run_collage(src_paths, out_path, note=instruction, rules=rules) if len(src_paths) > 1 \
                else self.recipe.run_single(src_paths[0], out_path, note=instruction, rules=rules)
            if result.status == "needs_clarification":
                posted = self.immich.post_comment(result.question or "Need more information to proceed.",
                                                   album_id=target_album, asset_id=old_asset_id)
                self._mark_own(img, posted)
                img.awaiting_clarification = True
                img.awaiting_album = False
                img.clarification_note = instruction
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
            posted = self.immich.post_comment(f"Applied: {note}", album_id=target_album, asset_id=new_asset_id)
            self._mark_own(img, posted)
            img.current_asset_id = new_asset_id
            img.awaiting_clarification = False
            img.clarification_note = img.clarification_question = None
            log.info("revised %s -> %s", old_asset_id, new_asset_id)
            self._propose_lesson(lineage_id, new_asset_id, target_album, result)
        finally:
            _cleanup(tmp_dir)

    # Drop tracking for anything manually deleted from everywhere.
    def _reap_deleted(self, state: PipelineState) -> None:
        all_current_ids: set[str] = set()
        all_current_ids |= {a.id for a in self.immich.list_album_assets(self.cfg.review_album_id)}
        for album_id in state.watched_albums.values():
            all_current_ids |= {a.id for a in self.immich.list_album_assets(album_id)}
        for lineage_id, img in list(state.images.items()):
            if img.current_asset_id and img.current_asset_id not in all_current_ids:
                if img.home not in ("collage_maker_wait", "awaiting_clarification"):
                    del state.images[lineage_id]


def _cleanup(tmp_dir: str) -> None:
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
