"""One poll cycle: notice new originals, then make Immich match the library.

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
    ):
        self.immich = immich
        self.store = store
        self.intake = intake
        self.worker = worker
        self.live_album_id = live_album_id
        self.share_user_ids = list(share_user_ids)
        # Managed albums already shared, so each is checked once, not every cycle.
        self._shared: set[str] = set()

    def run_once(self) -> None:
        self._step("looking for new originals", self._intake)
        self._step("creating albums", lambda: ensure_home_albums(self.immich, self.store))
        self._step("sharing albums", self._share)
        self._step("updating Live", lambda: sync_live(self.immich, self.store.load(), self.live_album_id))
        self._step("updating albums", lambda: sync_albums(self.immich, self.store.load()))

    def _intake(self) -> None:
        if self.intake.scan():
            self.worker.wake()

    def _share(self) -> None:
        if not self.share_user_ids:
            return
        for album_id in self.store.load().albums.values():
            if album_id in self._shared:
                continue
            if ensure_shared(self.immich, album_id, self.share_user_ids, VIEWER):
                self._shared.add(album_id)

    @staticmethod
    def _step(what: str, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("cycle step failed (%s); will retry next cycle", what)
