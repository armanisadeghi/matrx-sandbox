"""Tests for S3 storage operations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


@pytest.fixture(autouse=True)
def reset_s3_client():
    """Reset the cached S3 client between tests."""
    from orchestrator import storage
    storage._s3_client = None
    yield
    storage._s3_client = None


@pytest.mark.asyncio
async def test_ensure_user_storage_creates_markers():
    """ensure_user_storage should create .keep markers for new users."""
    with patch("orchestrator.storage.get_s3_client") as mock_get:
        s3 = MagicMock()
        mock_get.return_value = s3

        # Simulate empty prefixes (new user)
        s3.list_objects_v2.return_value = {"KeyCount": 0}

        with patch("orchestrator.storage.settings") as mock_settings:
            mock_settings.s3_bucket = "test-bucket"
            mock_settings.s3_region = "us-east-1"

            from orchestrator.storage import ensure_user_storage
            await ensure_user_storage("user-123")

        # Should have created markers for both hot and cold
        assert s3.put_object.call_count == 2
        calls = s3.put_object.call_args_list
        keys = [c.kwargs["Key"] for c in calls]
        assert "users/user-123/hot/.keep" in keys
        assert "users/user-123/cold/.keep" in keys


@pytest.mark.asyncio
async def test_validate_bucket_empty_name(caplog):
    """validate_bucket with empty bucket name should log a warning, not raise."""
    with patch("orchestrator.storage.settings") as mock_settings:
        mock_settings.s3_bucket = ""

        from orchestrator.storage import validate_bucket

        import logging
        with caplog.at_level(logging.WARNING):
            await validate_bucket()

        assert "MATRX_S3_BUCKET is not set" in caplog.text


@pytest.mark.asyncio
async def test_validate_bucket_nonexistent_raises():
    """validate_bucket should raise RuntimeError for a non-existent bucket."""
    with patch("orchestrator.storage.get_s3_client") as mock_get:
        s3 = MagicMock()
        mock_get.return_value = s3

        error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
        s3.head_bucket.side_effect = ClientError(error_response, "HeadBucket")

        with patch("orchestrator.storage.settings") as mock_settings:
            mock_settings.s3_bucket = "nonexistent-bucket"
            mock_settings.s3_region = "us-east-1"

            from orchestrator.storage import validate_bucket

            with pytest.raises(RuntimeError, match="does not exist"):
                await validate_bucket()


@pytest.mark.asyncio
async def test_get_user_storage_stats_returns_correct_structure():
    """get_user_storage_stats should return dict with hot and cold tier stats."""
    with patch("orchestrator.storage.get_s3_client") as mock_get:
        s3 = MagicMock()
        mock_get.return_value = s3

        # Create a mock paginator that returns pages
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator

        # Hot tier: 2 objects totalling 1500 bytes
        # Cold tier: 1 object of 5000 bytes
        def paginate_side_effect(Bucket, Prefix):
            if "hot" in Prefix:
                return [
                    {"Contents": [
                        {"Key": "users/u1/hot/a.txt", "Size": 500},
                        {"Key": "users/u1/hot/b.txt", "Size": 1000},
                    ]},
                ]
            else:
                return [
                    {"Contents": [
                        {"Key": "users/u1/cold/big.bin", "Size": 5000},
                    ]},
                ]

        paginator.paginate.side_effect = paginate_side_effect

        with patch("orchestrator.storage.settings") as mock_settings:
            mock_settings.s3_bucket = "test-bucket"

            from orchestrator.storage import get_user_storage_stats
            stats = await get_user_storage_stats("u1")

        assert "hot" in stats
        assert "cold" in stats
        assert stats["hot"]["total_size_bytes"] == 1500
        assert stats["hot"]["total_objects"] == 2
        assert stats["cold"]["total_size_bytes"] == 5000
        assert stats["cold"]["total_objects"] == 1


@pytest.mark.asyncio
async def test_cleanup_user_storage_deletes_objects():
    """cleanup_user_storage should delete all objects under the prefix."""
    with patch("orchestrator.storage.get_s3_client") as mock_get:
        s3 = MagicMock()
        mock_get.return_value = s3

        paginator = MagicMock()
        s3.get_paginator.return_value = paginator

        # Simulate 3 objects to delete
        paginator.paginate.return_value = [
            {"Contents": [
                {"Key": "users/u1/hot/a.txt"},
                {"Key": "users/u1/hot/b.txt"},
                {"Key": "users/u1/hot/.keep"},
            ]},
        ]

        with patch("orchestrator.storage.settings") as mock_settings:
            mock_settings.s3_bucket = "test-bucket"

            from orchestrator.storage import cleanup_user_storage
            deleted = await cleanup_user_storage("u1", tier="hot")

        assert deleted == 3
        s3.delete_objects.assert_called_once()
        call_args = s3.delete_objects.call_args
        assert call_args.kwargs["Bucket"] == "test-bucket"
        objects = call_args.kwargs["Delete"]["Objects"]
        assert len(objects) == 3
