"""Release-pinned caller for the privileged home-copy artifact."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from orchestrator.ec2_home_copy import (
    artifact_path, Ec2HomeCopyError, run_root_copy, verify_root_artifact,
)


def _artifact_digest():
    return hashlib.sha256(Path(__file__).with_name("ec2_home_copy.py").read_bytes()).hexdigest()


async def preflight_helper():
    digest = _artifact_digest()
    path = artifact_path(digest)
    verify_root_artifact(path, digest)
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "-n", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
        "/usr/bin/python3.11", "-I", path, "--preflight",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode or output.strip() != b"EC2_HOME_COPY_READY":
        raise Ec2HomeCopyError("service user cannot execute approved home-copy helper")


async def copy_home(operation):
    receipt = await run_root_copy(operation, artifact_sha256=_artifact_digest())
    manifest = receipt.get("manifest")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    if (receipt.get("source_identity") != operation or receipt.get("operation") != operation["operation"]
            or not isinstance(manifest, dict) or receipt.get("manifest_sha256") != digest):
        raise Ec2HomeCopyError("copy receipt does not prove requested source and home")
    return receipt


async def verify_home(operation, receipt, target_id, target_image):
    verified = await run_root_copy({"action": "verify", "operation": operation, "receipt": receipt,
                                   "target_id": target_id, "target_image": target_image},
                                  artifact_sha256=_artifact_digest())
    if (verified.get("operation") != operation["operation"] or verified.get("target_id") != target_id
            or verified.get("target_image") != target_image
            or verified.get("manifest_sha256") != receipt["manifest_sha256"]):
        raise Ec2HomeCopyError("postboot verification receipt identity changed")
    return verified
