"""One poll cycle: notice new originals, then make Immich match the library
(publish what needs publishing, fill and empty the albums, clear old copies).

Nothing here needs Claude, so it keeps running when the Claude session is
down; the worker decides for itself what it can process.

Each step is separate and best-effort: a failure is logged and the next step
still runs, and everything is retried next cycle because every step works out
what to do from the library and what Immich holds, not from what it did last
time.
"""
from __future__ import annotations

import logging

from .albums import ensure_home_albums
from .immich_client import ImmichClient
from .intake import Intake
from .library import LibraryStore
from .projection import sync_albums, sync_live
from .publish import publish_pending, tidy_stale
from .revisions import RevisionStore
from .sharing import VIEWER, ensure_shared
from .worker import Worker

log = logging.getLogger(__name__)


class Cycle:
    def __init__(
        self,
        immich: ImmichClient,
        store: LibraryStore,
        intake: Intake,
        worker: Worker,
        *,
        live_album_id: str,
        share_user_ids: list[str] = (),
        revisions: RevisionStore | None = None,
    ):
        self.immich = immich
        self.store = store
        self.intake = intake
        self.worker = worker
        self.live_album_id = live_album_id
        self.share_user_ids = list(share_user_ids)
        self.revisions = revisions
        # Managed albums already shared, so each is checked once, not every cycle.
        self._shared: set[str] = set()

    def run_once(self) -> None:
        self._step("looking for new originals", self._intake)
        self._step("creating albums", lambda: ensure_home_albums(self.immich, self.store))
        self._step("sharing albums", self._share)
        if self.revisions is not None:
            self._step("publishing images", lambda: publish_pending(self.immich, self.store, self.revisions))
        self._step("updating Live", lambda: sync_live(self.immich, self.store.load(), self.live_album_id))
        self._step("updating albums", lambda: sync_albums(self.immich, self.store.load()))
        # Last, so an asset is out of every album before it goes to the trash.
        self._step("clearing old copies", lambda: tidy_stale(self.immich, self.store))

    def _intake(self) -> None:
        if self.intake.scan():
            self.worker.wake()

    def _share(self) -> None:
        if not self.share_user_ids:
            return
        for album_id in self.store.load().albums.values():
            if album_id in self._shared:
                continue
            # convert: an output album shared as editor before this version
            # is turned into a viewer share.
            if ensure_shared(self.immich, album_id, self.share_user_ids, VIEWER, convert=True):
                self._shared.add(album_id)

    @staticmethod
    def _step(what: str, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("cycle step failed (%s); will retry next cycle", what)
