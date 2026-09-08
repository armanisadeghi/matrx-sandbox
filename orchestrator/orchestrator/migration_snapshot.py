"""Versioned, verified home snapshots for stopped S3-backed containers.

The snapshot is retained on every outcome. A successful Docker stop or an S3
sync log is not a preservation receipt. Restore reads the exact object version
and checks its bytes before making it available to the replacement container.
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotReceipt:
    bucket: str
    key: str
    version_id: str
    sha256: str
    size: int


def preserve_home(container, s3, *, bucket: str, key: str) -> SnapshotReceipt:
    container.reload()
    if container.status != "exited":
        raise RuntimeError("Home snapshot requires a stopped container")
    if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise RuntimeError("Home snapshot requires S3 bucket versioning")
    with tempfile.TemporaryFile() as archive:
        chunks, _ = container.get_archive("/home/agent")
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            archive.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        if not size:
            raise RuntimeError("Docker returned an empty home archive")
        archive.seek(0)
        # Managed multipart transfer handles homes larger than PutObject's 5GB.
        s3.upload_fileobj(archive, bucket, key, ExtraArgs={
            "ServerSideEncryption": "AES256", "Metadata": {"sha256": digest.hexdigest()},
        })
        meta = s3.head_object(Bucket=bucket, Key=key)
        version_id = meta.get("VersionId")
        if not version_id or version_id == "null":
            raise RuntimeError("S3 did not persist a versioned home snapshot")
        receipt = SnapshotReceipt(bucket, key, version_id, digest.hexdigest(), size)
        with verified_archive(s3, receipt):
            pass
        return receipt


def verified_archive(s3, receipt: SnapshotReceipt):
    archive = tempfile.TemporaryFile()
    try:
        response = s3.get_object(Bucket=receipt.bucket, Key=receipt.key, VersionId=receipt.version_id)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                archive.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        finally:
            body.close()
        if size != receipt.size or digest.hexdigest() != receipt.sha256:
            raise RuntimeError("S3 home snapshot verification failed")
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise


def restore_home(container, s3, receipt: SnapshotReceipt) -> None:
    with verified_archive(s3, receipt) as archive:
        if not container.put_archive("/home", archive):
            raise RuntimeError("Docker refused the verified home snapshot")
