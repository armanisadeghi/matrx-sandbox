"""S3 storage operations — bucket setup, user prefix management."""

from __future__ import annotations

import logging
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Reusable S3 client
_s3_client = None


def get_s3_client():
    """Get or create a reusable S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=settings.s3_region)
    return _s3_client


class TierStats(TypedDict):
    total_size_bytes: int
    total_objects: int


async def validate_bucket() -> None:
    """Validate that the configured S3 bucket exists and is accessible.

    Should be called at application startup to fail fast on misconfiguration (C8).
    """
    if not settings.s3_bucket:
        logger.warning("MATRX_S3_BUCKET is not set — S3 operations will fail")
        return

    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
        logger.info("S3 bucket validated: %s", settings.s3_bucket)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404":
            raise RuntimeError(
                f"S3 bucket does not exist: {settings.s3_bucket}"
            ) from e
        elif error_code == "403":
            raise RuntimeError(
                f"Access denied to S3 bucket: {settings.s3_bucket}"
            ) from e
        else:
            raise RuntimeError(
                f"Cannot access S3 bucket '{settings.s3_bucket}': {e}"
            ) from e


async def ensure_user_storage(user_id: str) -> None:
    """Ensure S3 prefixes exist for a user's hot and cold storage.

    S3 doesn't have real directories, but we create zero-byte marker objects
    so that tools like `aws s3 ls` show the paths exist.
    """
    s3 = get_s3_client()
    bucket = settings.s3_bucket

    prefixes = [
        f"users/{user_id}/hot/",
        f"users/{user_id}/cold/",
    ]

    for prefix in prefixes:
        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
            if resp.get("KeyCount", 0) == 0:
                s3.put_object(Bucket=bucket, Key=f"{prefix}.keep", Body=b"")
                logger.info("Created storage prefix: s3://%s/%s", bucket, prefix)
        except ClientError as e:
            logger.error("Failed to ensure storage for user %s: %s", user_id, e)
            raise


async def get_user_storage_stats(user_id: str) -> dict[str, TierStats]:
    """Get storage usage stats for a user."""
    s3 = get_s3_client()
    bucket = settings.s3_bucket

    stats: dict[str, TierStats] = {}
    for tier in ["hot", "cold"]:
        prefix = f"users/{user_id}/{tier}/"
        total_size = 0
        total_objects = 0

        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                total_size += obj["Size"]
                total_objects += 1

        stats[tier] = TierStats(
            total_size_bytes=total_size,
            total_objects=total_objects,
        )

    return stats


async def cleanup_user_storage(user_id: str, tier: str = "hot") -> int:
    """Delete all objects under a user's storage prefix. Returns count deleted."""
    s3 = get_s3_client()
    bucket = settings.s3_bucket
    prefix = f"users/{user_id}/{tier}/"
    deleted = 0

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            deleted += len(objects)

    logger.info("Deleted %d objects from s3://%s/%s", deleted, bucket, prefix)
    return deleted
