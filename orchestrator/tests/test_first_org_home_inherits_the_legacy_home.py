"""THE FIRST ORGANIZATION-KEYED HOME INHERITS THE LEGACY HOME.

On 2026-09-17 the hosted home became keyed by (user, organization) and the old
``matrx-user-<uid>`` volumes were correctly left in place. The consequence was a
silent loss from the person's seat: all 213 hosted users' NEXT sandbox mounted a
brand-new EMPTY ``/home/agent`` while their projects, repos, scratch,
``~/.matrx/session.json`` and the durable cloud-sync queue
(``/home/agent/.matrx/runtime/cloud-sync-queue.jsonl``) sat on the unmounted
legacy volume — nothing on any surface said so.

So the FIRST org-keyed home a user gets is seeded from their legacy home, once,
in one direction, before the box starts. A SECOND organization's home starts
empty on purpose: it is that tenant's view of their files, not a second copy of
the first tenant's drawer.

These tests drive the real ``ensure_user_volume`` against a fake Docker client
and hold the four lines: the copy happens exactly once for the first home, never
for a later one, never without a legacy volume, and a failed copy REFUSES the
creation, removes the new volume and leaves the legacy volume untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import NotFound

from orchestrator.storage_layout import (
    HomeInheritanceError,
    ensure_user_volume,
    inherited_from,
    legacy_user_volume_name,
    user_volume_name,
)

USER = "00000000-0000-4000-8000-000000000001"
ORG_A = "00000000-0000-4000-8000-0000000000aa"
ORG_B = "00000000-0000-4000-8000-0000000000bb"
HOST = "orchestrator-host-container"


class _FakeVolume:
    def __init__(self, store: "_FakeVolumes", name: str, labels: dict[str, str]):
        self._store = store
        self.name = name
        self.attrs = {"Name": name, "Driver": "local", "Labels": dict(labels)}

    def reload(self) -> None:
        return None

    def remove(self, force: bool = False) -> None:
        self._store.removed.append(self.name)
        self._store.volumes.pop(self.name, None)


class _FakeVolumes:
    def __init__(self) -> None:
        self.volumes: dict[str, _FakeVolume] = {}
        self.removed: list[str] = []
        self.created: list[str] = []

    def seed(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.volumes[name] = _FakeVolume(self, name, labels or {})

    def get(self, name: str) -> _FakeVolume:
        if name not in self.volumes:
            raise NotFound(f"no such volume: {name}")
        return self.volumes[name]

    def list(self) -> list[_FakeVolume]:
        return list(self.volumes.values())

    def create(self, name: str, driver: str, labels: dict[str, str]) -> _FakeVolume:
        self.created.append(name)
        volume = _FakeVolume(self, name, labels)
        self.volumes[name] = volume
        return volume


class _FakeContainer:
    def __init__(self, identity: str):
        self.id = identity + "0123456789ab"
        self.attrs = {"Image": "sha256:" + "a" * 64}

    def reload(self) -> None:
        return None


class _FakeContainers:
    def __init__(self, identity: str):
        self._identity = identity
        self.runs: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.output = b"MATRX_HOME_INHERITED=ok\n"

    def get(self, identity: str) -> _FakeContainer:
        return _FakeContainer(self._identity)

    def run(self, image, **kwargs):
        self.runs.append({"image": image, **kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        return self.output


class _FakeDocker:
    def __init__(self) -> None:
        self.volumes = _FakeVolumes()
        self.containers = _FakeContainers(HOST)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    import orchestrator.storage_layout as storage_layout

    monkeypatch.setattr(storage_layout.socket, "gethostname", lambda: HOST)
    return _FakeDocker()


def test_the_first_organization_home_inherits_the_legacy_home_exactly_once(
    client: _FakeDocker,
) -> None:
    legacy = legacy_user_volume_name(USER)
    client.volumes.seed(legacy)

    name = ensure_user_volume(client, USER, ORG_A)

    assert name == user_volume_name(USER, ORG_A)
    assert len(client.containers.runs) == 1, "the copy must run exactly once"
    run = client.containers.runs[0]
    assert run["volumes"] == {
        legacy: {"bind": "/from", "mode": "ro"},
        name: {"bind": "/to", "mode": "rw"},
    }, "the legacy home must be mounted READ-ONLY and the new one read-write"
    assert run["network_disabled"] is True
    assert inherited_from(client, name) == legacy
    # The legacy volume is still on disk, unchanged and unremoved.
    assert legacy in client.volumes.volumes
    assert client.volumes.removed == []

    # Idempotent: the same tenant reopening the same home copies nothing again.
    assert ensure_user_volume(client, USER, ORG_A) == name
    assert len(client.containers.runs) == 1


def test_a_second_organization_home_starts_empty_on_purpose(
    client: _FakeDocker,
) -> None:
    legacy = legacy_user_volume_name(USER)
    client.volumes.seed(legacy)

    ensure_user_volume(client, USER, ORG_A)
    assert len(client.containers.runs) == 1

    second = ensure_user_volume(client, USER, ORG_B)

    assert len(client.containers.runs) == 1, (
        "a later organization's home is that tenant's view, never a copy of the "
        "first tenant's home"
    )
    assert inherited_from(client, second) is None


def test_a_user_with_no_legacy_home_copies_nothing(client: _FakeDocker) -> None:
    name = ensure_user_volume(client, USER, ORG_A)

    assert client.containers.runs == []
    assert inherited_from(client, name) is None
    assert client.volumes.created == [name]


def test_a_failed_copy_refuses_the_creation_and_leaves_the_legacy_untouched(
    client: _FakeDocker,
) -> None:
    legacy = legacy_user_volume_name(USER)
    client.volumes.seed(legacy)
    client.containers.fail_with = RuntimeError("helper exited 1")

    with pytest.raises(HomeInheritanceError) as excinfo:
        ensure_user_volume(client, USER, ORG_A)

    message = str(excinfo.value)
    assert legacy in message and "untouched" in message

    new_name = user_volume_name(USER, ORG_A)
    assert new_name not in client.volumes.volumes, (
        "a box must never start on a half-copied home"
    )
    assert client.volumes.removed == [new_name]
    assert legacy in client.volumes.volumes, "the legacy home is never deleted"


def test_a_copy_that_does_not_confirm_itself_is_a_failure(
    client: _FakeDocker,
) -> None:
    """Nothing fails silently: a helper that exits 0 without finishing the copy
    is refused just like one that raises."""
    client.volumes.seed(legacy_user_volume_name(USER))
    client.containers.output = b"cp: some quiet trouble\n"

    with pytest.raises(HomeInheritanceError):
        ensure_user_volume(client, USER, ORG_A)

    assert user_volume_name(USER, ORG_A) not in client.volumes.volumes


def test_an_unreadable_daemon_is_never_read_as_no_legacy_home(
    client: _FakeDocker,
) -> None:
    """"Absent" and "could not tell" are different answers. Guessing "absent"
    here is exactly how a user's home would be silently abandoned."""

    def explode(name: str):
        raise RuntimeError("docker daemon is unreachable")

    client.volumes.get = explode  # type: ignore[assignment]

    with pytest.raises(HomeInheritanceError):
        ensure_user_volume(client, USER, ORG_A)
