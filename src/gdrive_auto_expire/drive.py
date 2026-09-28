"""Thin wrapper over the Drive v3 API.

The only module that talks to Google, so tests stub exactly one seam: pass a
fake ``service`` object into ``Drive`` and nothing else in the codebase needs to
know the difference.

A note worth keeping, because it is easy to rediscover the hard way: under the
``drive.file`` scope every folder in a destination path must be one this tool
created. A folder you made by hand in the Drive web UI is invisible to us — the
``files().list`` lookup comes back empty and using its id as a parent 404s.
"""

from __future__ import annotations

from typing import Any

from . import config


def status_of(exc: Exception) -> int | None:
    """HTTP status from a googleapiclient HttpError, else None."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_missing(exc: Exception) -> bool:
    """True for the 'it is already gone' responses, which we treat as success."""
    return status_of(exc) in (403, 404)


def build_service(credentials):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class Drive:
    def __init__(self, service):
        self.service = service

    # --- files -----------------------------------------------------------

    def upload(self, path, *, parent_id: str | None = None) -> dict:
        """Resumable upload; returns id, name and webViewLink."""
        from googleapiclient.http import MediaFileUpload

        metadata: dict[str, Any] = {"name": path.name}
        if parent_id:
            metadata["parents"] = [parent_id]
        media = MediaFileUpload(str(path), resumable=True)
        return (
            self.service.files()
            .create(body=metadata, media_body=media, fields="id,name,webViewLink")
            .execute()
        )

    def make_public(self, file_id: str) -> dict:
        """Grant anyone-with-link reader access; returns the permission."""
        return (
            self.service.permissions()
            .create(
                fileId=file_id,
                body={
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": False,
                },
                fields="id",
            )
            .execute()
        )

    def revoke(self, file_id: str, permission_id: str) -> str:
        """Delete the public permission.

        Returns "revoked", or "already" when the permission was gone (someone
        beat us to it) and "missing" when the file itself no longer exists.
        """
        from googleapiclient.errors import HttpError

        try:
            self.service.permissions().delete(
                fileId=file_id, permissionId=permission_id
            ).execute()
        except HttpError as exc:
            if not is_missing(exc):
                raise
            # Distinguish "permission gone" from "file gone" — they are
            # different outcomes for the record.
            return "missing" if not self.exists(file_id) else "already"
        return "revoked"

    def trash(self, file_id: str) -> str:
        """Move the file to Drive's trash.

        Trash rather than permanent delete: access is gone either way, but trash
        is recoverable for 30 days, which is the safer default for an unattended
        job acting on a fortnight-old decision.
        """
        from googleapiclient.errors import HttpError

        try:
            self.service.files().update(
                fileId=file_id, body={"trashed": True}
            ).execute()
        except HttpError as exc:
            if is_missing(exc):
                return "missing"
            raise
        return "trashed"

    def exists(self, file_id: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            self.service.files().get(fileId=file_id, fields="id").execute()
        except HttpError as exc:
            if is_missing(exc):
                return False
            raise
        return True

    # --- folders ---------------------------------------------------------

    def find_folder(self, name: str, parent_id: str = "root") -> str | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name='{escaped}' and mimeType='{config.FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed=false"
        )
        result = (
            self.service.files()
            .list(q=query, fields="files(id,name)", pageSize=10)
            .execute()
        )
        files = result.get("files") or []
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str = "root") -> str:
        created = (
            self.service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": config.FOLDER_MIME,
                    "parents": [parent_id],
                },
                fields="id",
            )
            .execute()
        )
        return created["id"]

    def ensure_folder(self, name: str, parent_id: str = "root") -> str:
        return self.find_folder(name, parent_id) or self.create_folder(name, parent_id)

    def folder_is_usable(self, folder_id: str) -> bool:
        """Whether a cached folder id still points at a live folder of ours."""
        from googleapiclient.errors import HttpError

        try:
            info = (
                self.service.files()
                .get(fileId=folder_id, fields="id,mimeType,trashed")
                .execute()
            )
        except HttpError as exc:
            if is_missing(exc):
                return False
            raise
        return info.get("mimeType") == config.FOLDER_MIME and not info.get("trashed")


def resolve_folder(drive: Drive, path: str, cfg: dict) -> str | None:
    """Resolve a slash-separated destination path to a folder id, creating what
    is missing and caching each segment in ``cfg['folder_ids']``.

    An empty path means My Drive root, for which we return None so the caller
    omits ``parents`` entirely. A cached id that no longer resolves is discarded
    and the folder recreated, so a folder deleted in the Drive UI self-heals
    rather than erroring.
    """
    segments = [s for s in str(path or "").strip("/").split("/") if s]
    if not segments:
        return None

    cache = cfg.setdefault("folder_ids", {})
    parent = "root"
    walked: list[str] = []
    for segment in segments:
        walked.append(segment)
        key = "/".join(walked)
        cached = cache.get(key)
        if cached and drive.folder_is_usable(cached):
            parent = cached
            continue
        if cached:
            cache.pop(key, None)
        parent = drive.ensure_folder(segment, parent)
        cache[key] = parent
    return parent
