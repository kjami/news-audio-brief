"""
Upload today's MP3 to a Google Cloud Storage bucket and delete yesterday's.

Auth: reuses the same GOOGLE_SERVICE_ACCOUNT_JSON as Gemini TTS (one service
account for the whole pipeline). The bucket name comes from env var
GCS_BUCKET. Enable the Cloud Storage API on the project and grant the
service account the "Storage Object Admin" role on the bucket.

Why GCS instead of Drive: Drive service-account uploads fail with
'storageQuotaExceeded' on personal Google accounts because service accounts
have no personal Drive quota. GCS uses the project's own storage quota
(free tier covers 5 GB-months — we use ~60 MB-months), so the same
service account works without any ownership-delegation dance.

Workflow:
  1. List blobs in the bucket with prefix 'brief-'
  2. Delete any whose name isn't today's (brief-YYYY-MM-DD.mp3)
  3. Upload today's MP3
  4. Return the public URL (bucket must have allUsers:objectViewer)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account

log = logging.getLogger(__name__)

FILE_PREFIX = "brief-"
FILE_SUFFIX = ".mp3"
GCS_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]


def _build_storage_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    # Lazy import so the SDK is only loaded when this module actually runs
    from google.cloud import storage

    creds = service_account.Credentials.from_service_account_info(
        info, scopes=GCS_SCOPES,
    )
    return storage.Client(credentials=creds, project=info.get("project_id"))


def _cleanup_old_briefs(bucket, today_name: str) -> int:
    """Delete any brief-*.mp3 in the bucket whose name != today_name."""
    deleted = 0
    for blob in bucket.list_blobs(prefix=FILE_PREFIX):
        name = blob.name
        if not name.endswith(FILE_SUFFIX):
            continue
        if name == today_name:
            log.info("  Found today's brief already in bucket: %s (will be replaced)",
                     name)
            try:
                blob.delete()
                deleted += 1
            except Exception as e:
                log.warning("  Failed to delete existing %s: %s", name, e)
            continue
        try:
            blob.delete()
            log.info("  Deleted old brief: %s", name)
            deleted += 1
        except Exception as e:
            log.warning("  Failed to delete %s: %s", name, e)
    return deleted


def _upload_blob(bucket, local_path: Path) -> str:
    """Upload with one retry. Returns the public URL."""
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            blob = bucket.blob(local_path.name)
            blob.content_type = "audio/mpeg"
            blob.upload_from_filename(str(local_path))
            # Deterministic public URL — works when bucket has
            # allUsers:objectViewer IAM binding
            return f"https://storage.googleapis.com/{bucket.name}/{local_path.name}"
        except Exception as e:
            last_err = e
            if attempt == 1:
                log.warning("Upload failed (attempt 1): %s — retrying", e)
                time.sleep(2)
            else:
                log.error("Upload failed (attempt 2): %s", e)
                raise
    raise RuntimeError(f"Upload failed: {last_err}")


def run(mp3_path: Path, config: dict) -> str:
    """
    Upload mp3_path to the configured GCS bucket, delete any other
    brief-*.mp3 in the bucket, and return the public URL.
    """
    mp3_path = Path(mp3_path)
    if not mp3_path.exists():
        raise FileNotFoundError(f"MP3 not found: {mp3_path}")

    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET is not set")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_name = f"{FILE_PREFIX}{today}{FILE_SUFFIX}"

    log.info("GCS: authenticating service account")
    client = _build_storage_client()
    bucket = client.bucket(bucket_name)

    log.info("GCS: cleaning up previous briefs in bucket %s", bucket_name)
    n_deleted = _cleanup_old_briefs(bucket, today_name)
    log.info("  %d old object(s) deleted", n_deleted)

    log.info("GCS: uploading %s (%.1f KB)",
             mp3_path.name, mp3_path.stat().st_size / 1024)
    public_url = _upload_blob(bucket, mp3_path)
    log.info("  Upload complete")

    log.info("Public URL: %s", public_url)
    return public_url
