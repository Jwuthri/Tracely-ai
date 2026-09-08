"""S3 / MinIO blob store. The raw OTLP request body is the source of truth:
the API uploads it BEFORE enqueueing (mirrors Langfuse processEventBatch:
nothing is queued unless the blob is durable). The worker reads it back.
"""

from __future__ import annotations

import boto3
import structlog
from botocore.config import Config

from tracely.config import settings

log = structlog.get_logger()
_client = None


def _s3():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
    return _client


def put_blob(key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
    _s3().put_object(Bucket=settings.s3_bucket, Key=key, Body=body, ContentType=content_type)


def get_blob(key: str) -> bytes:
    return _s3().get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()


def ensure_bucket() -> None:
    """Create the configured bucket if it doesn't exist (idempotent). Run once at deploy/init time —
    a fresh MinIO/S3 host has no bucket (locally the compose `minio-init` service handles it; on
    Railway/managed S3 the backend pre-deploy step calls this)."""
    client = _s3()
    bucket = settings.s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
        return  # already there
    except Exception:
        pass
    try:
        client.create_bucket(Bucket=bucket)
        print(f"created bucket {bucket}")
    except Exception as e:  # race / already-owned / region quirk — tolerate, the bucket exists
        print(f"ensure_bucket({bucket}): {type(e).__name__} — assuming it already exists")


def event_blob_key(project_id: str, batch_id: str, content_type: str) -> str:
    ext = "pb" if "x-protobuf" in content_type else "json"
    return f"{settings.s3_event_prefix}{project_id}/otlp/{batch_id}.{ext}"


def assistant_blob_key(project_id: str, attachment_id: str) -> str:
    """Where a file dropped into the chat widget lives. Under the project's own prefix on
    purpose: `delete_project_blobs` then takes attachments with the workspace, with no second
    list of places customer bytes hide."""
    return f"{settings.s3_event_prefix}{project_id}/assistant/{attachment_id}"


def get_blob_typed(key: str) -> tuple[bytes, str]:
    """Bytes plus the content type they were stored with — what serving a file back needs."""
    obj = _s3().get_object(Bucket=settings.s3_bucket, Key=key)
    return obj["Body"].read(), obj.get("ContentType") or "application/octet-stream"


def _delete_prefix(prefix: str) -> int:
    """Delete every object under one key prefix. Returns how many went.

    Best-effort: object storage being unavailable must not block the delete (the rows are already
    gone), so failures are counted as zero rather than raised.
    """
    client = _s3()
    removed = 0
    try:
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.s3_bucket, Prefix=prefix
        ):
            batch = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if not batch:
                continue
            # delete_objects caps at 1000 keys, which is exactly one page's default maximum.
            client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": batch})
            removed += len(batch)
    except Exception as exc:
        log.warning("blob_prefix_delete_failed", prefix=prefix, error=str(exc))
    return removed


def delete_project_blobs(project_id: str, *, traces_only: bool = False) -> int:
    """Delete a project's blobs. Returns how many objects went.

    The blobs are the source of truth — the customer's payloads verbatim — so anything that
    promises to delete traces has to come through here, or the bytes outlive the thing the
    customer asked us to remove. That is not just disk: a wiped workspace whose raw OTLP bodies
    are all still in the bucket has not actually been wiped.

    Two prefixes, because a project's keys live at two depths (`event_blob_key`,
    `regression_service`): `{prefix}{project}/…` for uploads, `{prefix}fixtures/{project}/…` for
    promoted regression bundles.

    `traces_only` is the Data → wipe half: raw OTLP bodies and the fixture bundles of the cases
    it deletes, but NOT `{project}/assistant/` — chat attachments are not traces, and that wipe
    keeps the workspace's configuration. A workspace delete takes everything.
    """
    base = settings.s3_event_prefix
    project_prefix = f"{base}{project_id}/otlp/" if traces_only else f"{base}{project_id}/"
    return (
        _delete_prefix(project_prefix)
        + _delete_prefix(f"{base}fixtures/{project_id}/")
        # the durable case artifacts (`domain/regression/artifact.py`) — deleted with the cases
        + _delete_prefix(f"{base}cases/{project_id}/")
    )
