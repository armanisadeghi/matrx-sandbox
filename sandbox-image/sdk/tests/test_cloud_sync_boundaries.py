from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from matrx_agent.cli.files import cmd_put
from matrx_agent.cloud_sync.client import BridgeConfig
from matrx_agent.cloud_sync.paths import is_system_path
from matrx_agent.cloud_sync.watcher import (
    CloudFilesWatcher,
    _is_retryable_bridge_error,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("report.md", False),
        ("projects/report.md", False),
        ("system-files/scraper/body.html", True),
        ("/generations/images/render.png", True),
        ("system-files-backup/notes.txt", False),
    ],
)
def test_system_path_boundary_is_segment_exact(path: str, expected: bool) -> None:
    assert is_system_path(path) is expected


def test_seed_hashes_never_tracks_system_managed_files(tmp_path: Path) -> None:
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "report.md").write_text("user", encoding="utf-8")
    (tmp_path / "system-files" / "scraper").mkdir(parents=True)
    (tmp_path / "system-files" / "scraper" / "body.html").write_text(
        "evidence",
        encoding="utf-8",
    )
    watcher = CloudFilesWatcher(cloud_root=tmp_path)

    watcher._seed_hashes()

    assert set(watcher._last_hash) == {"projects/report.md"}


def test_persisted_system_path_event_is_retired_without_bridge_call(
    tmp_path: Path,
) -> None:
    watcher = CloudFilesWatcher(cloud_root=tmp_path)

    asyncio.run(watcher._flush_upsert("system-files/scraper/body.html", "mem-old"))

    assert watcher._metrics.errors_total == 0


@pytest.mark.parametrize("status_code", [400, 403, 409, 422])
def test_permanent_client_rejections_are_not_retried(status_code: int) -> None:
    request = httpx.Request("PUT", "https://server.example/cloud-files/put")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("rejected", request=request, response=response)

    assert _is_retryable_bridge_error(error) is False


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_transient_bridge_failures_remain_retryable(status_code: int) -> None:
    request = httpx.Request("PUT", "https://server.example/cloud-files/put")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("temporary", request=request, response=response)

    assert _is_retryable_bridge_error(error) is True


def test_cli_refuses_system_path_before_network(tmp_path: Path) -> None:
    local = tmp_path / "body.html"
    local.write_text("changed evidence", encoding="utf-8")
    cfg = BridgeConfig(url="https://server.example", token="token", user_id="user")

    assert cmd_put(cfg, str(local), "system-files/scraper/body.html") == 2
