"""The poll-cycle orchestration described in the design doc: two entry
queues, Review, and promotion into whichever managed album a reply names.

The decision functions (`plan_*`) are pure -- they take plain data in and
return a plan of what to do, with no Immich or filesystem calls -- so they
can be unit tested without a live Immich instance. `Pipeline` executes
those plans against the real clients.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Literal

from .config import Config
from .immich_client import Asset, Comment, ImmichClient, ImmichError
from .recipe_runner import RecipeResult, RecipeRunner
from .state import ImageState, PipelineState, StateStore

log = logging.getLogger(__name__)

PORTRAIT = "PORTRAIT"


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
    ):
        self.cfg = config
        self.immich = immich
        self.recipe = recipe
        self.store = store
        # One ImmichClient per extra household API key (IMMICH_EXTRA_API_KEYS),
        # tried in order after the primary when removing an entry-queue
        # original -- see _clear_from_entry_queue for why.
        self.extra_clients = list(extra_clients)

    def run_once(self) -> None:
        state = self.store.load()
        self._flow1_wallpaper(state)
        self._flow1_collage(state)
        self._flow2_review(state)
        self._flow3_managed(state)
        self._reap_deleted(state)
        self.store.save(state)

    # Flow 1a -- Wallpaper Maker: process solo, immediately.
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

    # Flow 1b -- Collage Maker: hold a singleton, group 2+.
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
            result = self.recipe.run_collage(src_paths, out_path) if collage \
                else self.recipe.run_single(src_paths[0], out_path)
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
        # Placeholder for the real download call (GET /assets/{id}/original);
        # not exercised in this PR -- see recipe_runner.py's module docstring.
        raise NotImplementedError("asset download not yet wired to a live Immich instance")

    # Flow 2 -- Review: like promotes (after naming an album), comment revises.
    def _flow2_review(self, state: PipelineState) -> None:
        review_assets = {a.id: a for a in self.immich.list_album_assets(self.cfg.review_album_id)}
        for lineage_id, img in list(state.images.items()):
            if img.home != "review" or not img.current_asset_id:
                continue
            asset = review_assets.get(img.current_asset_id)
            if asset is None:
                continue
            try:
                comments = self.immich.list_comments(asset_id=asset.id)
                acted = set(img.acted_comment_ids)
                fresh = new_comments(comments, acted)
                if asset.is_favorite and not img.awaiting_clarification:
                    self._ask_which_album(state, lineage_id, asset.id)
                    continue
                for comment in fresh:
                    answer = is_approval_reply(comment.text) if img.awaiting_clarification else None
                    if img.awaiting_clarification and answer:
                        self._promote_to_album(state, lineage_id, asset.id, answer)
                        img.acted_comment_ids.append(comment.id)
                    else:
                        self._reprocess(state, lineage_id, asset.id, comment.text, target_album=self.cfg.review_album_id)
                        img.acted_comment_ids.append(comment.id)
            except Exception:
                log.exception("failed handling Review item %s; leaving it for next cycle", lineage_id)

    def _ask_which_album(self, state: PipelineState, lineage_id: str, asset_id: str) -> None:
        names = ", ".join(state.watched_albums) or "(none yet)"
        question = (
            f"Which album should this be promoted to? Currently watched: "
            f"{names}. Reply with one of those names, or a new name to "
            f"create one."
        )
        self.immich.post_comment(question, album_id=self.cfg.review_album_id, asset_id=asset_id)
        state.images[lineage_id].awaiting_clarification = True

    def _promote_to_album(self, state: PipelineState, lineage_id: str, asset_id: str, album_name: str) -> None:
        album_id = state.watched_albums.get(album_name)
        if not album_id:
            album_id = self.immich.create_album(album_name)
            state.watched_albums[album_name] = album_id
        self.immich.add_assets_to_album(album_id, [asset_id])
        self.immich.remove_assets_from_album(self.cfg.review_album_id, [asset_id])
        img = state.images[lineage_id]
        img.home = album_name
        img.awaiting_clarification = False

    # Flow 3 -- a managed album: comment revises in place, unlike pulls to Review.
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
                    continue
                comments = self.immich.list_comments(asset_id=asset.id)
                fresh = new_comments(comments, set(img.acted_comment_ids))
                for comment in fresh:
                    self._reprocess(state, lineage_id, asset.id, comment.text, target_album=album_id)
                    img.acted_comment_ids.append(comment.id)
            except Exception:
                log.exception("failed handling managed-album item %s; leaving it for next cycle", lineage_id)

    def _reprocess(self, state: PipelineState, lineage_id: str, old_asset_id: str, note: str, target_album: str) -> None:
        img = state.images[lineage_id]
        tmp_dir = tempfile.mkdtemp(prefix="pipeline-")
        try:
            src_paths = [self._download(sid, tmp_dir) for sid in img.source_asset_ids]
            out_path = os.path.join(tmp_dir, "output.jpg")
            result = self.recipe.run_collage(src_paths, out_path, note=note) if len(src_paths) > 1 \
                else self.recipe.run_single(src_paths[0], out_path, note=note)
            if result.status == "needs_clarification":
                self.immich.post_comment(result.question or "Need more information to proceed.",
                                          album_id=target_album, asset_id=old_asset_id)
                img.awaiting_clarification = True
                img.claude_session_id = result.session_id
                return
            new_asset_id = self.immich.upload_asset(result.output_path, f"{lineage_id}.jpg")
            self.immich.add_assets_to_album(target_album, [new_asset_id])
            if img.home != "review":
                self.immich.set_favorite(new_asset_id, True)
            self.immich.delete_assets([old_asset_id])
            self.immich.post_comment(f"Applied: {note}", album_id=target_album, asset_id=new_asset_id)
            img.current_asset_id = new_asset_id
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
