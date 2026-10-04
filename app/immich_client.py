"""Thin wrapper over the Immich REST API.

Covers exactly what the pipeline's state machine needs: listing an album's
assets (via /search/metadata, since GET /albums/{id} has no assets array),
album membership, favorite (like) state, activities (comments), asset
upload/delete, and album creation/listing. Nothing here is Immich-UI-only
behavior; it's all documented REST endpoints.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)


class ImmichError(RuntimeError):
    pass


# Immich serves /original as the source file's own bytes, unconverted, so
# the extension on disk should match the source type rather than always
# assuming JPEG -- a HEIC original fed to the recipe as "whatever.jpg"
# would be mislabeled, not just cosmetically wrong. Falls back to .jpg for
# any content-type not in this table (covers the vast majority of phone
# photos, and is no worse than the previous hardcoded assumption).
_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/tiff": ".tiff",
    "image/gif": ".gif",
}


# Marks the comments the pipeline itself posts. The author can't be used to
# tell them apart: Immich's activity payload has no "is this you" flag, and
# the account whose API key the pipeline runs as is often the same account a
# person comments from -- so comparing author ids made the pipeline ignore
# that person's comments. The text prefix works for any account and across
# restarts.
PIPELINE_COMMENT_PREFIX = "\U0001F916 "


@dataclass
class Asset:
    id: str
    original_file_name: str
    is_favorite: bool
    exif_orientation: str | None = None
    # Whose account uploaded this asset (AssetResponseDto.ownerId). Only
    # used for diagnostic logging (pipeline.py's _clear_from_entry_queue) --
    # removal itself is handled by trying each configured account's API
    # key in turn, not by predicting ownership up front.
    owner_id: str = ""


@dataclass
class Comment:
    id: str
    text: str
    user_id: str
    is_own: bool


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._session.headers.update({
            "x-api-key": api_key,
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}/api{path}"
        resp = self._session.request(method, url, timeout=30, **kwargs)
        if not resp.ok:
            raise ImmichError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        if resp.content and resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return None

    # ---- albums ---------------------------------------------------------

    def list_album_assets(self, album_id: str) -> list[Asset]:
        """The only reliable way to list an album's assets: GET
        /albums/{id} has no `assets` field (confirmed against the current
        AlbumResponseDto schema), so this searches by albumIds instead."""
        page = 1
        assets: list[Asset] = []
        while True:
            body = self._request(
                "POST",
                "/search/metadata",
                json={"albumIds": [album_id], "page": page, "size": 200},
            )
            items = body.get("assets", {}).get("items", [])
            for item in items:
                assets.append(Asset(
                    id=item["id"],
                    original_file_name=item.get("originalFileName", ""),
                    is_favorite=bool(item.get("isFavorite", False)),
                    exif_orientation=(item.get("exifInfo") or {}).get("orientation"),
                    owner_id=item.get("ownerId", ""),
                ))
            next_page = body.get("assets", {}).get("nextPage")
            if not next_page:
                break
            page = int(next_page)
        return assets

    def create_album(self, name: str) -> str:
        body = self._request("POST", "/albums", json={"albumName": name})
        return body["id"]

    def list_albums(self) -> list[dict]:
        return self._request("GET", "/albums") or []

    def get_my_user_id(self) -> str:
        """The id of the account this client's API key belongs to
        (GET /users/me)."""
        return self._request("GET", "/users/me")["id"]

    def get_album(self, album_id: str) -> dict:
        return self._request("GET", f"/albums/{album_id}") or {}

    def add_album_users(self, album_id: str, user_ids: list[str], role: str = "editor") -> None:
        """Shares an album with other accounts (PUT /albums/{id}/users).
        Immich rejects users who already have access, so callers should
        pass only the missing ones -- see sharing.ensure_shared."""
        if not user_ids:
            return
        self._request(
            "PUT", f"/albums/{album_id}/users",
            json={"albumUsers": [{"userId": uid, "role": role} for uid in user_ids]},
        )

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        self._request("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    def remove_assets_from_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        self._request("DELETE", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    # ---- assets -----------------------------------------------------------

    def download_asset_original(self, asset_id: str, dest_dir: str) -> str:
        """Streams GET /assets/{id}/original into dest_dir and returns the
        written file's path. Uses this client's own API key -- an
        admin/master key has unrestricted read access to every asset
        regardless of who added it, unlike album removal, which Immich
        restricts to the account that added the asset (see pipeline.py's
        _clear_from_entry_queue for that distinction; this call has no
        such restriction so there's no fallback-to-extra-clients logic
        here).

        Goes through requests directly rather than self._request, since
        that helper assumes a JSON response; this one streams binary
        bytes to disk instead, chunked so a large original doesn't have
        to be held in memory as a single bytes object."""
        url = f"{self.base_url}/api/assets/{asset_id}/original"
        resp = self._session.get(url, timeout=60, stream=True)
        if not resp.ok:
            raise ImmichError(f"GET /assets/{asset_id}/original -> {resp.status_code}: {resp.text[:500]}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = _EXT_BY_CONTENT_TYPE.get(content_type, ".jpg")
        dest_path = os.path.join(dest_dir, f"{asset_id}{ext}")
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
        return dest_path

    def set_favorite(self, asset_id: str, favorite: bool) -> None:
        self._request("PUT", f"/assets/{asset_id}", json={"isFavorite": favorite})

    def delete_assets(self, asset_ids: list[str], force: bool = True) -> None:
        if not asset_ids:
            return
        self._request("DELETE", "/assets", json={"ids": asset_ids, "force": force})

    def upload_asset(self, file_path: str, file_name: str) -> str:
        with open(file_path, "rb") as fh:
            files = {"assetData": (file_name, fh, "application/octet-stream")}
            data = {
                "deviceAssetId": file_name,
                "deviceId": "immich-photo-pipeline",
                "fileCreatedAt": _now_iso(),
                "fileModifiedAt": _now_iso(),
            }
            body = self._request("POST", "/assets", data=data, files=files)
        return body["id"]

    # ---- activities (comments) --------------------------------------------

    def list_comments(self, *, album_id: str, asset_id: str | None = None) -> list[Comment]:
        """GET /activities requires `albumId` -- confirmed against a live
        Immich, which rejects a request without it with a 400 ("expected
        string, received undefined" at path albumId). `assetId` is an
        optional narrowing filter on top of that, so a per-asset lookup
        still has to say which album the asset is being viewed in."""
        params: dict[str, str] = {"type": "comment", "albumId": album_id}
        if asset_id:
            params["assetId"] = asset_id
        body = self._request("GET", "/activities", params=params) or []
        return [
            Comment(
                id=item["id"],
                text=item.get("comment", ""),
                user_id=item.get("user", {}).get("id", ""),
                is_own=item.get("comment", "").startswith(PIPELINE_COMMENT_PREFIX),
            )
            for item in body
        ]

    def list_like_ids(self, *, album_id: str, asset_id: str) -> list[str]:
        """Ids of the "like" activities on an asset in an album. In a shared
        album Immich's thumbs-up is an activity, not the asset's favorite
        flag (and only an asset's owner can set that flag), so this is how
        a household member's like on a photo shows up."""
        params = {"type": "like", "albumId": album_id, "assetId": asset_id}
        body = self._request("GET", "/activities", params=params) or []
        return [item["id"] for item in body]

    def post_comment(self, text: str, *, album_id: str, asset_id: str | None = None) -> str:
        """Every comment the pipeline posts starts with PIPELINE_COMMENT_PREFIX,
        which is how list_comments recognizes them as its own."""
        if not text.startswith(PIPELINE_COMMENT_PREFIX):
            text = PIPELINE_COMMENT_PREFIX + text
        payload: dict[str, Any] = {"albumId": album_id, "type": "comment", "comment": text}
        if asset_id:
            payload["assetId"] = asset_id
        body = self._request("POST", "/activities", json=payload)
        return body["id"]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
