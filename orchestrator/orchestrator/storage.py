"""S3 storage operations — bucket setup, user prefix management."""

from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from orchestrator.config import settings

logger = logging.getLogger(__name__)


def get_s3_client():
    return boto3.client("s3", region_name=settings.s3_region)


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
            # Check if any objects exist under this prefix
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
            if resp.get("KeyCount", 0) == 0:
                # Create marker object
                s3.put_object(Bucket=bucket, Key=f"{prefix}.keep", Body=b"")
                logger.info(f"Created storage prefix: s3://{bucket}/{prefix}")
        except ClientError as e:
            logger.error(f"Failed to ensure storage for user {user_id}: {e}")
            raise


async def get_user_storage_stats(user_id: str) -> dict:
    """Get storage usage stats for a user."""
    s3 = get_s3_client()
    bucket = settings.s3_bucket

    stats = {}
    for tier in ["hot", "cold"]:
        prefix = f"users/{user_id}/{tier}/"
        total_size = 0
        total_objects = 0

        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                total_size += obj["Size"]
                total_objects += 1

        stats[tier] = {
            "total_size_bytes": total_size,
            "total_objects": total_objects,
        }

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

    logger.info(f"Deleted {deleted} objects from s3://{bucket}/{prefix}")
    return deleted
