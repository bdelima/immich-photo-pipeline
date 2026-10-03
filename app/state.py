"""Pipeline state: the lineage/home/comment-tracking the design doc calls
for, since Immich itself exposes none of it. Plain JSON on disk, written
atomically (write-temp + rename) so a crash mid-write never corrupts it.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ImageState:
    # The *original* unprocessed asset id(s) this lineage traces back to.
    # A list because a collage has more than one source photo.
    source_asset_ids: list[str]
    # The current derived (matted) asset id, or None before first processing.
    current_asset_id: str | None = None
    # "review" | "<managed-album-name>"
    home: str = "review"
    # Comment ids already acted on, so a reprocess never repeats itself.
    acted_comment_ids: list[str] = field(default_factory=list)
    # Set while the pipeline is waiting on a reply to its own question.
    awaiting_clarification: bool = False
    # The headless Claude Code session id, so a clarification answer can
    # resume the same run instead of starting over.
    claude_session_id: str | None = None


@dataclass
class PipelineState:
    # lineage id -> ImageState. The lineage id is the first source asset's
    # id (stable even across reprocesses, since source ids never change).
    images: dict[str, ImageState] = field(default_factory=dict)
    # Album names the "which album should this promote to" question has
    # ever offered, created the first time a reply names a new one.
    watched_albums: dict[str, str] = field(default_factory=dict)  # name -> album_id
    # Which watched album is currently mirrored into Live.
    live_source_album: str | None = None


class StateStore:
    """Loads/saves a PipelineState to a single JSON file, with an in-process
    lock since the poll loop and the web UI both touch it."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> PipelineState:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> PipelineState:
        if not os.path.exists(self._path):
            return PipelineState()
        with open(self._path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        images = {
            lineage_id: ImageState(**data)
            for lineage_id, data in raw.get("images", {}).items()
        }
        return PipelineState(
            images=images,
            watched_albums=raw.get("watched_albums", {}),
            live_source_album=raw.get("live_source_album"),
        )

    def save(self, state: PipelineState) -> None:
        with self._lock:
            raw: dict[str, Any] = {
                "images": {lid: asdict(s) for lid, s in state.images.items()},
                "watched_albums": state.watched_albums,
                "live_source_album": state.live_source_album,
            }
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(raw, fh, indent=2, sort_keys=True)
                os.replace(tmp_path, self._path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
