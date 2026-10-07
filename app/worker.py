"""Processes queued photos: copies the originals into the revision store,
runs the recipe, saves the result as revision 0, publishes it to Immich and
lands the photo in Review.

The queue is the library itself: a photo with status "processing" is waiting
for (or in the middle of) a job. Nothing else is persisted, so a restart just
picks the same photos up again, and each step records what it has done so a
resumed job continues instead of starting over:

  1. originals copied into the store (recorded per source),
  2. the recipe's result saved as revision 0 (recorded on the photo),
  3. the result uploaded to Immich (asset id recorded; status becomes ready).

Step 2 is what costs Claude plan credits, so once it is recorded a retry only
repeats the upload. A job that fails for any other reason leaves the photo
"failed" with the error and is not retried by itself, since every retry
spends credits; an Immich error that looks temporary is retried a few times.

How many photos are processed at once is `count` (WORKER_COUNT). A photo is
only ever worked on by one worker at a time.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Callable

from . import chat
from .immich_client import ImmichClient, ImmichError
from .library import (
    REVIEW, STATUS_AWAITING_ANSWER, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY,
    LibraryStore, Photo, Revision,
)
from .recipe_runner import RecipeRunner
from .revisions import RevisionStore
from .rules import RulesStore

log = logging.getLogger(__name__)

# How many times in a row an Immich error is treated as temporary for one
# photo before the photo is marked failed.
MAX_IMMICH_RETRIES = 5
# Seconds to leave a photo alone after a temporary Immich error.
RETRY_DELAY_SECONDS = 30


class Worker:
    def __init__(
        self,
        store: LibraryStore,
        revisions: RevisionStore,
        immich: ImmichClient,
        recipe: RecipeRunner,
        *,
        wallpaper_album_id: str,
        collage_album_id: str,
        extra_clients: list[ImmichClient] = (),
        rules: RulesStore | None = None,
        count: int = 1,
        can_run_recipe: Callable[[], bool] = lambda: True,
        on_change: Callable[[], None] | None = None,
    ):
        self.store = store
        self.revisions = revisions
        self.immich = immich
        self.recipe = recipe
        self.rules = rules
        self.count = max(1, count)
        self.can_run_recipe = can_run_recipe
        self.on_change = on_change
        self.extra_clients = list(extra_clients)
        self._queue_albums = {"wallpaper": wallpaper_album_id, "collage": collage_album_id}
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._immich_failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- running ---------------------------------------------------------

    def start(self) -> None:
        for i in range(self.count):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("%d worker(s) started", self.count)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did = self.run_one()
            except Exception:
                log.exception("worker error; continuing")
                did = False
            if not did:
                # Wake early for new work; otherwise look again soon, since
                # a recipe-less photo may become claimable (auth recovering).
                self._wake.wait(timeout=10)
                self._wake.clear()

    def run_one(self) -> bool:
        """Does one unit of work: a reviewer's chat message if one is waiting
        (they are answering a person, so they go first), otherwise a queued
        photo. Returns False if there was nothing to do."""
        chat_id = self._claim_chat()
        if chat_id is not None:
            try:
                if chat.run_job(self.store, self.revisions, self.recipe, self.rules, chat_id):
                    self.wake()  # a first result still has to be published
                    if self.on_change is not None:
                        self.on_change()
            finally:
                with self._lock:
                    self._active.discard(chat_id)
            return True
        photo_id = self._claim()
        if photo_id is None:
            return False
        try:
            self._process(photo_id)
        finally:
            with self._lock:
                self._active.discard(photo_id)
        return True

    def _queued(self) -> list[Photo]:
        """Photos waiting for a worker, oldest first."""
        photos = [p for p in self.store.load().photos.values() if p.status == STATUS_PROCESSING and not p.trashed]
        photos.sort(key=lambda p: (p.created_at, p.id))
        return photos

    def pending(self) -> list[str]:
        return [p.id for p in self._queued()]

    def _claim_chat(self) -> str | None:
        """The photo whose oldest chat message has waited longest, if the
        recipe can run (every message is read by Claude)."""
        if not self.can_run_recipe():
            return None
        waiting = []
        for photo in self.store.load().photos.values():
            message = chat.next_open_message(photo)
            if message is None or photo.trashed or photo.status not in (STATUS_READY, STATUS_AWAITING_ANSWER):
                continue
            waiting.append((message.at, message.id, photo.id))
        waiting.sort()
        with self._lock:
            for _, _, photo_id in waiting:
                if photo_id not in self._active:
                    self._active.add(photo_id)
                    return photo_id
        return None

    def _needs_claude(self, photo: Photo) -> bool:
        return photo.media_type == "image" and photo.current_revision() is None

    def _claim(self) -> str | None:
        photos = self._queued()
        now = time.monotonic()
        with self._lock:
            for photo in photos:
                if photo.id in self._active or self._retry_after.get(photo.id, 0) > now:
                    continue
                if self._needs_claude(photo) and not self.can_run_recipe():
                    continue
                self._active.add(photo.id)
                return photo.id
        return None

    # ---- one photo -------------------------------------------------------

    def _process(self, photo_id: str) -> None:
        log.info("processing %s", photo_id)
        try:
            self._run(photo_id)
            self._immich_failures.pop(photo_id, None)
            self._retry_after.pop(photo_id, None)
        except ImmichError as exc:
            count = self._immich_failures.get(photo_id, 0) + 1
            self._immich_failures[photo_id] = count
            if count < MAX_IMMICH_RETRIES:
                log.warning("Immich error on %s (%d/%d); will retry: %s", photo_id, count, MAX_IMMICH_RETRIES, exc)
                self._retry_after[photo_id] = time.monotonic() + RETRY_DELAY_SECONDS
                return
            self._fail(photo_id, f"Immich: {exc}")
        except Exception as exc:
            log.exception("processing %s failed", photo_id)
            self._fail(photo_id, str(exc) or exc.__class__.__name__)

    def _fail(self, photo_id: str, message: str) -> None:
        self._immich_failures.pop(photo_id, None)
        self._retry_after.pop(photo_id, None)

        def mark(photo: Photo) -> None:
            photo.status = STATUS_FAILED
            photo.error = message[:500]

        try:
            self.store.update_photo(photo_id, mark)
        except KeyError:
            pass

    def _run(self, photo_id: str) -> None:
        photo = self.store.load().photos.get(photo_id)
        if photo is None or photo.status != STATUS_PROCESSING or photo.trashed:
            return
        tmp = tempfile.mkdtemp(prefix="pipeline-")
        try:
            if photo.current_revision() is None:
                photo = self._copy_sources(photo, tmp)
                if photo.media_type == "video":
                    result_path = self.revisions.path(photo.sources[0].file)
                    instruction_rules: list[str] = []
                    session_id = None
                else:
                    outcome = self._run_recipe(photo, tmp)
                    if outcome is None:
                        return  # a question was recorded
                    result_path, instruction_rules, session_id = outcome
                self._save_first_revision(photo, result_path, instruction_rules, session_id)
                photo = self.store.load().photos[photo_id]
            self._publish(photo)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._clear_from_queue(self.store.load().photos[photo_id])

    def _copy_sources(self, photo: Photo, tmp: str) -> Photo:
        """Copies every original into the store (the contributor may delete
        theirs at any time), recording each as it is done."""
        for index, source in enumerate(photo.sources):
            if source.file and self.revisions.exists(source.file):
                continue
            path = self.immich.download_asset_original(source.asset_id, tmp)
            rel = self.revisions.save_source(photo.id, index, path, os.path.basename(path))

            def record(p: Photo, index=index, rel=rel, name=os.path.basename(path)) -> None:
                p.sources[index].file = rel
                p.sources[index].name = p.sources[index].name or name

            self.store.update_photo(photo.id, record)
        return self.store.load().photos[photo.id]

    def _run_recipe(self, photo: Photo, tmp: str) -> tuple[str, list[str], str | None] | None:
        out_path = os.path.join(tmp, "output.jpg")
        rules = self._rules_for("collage" if photo.kind == "collage" else "single")
        paths = [self.revisions.path(s.file) for s in photo.sources]
        if photo.kind == "collage":
            result = self.recipe.run_collage(paths, out_path, rules=rules)
        else:
            result = self.recipe.run_single(paths[0], out_path, rules=rules)
        if result.status == "needs_clarification":
            def ask(p: Photo) -> None:
                p.status = STATUS_AWAITING_ANSWER
                p.question = result.question or "Need more information to proceed."
                p.session_id = result.session_id

            self.store.update_photo(photo.id, ask)
            log.info("%s: the recipe asked a question", photo.id)
            return None
        return result.output_path, rules, result.session_id

    def _save_first_revision(
        self, photo: Photo, result_path: str, rules: list[str], session_id: str | None,
    ) -> None:
        n = photo.next_revision_number()
        rel, digest = self.revisions.save_revision(photo.id, n, result_path)
        origin = "original" if photo.media_type == "video" else "processed"

        def record(p: Photo) -> None:
            p.revisions.append(Revision(
                n=n, parent=None, file=rel, rules=list(rules), session_id=session_id,
                sha256=digest, origin=origin,
            ))
            p.current = n
            p.home = REVIEW

        self.store.update_photo(photo.id, record)

    def _publish(self, photo: Photo) -> None:
        """Uploads the current revision as a pipeline-owned asset."""
        if photo.immich_asset_id is None:
            rev = photo.current_revision()
            path = self.revisions.path(rev.file)
            ext = os.path.splitext(path)[1] or ".jpg"
            asset_id = self.immich.upload_asset(path, f"{photo.id}{ext}")
        else:
            asset_id = photo.immich_asset_id

        published = photo.current

        def done(p: Photo) -> None:
            p.immich_asset_id = asset_id
            p.published_revision = published
            p.status = STATUS_READY
            p.error = None
            p.question = None

        self.store.update_photo(photo.id, done)
        log.info("%s is ready in Review", photo.id)

    def _rules_for(self, kind: str) -> list[str]:
        """Active rule texts for a run of `kind`. A rules file that can't be
        read must not stop photos being processed."""
        if self.rules is None:
            return []
        try:
            return self.rules.active_texts(kind)
        except Exception:
            log.exception("could not read reviewer rules; processing without them")
            return []

    # ---- the entry queue -------------------------------------------------

    def _clear_from_queue(self, photo: Photo) -> None:
        """Removes the originals from their entry queue. Immich only lets
        whoever added an asset to an album remove it, even for the album's
        owner or an admin, so the primary key is tried first and then each
        household member's. If none can, the original stays where it is,
        harmlessly: it is already a source of a photo, so it is not picked up
        again."""
        album_id = self._queue_albums.get(photo.queue or "")
        if not album_id:
            return
        for source in photo.sources:
            if self._try_remove(album_id, source.asset_id):
                continue
            log.warning(
                "no configured Immich account could remove asset %s (owner %s) from the entry queue; "
                "it can be deleted from there by hand", source.asset_id, source.owner_id or "unknown",
            )

    def _try_remove(self, album_id: str, asset_id: str) -> bool:
        for client in (self.immich, *self.extra_clients):
            try:
                client.remove_assets_from_album(album_id, [asset_id])
                return True
            except ImmichError:
                continue
        return False
