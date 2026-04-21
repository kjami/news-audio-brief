"""
Slice 5 — upload today's MP3 to Google Drive and delete yesterday's.

Auth: service account JSON passed via env var GOOGLE_SERVICE_ACCOUNT_JSON
(the full JSON content as a single string). The folder to upload into is
identified by env var GDRIVE_FOLDER_ID. The service account email must have
been granted Editor access to that folder — see the README.

Workflow:
  1. List files in the folder matching brief-*.mp3
  2. Delete any whose name isn't today's (brief-YYYY-MM-DD.mp3)
  3. Upload today's MP3
  4. Set a public "anyone with link can view" permission
  5. Return the webViewLink share URL
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
FILE_PREFIX = "brief-"
FILE_SUFFIX = ".mp3"


def _build_drive_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    creds = service_account.Credentials.from_service_account_info(
        info, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _cleanup_old_briefs(service, folder_id: str, today_name: str) -> int:
    """Delete any brief-*.mp3 in the folder whose name != today_name."""
    query = (
        f"'{folder_id}' in parents and trashed = false and "
        f"name contains '{FILE_PREFIX}' and mimeType = 'audio/mpeg'"
    )
    resp = service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])

    deleted = 0
    for f in files:
        name = f["name"]
        if not (name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX)):
            continue
        if name == today_name:
            log.info("  Found today's brief already in Drive: %s (will be replaced)", name)
            try:
                service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                deleted += 1
            except Exception as e:
                log.warning("  Failed to delete existing %s: %s", name, e)
            continue
        try:
            service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
            log.info("  Deleted old brief: %s", name)
            deleted += 1
        except Exception as e:
            log.warning("  Failed to delete %s: %s", name, e)
    return deleted


def _upload_file(service, folder_id: str, local_path: Path) -> dict:
    """Upload with one retry. Returns the created file resource."""
    metadata = {"name": local_path.name, "parents": [folder_id]}
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            media = MediaFileUpload(str(local_path), mimetype="audio/mpeg",
                                    resumable=False)
            return service.files().create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            last_err = e
            if attempt == 1:
                log.warning("Upload failed (attempt 1): %s — retrying", e)
                time.sleep(2)
            else:
                log.error("Upload failed (attempt 2): %s", e)
                raise
    raise RuntimeError(f"Upload failed: {last_err}")


def _make_shareable(service, file_id: str) -> None:
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
        supportsAllDrives=True,
    ).execute()


def run(mp3_path: Path, config: dict) -> str:
    """
    Upload mp3_path to Drive, delete any other brief-*.mp3 in the folder,
    return the share URL.
    """
    mp3_path = Path(mp3_path)
    if not mp3_path.exists():
        raise FileNotFoundError(f"MP3 not found: {mp3_path}")

    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID is not set")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_name = f"{FILE_PREFIX}{today}{FILE_SUFFIX}"

    log.info("Drive: authenticating service account")
    service = _build_drive_client()

    log.info("Drive: cleaning up previous briefs in folder %s", folder_id)
    n_deleted = _cleanup_old_briefs(service, folder_id, today_name)
    log.info("  %d old file(s) deleted", n_deleted)

    log.info("Drive: uploading %s (%.1f KB)",
             mp3_path.name, mp3_path.stat().st_size / 1024)
    uploaded = _upload_file(service, folder_id, mp3_path)
    file_id = uploaded["id"]
    log.info("  Upload complete, file id = %s", file_id)

    log.info("Drive: setting anyone-with-link permission")
    _make_shareable(service, file_id)

    share_url = uploaded.get("webViewLink") or (
        f"https://drive.google.com/file/d/{file_id}/view"
    )
    log.info("Share URL: %s", share_url)
    return share_url
