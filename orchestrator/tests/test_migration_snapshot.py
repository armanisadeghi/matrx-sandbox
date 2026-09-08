"""Boundary tests; real EC2/Docker/S3 canary remains the release acceptance gate."""
from dataclasses import replace
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from orchestrator.migration_snapshot import preserve_home, restore_home


class ArchiveContainer:
    status = "exited"

    def reload(self):
        pass

    def get_archive(self, path):
        assert path == "/home/agent"
        # The algorithm handles bytes as an opaque Docker archive.
        return iter([b"first chunk", b"second chunk"]), {}


@mock_aws
def test_receipt_restores_exact_version_even_after_new_object_at_same_key():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="sandbox-migration-test")
    s3.put_bucket_versioning(Bucket="sandbox-migration-test", VersioningConfiguration={"Status": "Enabled"})
    receipt = preserve_home(ArchiveContainer(), s3, bucket="sandbox-migration-test", key="home.tar")
    s3.put_object(Bucket=receipt.bucket, Key=receipt.key, Body=b"different later version")
    restored = []
    target = SimpleNamespace(put_archive=lambda path, data: restored.append((path, data.read())) or True)
    restore_home(target, s3, receipt)
    assert restored == [("/home", b"first chunksecond chunk")]
    with pytest.raises(RuntimeError, match="verification failed"):
        restore_home(target, s3, replace(receipt, sha256="wrong"))
    assert len(restored) == 1  # no Docker write before verification


@mock_aws
def test_refuses_unversioned_bucket_without_reading_container_archive():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="sandbox-migration-test")
    with pytest.raises(RuntimeError, match="versioning"):
        preserve_home(ArchiveContainer(), s3, bucket="sandbox-migration-test", key="home.tar")


def test_refuses_running_container_without_contacting_s3():
    old = ArchiveContainer()
    old.status = "running"
    with pytest.raises(RuntimeError, match="stopped container"):
        preserve_home(old, None, bucket="unused", key="unused")
