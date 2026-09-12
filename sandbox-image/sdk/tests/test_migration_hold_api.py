"""The agent API stays health-only until the migration CAS marker exists."""
from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from matrx_agent.api import main as api_main


@pytest.fixture(autouse=True)
def _isolate_daemon_runtime(monkeypatch):
    """Keep each lifespan test out of the developer's real /home/agent tree."""
    cached_token = api_main._auth._AGENT_TOKEN
    monkeypatch.setattr(api_main, "collect_manifest", lambda **_kwargs: SimpleNamespace(repos=[], cloud_sync=None))
    monkeypatch.setattr(api_main, "write_manifest", lambda _manifest: None)
    monkeypatch.setattr(api_main, "_find_git_repos", lambda _home: [])
    monkeypatch.setattr(api_main, "auto_stash_all_repos", lambda *_args, **_kwargs: {})
    try:
        yield
    finally:
        # _auth reads its token once at import. Restore that real cache even if
        # a websocket test changed it, so full-suite order cannot bypass auth.
        api_main._auth._AGENT_TOKEN = cached_token


class _Checkpoint:
    def __init__(self): self.starts = 0; self.stops = 0
    async def start(self): self.starts += 1
    async def stop(self): self.stops += 1


class _Watcher:
    def __init__(self): self.starts = 0
    mode = "dormant"
    async def start(self): self.starts += 1
    async def stop(self): return None
    def get_stats(self): return {}


def test_migration_hold_defers_home_background_work_until_marker(monkeypatch, tmp_path):
    """Break caught: target health passes while startup/report/checkpoint writes retained home pre-CAS."""
    marker = tmp_path / "committed"
    activated = tmp_path / "activated"
    checkpoint = _Checkpoint()
    watcher = _Watcher()
    calls: list[str] = []
    monkeypatch.setenv("MATRX_MIGRATION_HOLD", "1")
    monkeypatch.setattr(api_main, "MIGRATION_COMMIT_MARKER", marker)
    monkeypatch.setattr(api_main, "MIGRATION_ACTIVATED_MARKER", activated)
    monkeypatch.setattr(api_main, "_checkpoint", checkpoint)
    monkeypatch.setattr(api_main, "_cloud_watcher", watcher)
    monkeypatch.setattr(api_main, "read_prior_manifest", lambda: calls.append("read") or None)
    monkeypatch.setattr(api_main, "render_report", lambda _prior: calls.append("report") or "")

    with TestClient(api_main.app) as client:
        held = client.get("/health")
        assert held.status_code == 200
        assert held.json()["migration_state"] == "held"
        assert client.get("/fs/list", params={"path": str(tmp_path)}).status_code == 503
        assert checkpoint.starts == 0
        assert calls == []

        marker.write_text("committed\n")
        deadline = time.monotonic() + 2
        while checkpoint.starts != 1:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert client.get("/health").json()["migration_state"] == "activating"
        activated.write_text("activated\n")
        while client.get("/health").json()["migration_state"] != "active":
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert checkpoint.starts == 1
        assert calls == ["read", "report"]


def test_hold_retries_transient_activation_without_duplicate_watcher(monkeypatch, tmp_path):
    """Break caught: transient post-CAS activation either stays inert or starts duplicate watchers."""
    marker, activated = tmp_path / "committed", tmp_path / "activated"

    class FlakyCheckpoint(_Checkpoint):
        async def start(self):
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("transient")

    checkpoint, watcher = FlakyCheckpoint(), _Watcher()
    monkeypatch.setenv("MATRX_MIGRATION_HOLD", "1")
    monkeypatch.setattr(api_main, "MIGRATION_COMMIT_MARKER", marker)
    monkeypatch.setattr(api_main, "MIGRATION_ACTIVATED_MARKER", activated)
    monkeypatch.setattr(api_main, "_checkpoint", checkpoint)
    monkeypatch.setattr(api_main, "_cloud_watcher", watcher)
    monkeypatch.setattr(api_main, "read_prior_manifest", lambda: None)
    monkeypatch.setattr(api_main, "render_report", lambda _prior: "")

    with TestClient(api_main.app) as client:
        marker.write_text("committed\n")
        activated.write_text("activated\n")
        deadline = time.monotonic() + 2
        while client.get("/health").json()["migration_state"] != "active":
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert checkpoint.starts == 2
        assert watcher.starts == 1


def test_normal_boot_does_not_wait_for_cloud_down_marker(monkeypatch):
    """Break caught: normal daemon readiness blocks on the watcher's marker probe."""
    class SlowWatcher(_Watcher):
        async def start(self):
            self.starts += 1
            await __import__("asyncio").sleep(60)

    checkpoint, watcher = _Checkpoint(), SlowWatcher()
    monkeypatch.delenv("MATRX_MIGRATION_HOLD", raising=False)
    monkeypatch.setattr(api_main, "_checkpoint", checkpoint)
    monkeypatch.setattr(api_main, "_cloud_watcher", watcher)
    monkeypatch.setattr(api_main, "read_prior_manifest", lambda: None)
    monkeypatch.setattr(api_main, "render_report", lambda _prior: "")

    with TestClient(api_main.app) as client:
        assert client.get("/health").status_code == 200
        assert checkpoint.starts == 1


def test_hold_rejects_direct_websocket_before_activation(monkeypatch, tmp_path):
    """Break caught: a token-bearing direct PTY socket writes home before migration commit."""
    marker, activated = tmp_path / "committed", tmp_path / "activated"
    monkeypatch.setenv("MATRX_MIGRATION_HOLD", "1")
    monkeypatch.setenv("MATRX_AGENT_TOKEN", "test-token")
    monkeypatch.setattr(api_main._auth, "_AGENT_TOKEN", "test-token")
    monkeypatch.setattr(api_main, "MIGRATION_COMMIT_MARKER", marker)
    monkeypatch.setattr(api_main, "MIGRATION_ACTIVATED_MARKER", activated)
    monkeypatch.setattr(api_main, "_checkpoint", _Checkpoint())
    monkeypatch.setattr(api_main, "_cloud_watcher", _Watcher())
    monkeypatch.setattr(api_main, "read_prior_manifest", lambda: None)
    monkeypatch.setattr(api_main, "render_report", lambda _prior: "")

    with TestClient(api_main.app) as client:
        with __import__("pytest").raises(WebSocketDisconnect) as held:
            with client.websocket_connect("/pty", headers={"X-Matrx-Agent-Token": "test-token"}):
                pass
        assert held.value.code == 1013

        marker.write_text("committed\n")
        activated.write_text("activated\n")
        deadline = time.monotonic() + 2
        while client.get("/health").json()["migration_state"] != "active":
            assert time.monotonic() < deadline
            time.sleep(0.03)
        # Once activated, the ordinary HTTP auth boundary—not the migration
        # gate—handles a real daemon route.
        assert client.get("/fs/list", params={"path": str(tmp_path)}).status_code == 401
