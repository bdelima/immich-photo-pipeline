"""Command-line front end for app/importer.py. Run inside the running
container (it reads the same env vars and shared secrets file as the
service itself):

    docker exec immich-photo-pipeline python -m app.import_cli \\
        --from "Screensaver" --into "Everyday"            # dry run
    docker exec immich-photo-pipeline python -m app.import_cli \\
        --from "Screensaver" --into "Everyday" --apply    # do it

--into takes a managed album name (created if missing) or the literal
word `review` to drop the photos into Review instead.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .immich_client import ImmichClient, ImmichError
from .importer import REVIEW, import_existing
from .library import LibraryStore
from .revisions import RevisionStore
from .secrets import resolve_secret


def resolve_source_album(albums: list[dict], ref: str) -> tuple[str, str]:
    """Matches `ref` against album ids first, then exact album names.
    Returns (id, name); raises ValueError on no match or an ambiguous name."""
    for album in albums:
        if album.get("id") == ref:
            return album["id"], album.get("albumName", ref)
    matches = [a for a in albums if a.get("albumName") == ref]
    if not matches:
        raise ValueError(f"no album named or with id {ref!r}")
    if len(matches) > 1:
        ids = ", ".join(a["id"] for a in matches)
        raise ValueError(f"several albums are named {ref!r} ({ids}); pass the album id instead")
    return matches[0]["id"], matches[0]["albumName"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_cli",
        description="Bring already-processed photos into the library (no lineage). Dry run unless --apply.",
    )
    parser.add_argument("--from", dest="source", required=True, help="source album name or id")
    parser.add_argument("--into", dest="target", required=True,
                        help="managed album name (created if missing), or 'review'")
    parser.add_argument("--apply", action="store_true", help="actually import; without it nothing changes")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    api_key = resolve_secret(cfg.secrets_file, "IMMICH_API_KEY", cfg.immich_api_key)
    if not api_key:
        print(f"no Immich API key: set IMMICH_API_KEY or add it to {cfg.secrets_file}", file=sys.stderr)
        return 2
    immich = ImmichClient(cfg.immich_url, api_key)
    try:
        source_id, source_name = resolve_source_album(immich.list_albums(), args.source)
        target = REVIEW if args.target.strip().lower() == REVIEW else args.target.strip()
        report = import_existing(
            immich, LibraryStore(cfg.library_path), RevisionStore(cfg.revisions_path),
            source_album_id=source_id, source_album_name=source_name,
            target=target, apply=args.apply,
        )
    except (ImmichError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    verb = "would import" if report.dry_run else "imported"
    print(f"{verb} {len(report.imported_ids)} photo(s) from {source_name!r} into {target!r}")
    print(f"already tracked (skipped): {len(report.already_tracked)}")
    if report.failed:
        print(f"failed (skipped, safe to re-run): {len(report.failed)}")
        for asset_id, reason in report.failed:
            print(f"  {asset_id}: {reason}")
    if report.dry_run:
        print("dry run only -- re-run with --apply to make this change")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
