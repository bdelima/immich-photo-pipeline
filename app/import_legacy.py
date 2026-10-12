"""One-time import of the old pipeline's state (state.json) into the library.

The old state was built around Immich comments and likes. The few photos that
went through it and turned out well are carried over:

  * each processed photo becomes a Photo with the same id, in the same home
    (Review or the managed album it was in);
  * the photo in the old `live_source_album` is marked as promoted to Live;
  * the current image is downloaded into the revision store as revision 0
    and uploaded as a new Immich asset owned by the account this runs as, so
    the pipeline owns it from now on;
  * for photos the old pipeline made itself, the original(s) are copied into
    the store too, so they can be reprocessed (a photo that was already
    finished when the old pipeline first saw it has no original, as before);
  * the instructions the old pipeline recorded are kept as `legacy_notes`.
    The intermediate images never existed as files, so they can be seen but
    not reverted to.

Anything the old pipeline was still waiting on (a lone portrait, a recipe
question) is not carried over: the originals are still in their entry queue.

Safe to run twice: photos already in the library are skipped. Run inside the
container, like app/import_cli.py. It is a dry run unless --apply is given:

    docker exec immich-photo-pipeline python -m app.import_legacy
    docker exec immich-photo-pipeline python -m app.import_legacy --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .immich_client import ImmichClient, ImmichError
from .library import REVIEW, STATUS_READY, LibraryStore, Photo, Revision, Source
from .revisions import RevisionStore
from .secrets import resolve_secret

log = logging.getLogger(__name__)

# Homes the old pipeline used for photos it had not finished with.
_UNFINISHED_HOMES = ("collage_maker_wait", "awaiting_clarification")


@dataclass
class LegacyReport:
    dry_run: bool
    imported: list[str] = field(default_factory=list)
    already_there: list[str] = field(default_factory=list)
    skipped_unfinished: list[str] = field(default_factory=list)
    # (photo id, reason): left out, safe to re-run.
    failed: list[tuple[str, str]] = field(default_factory=list)
    # Sources that could not be copied (the photo is still imported, but
    # cannot be reprocessed until it has an original).
    missing_sources: list[tuple[str, str]] = field(default_factory=list)
    # Album name -> "reused" or "created".
    albums: dict[str, str] = field(default_factory=dict)
    live: list[str] = field(default_factory=list)


def read_legacy_state(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _home(raw_home: str) -> str:
    return REVIEW if raw_home == "review" else raw_home


def import_legacy(
    immich: ImmichClient, store: LibraryStore, revisions: RevisionStore,
    legacy: dict[str, Any], *, apply: bool,
) -> LegacyReport:
    report = LegacyReport(dry_run=not apply)
    live_album = legacy.get("live_source_album")
    existing = store.load()

    # Albums: reuse one this account owns, otherwise make one of its own,
    # since this account could not change an album that belongs to another.
    albums: dict[str, str] = {}
    me = immich.get_my_user_id()
    for name, album_id in sorted((legacy.get("watched_albums") or {}).items()):
        if name in existing.albums:
            report.albums[name] = "reused"
            albums[name] = existing.albums[name]
            continue
        owned = False
        try:
            owned = immich.get_album(album_id).get("ownerId") == me
        except ImmichError:
            pass
        if owned:
            report.albums[name] = "reused"
            albums[name] = album_id
        else:
            report.albums[name] = "created"
            albums[name] = immich.create_album(name) if apply else ""

    for photo_id, data in sorted((legacy.get("images") or {}).items()):
        home = data.get("home", REVIEW)
        current_asset = data.get("current_asset_id")
        if not current_asset or home in _UNFINISHED_HOMES:
            report.skipped_unfinished.append(photo_id)
            continue
        if photo_id in existing.photos:
            report.already_there.append(photo_id)
            continue
        if not apply:
            report.imported.append(photo_id)
            if _home(home) == live_album:
                report.live.append(photo_id)
            continue
        try:
            photo = _import_one(immich, revisions, photo_id, data, report)
        except (ImmichError, OSError) as exc:
            revisions.purge(photo_id)
            report.failed.append((photo_id, str(exc)))
            continue
        photo.live = photo.home == live_album
        if photo.live:
            report.live.append(photo_id)

        def add(library, photo=photo):
            library.photos[photo.id] = photo
            for name, album_id in albums.items():
                if album_id:
                    library.albums.setdefault(name, album_id)

        store.update(add)
        report.imported.append(photo_id)
    return report


def _import_one(
    immich: ImmichClient, revisions: RevisionStore, photo_id: str, data: dict[str, Any],
    report: LegacyReport,
) -> Photo:
    imported = bool(data.get("imported"))
    source_ids = list(data.get("source_asset_ids") or [])
    tmp = tempfile.mkdtemp(prefix="import-legacy-")
    try:
        sources: list[Source] = []
        if not imported:
            for index, asset_id in enumerate(source_ids):
                try:
                    path = immich.download_asset_original(asset_id, tmp)
                except ImmichError:
                    report.missing_sources.append((photo_id, asset_id))
                    sources.append(Source(asset_id=asset_id))
                    continue
                sources.append(Source(
                    asset_id=asset_id,
                    file=revisions.save_source(photo_id, index, path, os.path.basename(path)),
                    name=os.path.basename(path),
                ))
        else:
            sources = [Source(asset_id=a) for a in source_ids]
        current_path = immich.download_asset_original(data["current_asset_id"], tmp)
        rel, digest = revisions.save_revision(photo_id, 0, current_path)
        new_asset = immich.upload_asset(current_path, f"{photo_id}{os.path.splitext(current_path)[1] or '.jpg'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Photo(
        id=photo_id,
        kind="collage" if len(source_ids) > 1 else "single",
        sources=sources,
        revisions=[Revision(n=0, parent=None, file=rel, sha256=digest, origin="legacy")],
        current=0,
        home=_home(data.get("home", REVIEW)),
        status=STATUS_READY,
        immich_asset_id=new_asset,
        legacy_notes=list(data.get("revision_notes") or []),
        imported=imported,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_legacy",
        description="Carry the old pipeline's processed photos into the library. Dry run unless --apply.",
    )
    parser.add_argument("--legacy", help="the old state file (default: STATE_PATH)")
    parser.add_argument("--apply", action="store_true", help="actually import; without it nothing changes")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    legacy_path = args.legacy or cfg.state_path
    if not os.path.isfile(legacy_path):
        print(f"no old state file at {legacy_path}", file=sys.stderr)
        return 2
    api_key = resolve_secret(cfg.secrets_file, "IMMICH_API_KEY", cfg.immich_api_key)
    if not api_key:
        print(f"no Immich API key: set IMMICH_API_KEY or add it to {cfg.secrets_file}", file=sys.stderr)
        return 2
    immich = ImmichClient(cfg.immich_url, api_key)
    try:
        report = import_legacy(
            immich, LibraryStore(cfg.library_path), RevisionStore(cfg.revisions_path),
            read_legacy_state(legacy_path), apply=args.apply,
        )
    except (ImmichError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    verb = "would import" if report.dry_run else "imported"
    print(f"{verb} {len(report.imported)} photo(s); already in the library: {len(report.already_there)}; "
          f"not finished in the old pipeline (left in their entry queue): {len(report.skipped_unfinished)}")
    for name, what in report.albums.items():
        print(f"album {name!r}: {'reused' if what == 'reused' else 'a new one will be made (the old one belongs to another account)' if report.dry_run else 'new one made (the old one belongs to another account)'}")
    if report.live:
        print(f"promoted to Live: {len(report.live)}")
    for photo_id, asset_id in report.missing_sources:
        print(f"  {photo_id}: original {asset_id} could not be copied (it cannot be reprocessed until it has one)")
    for photo_id, reason in report.failed:
        print(f"  failed {photo_id}: {reason}")
    if report.dry_run:
        print("dry run only -- re-run with --apply to make this change")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
