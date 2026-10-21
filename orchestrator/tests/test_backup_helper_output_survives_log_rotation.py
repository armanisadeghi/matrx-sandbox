"""The backup helper's manifests survive the host's log rotation.

Live evidence (2026-10-21): every migration of sbx-358104000663 (a ~95k-file,
7.8 GB home) rolled back at backup_intent with "helper did not emit complete
source and backup manifests". The helper returns its manifests as its LOG, and
the hosted host's daemon rotates json-file logs at 10m x 3. Measured on that
host with the real helper image: a 40 MB output came back as 5.9 MB without a
log_config, and intact (40,400,010 bytes, head and tail) with
``HELPER_LOG_CONFIG``.

The double below reproduces that measured behavior: Docker hands back a
truncated log unless the run carries one large, unrotated log file.
"""

from __future__ import annotations

import pytest

from orchestrator import hosted_backup

BIG = "HEAD\n" + ("x" * 100 + "\n") * 400_000 + "TAIL\n"  # ~40 MB, like the real manifests


def _size_bytes(value: str) -> int:
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
    return int(value[:-1]) * units[value[-1].lower()] if value[-1].lower() in units else int(value)


class _Containers:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def run(self, image, **kwargs):
        self.kwargs = kwargs
        cfg = (kwargs.get("log_config") or {}).get("config") or {}
        unrotated = cfg.get("max-file") == "1" and _size_bytes(cfg.get("max-size", "0")) >= len(BIG)
        data = BIG.encode()
        # The daemon default (10m x 3) loses part of a 40 MB log (measured: 5.9 MB back).
        return data if unrotated else data[:5_910_600]


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()


@pytest.mark.asyncio
async def test_a_large_manifest_comes_back_whole():
    client = _Client()
    out = await hosted_backup._run_helper(client, image="sha256:" + "c" * 64, volumes={}, command="true")
    assert out.startswith("HEAD") and out.rstrip().endswith("TAIL"), (
        "the helper's output was cut by log rotation — its manifests would be incomplete"
    )
    assert client.containers.kwargs["log_config"]["type"] == "json-file"
