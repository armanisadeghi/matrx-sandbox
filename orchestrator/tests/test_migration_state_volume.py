"""Private migration-state volume ownership, restart, and cleanup contracts."""
from __future__ import annotations

from types import SimpleNamespace
import subprocess

import pytest

from orchestrator.hosted_migration import HostedMigrationStateError
from orchestrator.hosted_runtime import (
    MIGRATION_STATE_DIR,
    _ACTIVATION_HOME_PREFLIGHT,
    _cleanup,
    _preflight_activation_home,
    migration_state_volume_name,
    remove_migration_state_volume,
)
from orchestrator.runtime_isolation import container_runtime_isolation


def test_restart_gate_is_outside_every_aidream_tmpfs():
    """The old /tmp marker vanished on every hosted aidream restart."""
    tmpfs = container_runtime_isolation("aidream", "hosted")["tmpfs"]
    assert all(
        MIGRATION_STATE_DIR != mount and not MIGRATION_STATE_DIR.startswith(mount.rstrip("/") + "/")
        for mount in tmpfs
    )


def test_activation_preflight_rejects_symlink_to_protected_content(tmp_path):
    home = tmp_path / "home"
    instructions = home / ".matrx" / "instructions"
    instructions.mkdir(parents=True)
    protected = instructions / "SANDBOX_LAYOUT.md"
    protected.write_text("protected\n")
    (home / ".matrx" / "session-report.md").symlink_to(protected)
    script = _ACTIVATION_HOME_PREFLIGHT.replace("/home/agent", str(home))
    result = subprocess.run(["bash", "-ec", script], text=True, capture_output=True)
    assert result.returncode == 45
    assert "contains a symlink" in result.stderr
    assert protected.read_text() == "protected\n"


@pytest.mark.asyncio
async def test_activation_preflight_preserves_specific_failure_reason():
    calls = []

    class Target:
        def exec_run(self, command, **kwargs):
            calls.append((command, kwargs))
            return 43, b"required lifecycle file is not agent-writable: /home/agent/.matrx/session-report.md\n"

    with pytest.raises(HostedMigrationStateError, match="session-report.md.*original sandbox is unchanged"):
        await _preflight_activation_home(Target())
    assert calls[0][1] == {"user": "agent"}


@pytest.mark.asyncio
async def test_cleanup_removes_only_exact_unconsumed_labelled_state_volume():
    sandbox_id = "sbx-0123456789ab"
    name = migration_state_volume_name(sandbox_id)

    class Volume:
        attrs = {"Driver": "local", "Labels": {
            "matrx.owner": "orchestrator", "matrx.kind": "migration-state",
            "matrx.sandbox_id": sandbox_id,
        }}
        removed = False
        def reload(self): pass
        def remove(self): self.removed = True

    volume = Volume()
    client = SimpleNamespace(
        volumes=SimpleNamespace(get=lambda identity: volume if identity == name else None),
        containers=SimpleNamespace(list=lambda **_kwargs: []),
    )
    assert await remove_migration_state_volume(client, sandbox_id, expected_name=name) is True
    assert volume.removed is True


@pytest.mark.asyncio
async def test_cleanup_refuses_a_state_volume_with_a_consumer():
    sandbox_id = "sbx-0123456789ab"
    name = migration_state_volume_name(sandbox_id)
    volume = SimpleNamespace(
        attrs={"Driver": "local", "Labels": {
            "matrx.owner": "orchestrator", "matrx.kind": "migration-state",
            "matrx.sandbox_id": sandbox_id,
        }},
        reload=lambda: None,
        remove=lambda: (_ for _ in ()).throw(AssertionError("must remain")),
    )
    client = SimpleNamespace(
        volumes=SimpleNamespace(get=lambda _identity: volume),
        containers=SimpleNamespace(list=lambda **_kwargs: [object()]),
    )
    with pytest.raises(HostedMigrationStateError, match="still has a container consumer"):
        await remove_migration_state_volume(client, sandbox_id, expected_name=name)


@pytest.mark.asyncio
async def test_creation_intent_recovers_lost_ack_without_orphaning_state_or_pin():
    from docker.errors import NotFound

    sandbox_id = "sbx-0123456789ab"
    name = migration_state_volume_name(sandbox_id)
    helper_pin = "matrx-migration-helper:" + "a" * 32

    class Volume:
        attrs = {"Driver": "local", "Labels": {
            "matrx.owner": "orchestrator", "matrx.kind": "migration-state",
            "matrx.sandbox_id": sandbox_id,
        }}
        removed = False
        def reload(self): pass
        def remove(self): self.removed = True

    volume = Volume()
    client = SimpleNamespace(
        volumes=SimpleNamespace(get=lambda identity: volume if identity == name else (_ for _ in ()).throw(NotFound("volume"))),
        containers=SimpleNamespace(list=lambda **_kwargs: []),
        images=SimpleNamespace(get=lambda _identity: (_ for _ in ()).throw(NotFound("image"))),
    )
    writes = []
    record = {
        "sandbox_id": sandbox_id, "old_id": "", "helper_image": "sha256:" + "c" * 64,
        "helper_image_pin": helper_pin,
        "helper_image_pin_creation_intent": {"pin": helper_pin, "image": "sha256:" + "c" * 64},
        "state_volume_name": name,
        "state_volume_creation_intent": {"name": name, "sandbox_id": sandbox_id},
        "old_state_volume": None,
    }
    await _cleanup(record, client, SimpleNamespace(write=lambda value: writes.append(dict(value))), committed=False)
    assert volume.removed is True
    assert record["cleanup_receipt"]["helper_image_pin_never_created"] == helper_pin
    assert record["cleanup_receipt"]["migration_state_volume_removed"] == name
    assert record["cleanup_complete"] is True


@pytest.mark.asyncio
async def test_helper_pin_creation_intent_recovers_successful_tag_with_lost_receipt():
    """A Docker tag success followed by a journal-write crash remains removable."""
    sandbox_id = "sbx-0123456789ab"
    helper_image = "sha256:" + "c" * 64
    helper_pin = "matrx-migration-helper:" + "a" * 32
    remove_calls = []

    pinned = SimpleNamespace(id=helper_image, tags=[helper_pin], attrs={"RepoTags": [helper_pin]})
    client = SimpleNamespace(
        volumes=SimpleNamespace(),
        containers=SimpleNamespace(list=lambda **_kwargs: []),
        images=SimpleNamespace(
            get=lambda identity: pinned if identity == helper_pin else None,
            remove=lambda identity, **kwargs: remove_calls.append((identity, kwargs)),
        ),
    )
    writes = []
    record = {
        "sandbox_id": sandbox_id, "old_id": "", "helper_image": helper_image,
        "helper_image_pin": helper_pin,
        "helper_image_pin_creation_intent": {"pin": helper_pin, "image": helper_image},
    }

    await _cleanup(
        record, client, SimpleNamespace(write=lambda value: writes.append(dict(value))),
        committed=False,
    )

    expected_intent = {"pin": helper_pin, "image": helper_image}
    assert record["cleanup_receipt"]["helper_image_pin_removal_intent"] == expected_intent
    assert record["cleanup_receipt"]["helper_image_pin_removed"] == helper_pin
    assert remove_calls == [(helper_pin, {"noprune": True, "force": False})]
    assert len(writes) >= 2
