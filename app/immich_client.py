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


def read_api_keys_file(path: str) -> list[str]:
    """One API key per line (blank lines and '#' comments skipped). Used
    for IMMICH_EXTRA_API_KEYS_FILE -- other household accounts' keys, tried
    as a fallback when the primary account can't remove something it
    didn't add (see pipeline.py's _clear_from_entry_queue). Returns []
    when the file doesn't exist, since having no extra accounts configured
    is the normal/default case, not an error."""
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    return [line for line in lines if line and not line.startswith("#")]


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

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        self._request("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    def remove_assets_from_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        self._request("DELETE", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    # ---- assets -----------------------------------------------------------

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

    def list_comments(self, asset_id: str | None = None, album_id: str | None = None) -> list[Comment]:
        params: dict[str, str] = {"type": "comment"}
        if asset_id:
            params["assetId"] = asset_id
        if album_id:
            params["albumId"] = album_id
        body = self._request("GET", "/activities", params=params) or []
        return [
            Comment(
                id=item["id"],
                text=item.get("comment", ""),
                user_id=item.get("user", {}).get("id", ""),
                is_own=item.get("user", {}).get("isOwner", False),
            )
            for item in body
        ]

    def post_comment(self, text: str, *, album_id: str, asset_id: str | None = None) -> str:
        payload: dict[str, Any] = {"albumId": album_id, "type": "comment", "comment": text}
        if asset_id:
            payload["assetId"] = asset_id
        body = self._request("POST", "/activities", json=payload)
        return body["id"]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
