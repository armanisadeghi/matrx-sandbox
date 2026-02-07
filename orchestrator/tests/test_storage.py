"""Tests for S3 storage operations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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
