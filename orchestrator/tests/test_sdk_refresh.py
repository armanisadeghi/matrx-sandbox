"""Guards for the binding-time runtime SDK refresh.

The class this closes: a box created before an SDK command existed could never
run it, because existing boxes are not force-migrated (SBX-006). These guards
fail on any build where the binding stops installing the current SDK into an
older box, where a refresh failure can reach the caller as anything other than a
diagnostic, or where a box already on the current image pays for an exec.
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import activity, sdk_refresh, versioning
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.routes import sandboxes

CURRENT_IMAGE_ID = "sha256:current"
OLD_IMAGE_ID = "sha256:august"


def _sandbox(template: str = "bare", sandbox_id: str = "sbx-old") -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=sandbox_id,
        user_id="00000000-0000-0000-0000-000000000001",
        organization_id="22222222-2222-4222-8222-222222222222",
        status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
        template=template,
    )


def _container(image_id: str, version: str = "aug17") -> MagicMock:
    container = MagicMock()
    container.status = "running"
    container.attrs = {
        "Image": image_id,
        "Config": {"Env": [f"MATRX_IMAGE_VERSION={version}"]},
    }
    container.put_archive.return_value = True
    return container


def _sdk_tar() -> bytes:
    """A payload shaped like ``docker cp`` of /opt/sandbox/sdk from the image."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        body = b"print('current')\n"
        for name in ("sdk/matrx_agent/cli/toolchain.py", "sdk/matrx_agent/api/main.py"):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clean_caches():
    sdk_refresh.clear_caches()
    yield
    sdk_refresh.clear_caches()


def _wire(monkeypatch, container, *, exec_results, current_available=True):
    client = MagicMock()
    client.containers.get.return_value = container
    holder = MagicMock()
    holder.get_archive.return_value = ([_sdk_tar()], {})
    client.containers.create.return_value = holder
    monkeypatch.setattr(sdk_refresh.sandbox_manager, "_get_docker_client", lambda: client)
    monkeypatch.setattr(
        sdk_refresh.versioning,
        "current_image",
        lambda _client, template: versioning.CurrentImage(
            template, "matrx-sandbox:core", CURRENT_IMAGE_ID if current_available else None,
            "sep14", current_available,
        ),
    )
    execute = AsyncMock(side_effect=exec_results)
    monkeypatch.setattr(sdk_refresh.sandbox_manager, "exec_in_sandbox", execute)
    return client, execute


@pytest.mark.asyncio
async def test_binding_installs_the_current_sdk_into_an_older_box(monkeypatch):
    """The gap itself: an August box gets September's CLI at binding time."""
    installer_report = json.dumps(
        {
            "action": "sdk_self_update",
            "status": "refreshed",
            "daemon_restart": {"status": "restarted"},
            "deps_checked": {"added": [], "removed": []},
            "mtx_shim": "current",
            "elapsed_seconds": 3.1,
        }
    )
    container = _container(OLD_IMAGE_ID)
    client, execute = _wire(
        monkeypatch,
        container,
        exec_results=[
            (0, "", "", "/opt/sandbox"),          # no stamp yet — never refreshed
            (0, installer_report, "", "/opt/sandbox"),
        ],
    )

    result = await sandboxes._prepare_connection(_sandbox())

    assert result is not None, "an ordinary sandbox must still get the SDK hook"
    stamped = result["sdk_refresh"]
    assert stamped["status"] == "refreshed"
    assert stamped["from"] == "aug17"
    assert stamped["to"] == "sep14"
    assert stamped["daemon_restart"] == "restarted"

    # The staged tree is unpacked beside the live SDK, never on top of it, and
    # the installer that runs is the STAGED one — not whatever the old box has.
    put_path, payload = container.put_archive.call_args[0]
    assert put_path == "/opt/sandbox"
    names = tarfile.open(fileobj=io.BytesIO(payload)).getnames()
    assert names and all(n.startswith("sdk.incoming/") for n in names), names
    # The hook must not relocate the agent's shell: the exec helper caches the
    # directory each call lands in, and that cache is the user's location.
    assert sdk_refresh.sandbox_manager._sandbox_cwd.get("sbx-old") in (None, "/home/agent")
    install_cmd = execute.await_args_list[-1].kwargs["command"]
    assert "/opt/sandbox/sdk.incoming/matrx_agent/selfupdate.py" in install_cmd
    assert "--target /opt/sandbox/sdk" in install_cmd
    assert "/home/agent" not in install_cmd
    assert "rm -rf /opt/sandbox/sdk.incoming" in install_cmd
    assert execute.await_args_list[-1].kwargs["user"] == "root"


@pytest.mark.asyncio
async def test_a_box_already_on_the_current_image_is_left_alone(monkeypatch):
    """Zero execs, zero staging — an up-to-date box must not pay for this."""
    container = _container(CURRENT_IMAGE_ID, version="sep14")
    client, execute = _wire(monkeypatch, container, exec_results=[])

    result = await sdk_refresh.refresh_sdk_if_stale(_sandbox(sandbox_id="sbx-new"))

    assert result["status"] == "current"
    assert result["to"] == "sep14"
    execute.assert_not_awaited()
    container.put_archive.assert_not_called()
    client.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_runs_once_per_box_per_image_version(monkeypatch):
    """The stamp inside the box is the rate limit: a second binding re-reads it
    and stops, rather than reinstalling on every connection."""
    container = _container(OLD_IMAGE_ID)
    stamp = json.dumps({"to_image_id": CURRENT_IMAGE_ID, "to_version": "sep14"})
    client, execute = _wire(
        monkeypatch, container, exec_results=[(0, stamp, "", "/opt/sandbox")]
    )

    first = await sdk_refresh.refresh_sdk_if_stale(_sandbox())
    second = await sdk_refresh.refresh_sdk_if_stale(_sandbox())

    assert first["status"] == "already_refreshed"
    assert first["to"] == "sep14"
    assert second["status"] == "already_refreshed"
    assert second.get("cached") is True
    assert execute.await_count == 1, "the second binding must not exec at all"
    container.put_archive.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_refresh_never_blocks_the_binding(monkeypatch):
    """Loud, non-fatal: the caller still gets a binding, with the failure on it."""
    container = _container(OLD_IMAGE_ID)
    _wire(
        monkeypatch,
        container,
        exec_results=[
            (0, "", "", "/opt/sandbox"),
            (1, "", "python3: cannot execute binary file", "/opt/sandbox"),
        ],
    )

    result = await sandboxes._prepare_connection(_sandbox())

    assert result["sdk_refresh"]["status"] == "failed"
    assert "cannot execute" in result["sdk_refresh"]["reason"]
    assert result["sdk_refresh"]["from"] == "aug17"


@pytest.mark.asyncio
async def test_a_raising_docker_layer_never_blocks_the_binding(monkeypatch):
    container = _container(OLD_IMAGE_ID)
    client, _execute = _wire(monkeypatch, container, exec_results=[])
    client.containers.get.side_effect = RuntimeError("docker socket gone")

    result = await sandboxes._prepare_connection(_sandbox())

    assert result["sdk_refresh"]["status"] == "failed"
    assert "docker socket gone" in result["sdk_refresh"]["reason"]


@pytest.mark.asyncio
async def test_an_open_terminal_defers_the_daemon_restart(monkeypatch):
    """A PTY lives inside the daemon process. An attached session must never be
    killed by a background tooling update."""
    installer_report = json.dumps(
        {
            "status": "refreshed",
            "daemon_restart": {"status": "deferred", "reason": "live terminal sessions"},
            "deps_checked": {"added": [], "removed": []},
        }
    )
    container = _container(OLD_IMAGE_ID)
    _client, execute = _wire(
        monkeypatch,
        container,
        exec_results=[
            (0, "", "", "/opt/sandbox"),
            (0, installer_report, "", "/opt/sandbox"),
        ],
    )
    monkeypatch.setattr(activity, "open_session_count", lambda _sid: 1)

    result = await sdk_refresh.refresh_sdk_if_stale(_sandbox())

    assert result["status"] == "refreshed"
    assert result["daemon_restart"] == "deferred"
    assert "--allow-daemon-restart" not in execute.await_args_list[-1].kwargs["command"]


@pytest.mark.asyncio
async def test_the_knob_turns_the_whole_hook_off(monkeypatch):
    from tests.conftest import seed_sandbox_knobs

    seed_sandbox_knobs({"sdk_refresh_on_binding": False})
    container = _container(OLD_IMAGE_ID)
    _client, execute = _wire(monkeypatch, container, exec_results=[])

    result = await sdk_refresh.refresh_sdk_if_stale(_sandbox())

    assert result["status"] == "disabled"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_migrating_box_is_left_to_its_migration(monkeypatch):
    monkeypatch.setattr(activity, "is_migrating", lambda _sid: True)
    result = await sdk_refresh.refresh_sdk_if_stale(_sandbox())
    assert result["status"] == "skipped"
