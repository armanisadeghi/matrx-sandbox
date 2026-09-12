"""Hosted Docker discovery must not resurrect retained migration artifacts."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.reconcile import reconcile_from_docker


def _patch_journal(monkeypatch, journal):
    """Reconcile and its imported lease class must share durable test state."""
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)


def _record(*, phase: str, old_id: str, target_id: str | None = None) -> dict:
    record = {"schema_version": 1, "sandbox_id": "sbx-old", "phase": phase, "old_id": old_id, "old_name": "old", "old_image": "sha256:" + "a" * 64, "source_volume": "home", "source_identity": {"name": "home"}, "row_identity": {"sandbox_id": "sbx-old"}, "target_name": "target", "target_image": "sha256:" + "b" * 64, "target_id": target_id, "operation_label": "op", "backup_name": "backup", "helper_image": "sha256:" + "c" * 64, "rollback_name": "rollback", "verify_timeout": 1, "stop_timeout": 1, "source_endpoint": {"network": "bridge", "network_id": "network-id", "aliases": ["old"], "requested_aliases": ["old"], "ipv4_address": "172.17.0.2", "ipv4_address_prefixlen": 16, "mac_address": "02:42:ac:11:00:02"}, "network_disconnect_receipt": {"old_id": old_id, "network": "bridge", "network_id": "network-id", "absent": True}, "pre_cas_home_receipt": {"manifest_sha256": "digest", "source_volume": {"name": "home"}}}
    if phase == "committed":
        record["backup_receipt"] = {"verified": True}
    return record


def _live(container_id: str, sandbox_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=container_id, attrs={"Config": {"Labels": {"matrx.sandbox_id": sandbox_id, "matrx.user_id": "11111111-1111-4111-8111-111111111111", "matrx.tier": "hosted", "matrx.organization_id": "22222222-2222-4222-8222-222222222222"}}, "State": {"Running": True}}, reload=lambda: None)


@pytest.mark.asyncio
async def test_retained_container_id_is_skipped_before_row_reconciliation(monkeypatch, tmp_path):
    from orchestrator.hosted_migration import HostedMigrationJournal
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record(phase="admitted", old_id="old-artifact", target_id="target-artifact"))
    _patch_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    old = SimpleNamespace(id="old-artifact", attrs={}, reload=lambda: None)
    live = _live("live", "sbx-live")
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [old, live]))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    saved = []
    class Store:
        async def get(self, sandbox_id): return None
        async def get_lifecycle(self, sandbox_id): return None
        async def save(self, value): saved.append(value)
    summary = await reconcile_from_docker(Store())
    assert summary["skipped"] >= 1
    assert [item.sandbox_id for item in saved] == ["sbx-live"]


@pytest.mark.asyncio
async def test_committed_target_is_reconciled_but_committed_old_artifact_is_not(monkeypatch, tmp_path):
    """The usable committed target must not be mistaken for migration debris."""
    from orchestrator.hosted_migration import HostedMigrationJournal
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record(phase="committed", old_id="old-artifact", target_id="current-target"))
    _patch_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    old = SimpleNamespace(id="old-artifact", attrs={}, reload=lambda: None)
    target = _live("current-target", "sbx-current")
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [old, target])))
    saved = []
    class Store:
        async def get(self, _id): return None
        async def get_lifecycle(self, _id): return None
        async def save(self, value): saved.append(value)
    summary = await reconcile_from_docker(Store())
    assert summary["skipped"] == 1
    assert [item.sandbox_id for item in saved] == ["sbx-current"]


@pytest.mark.asyncio
async def test_deleted_row_reaps_container_without_resurrecting_it(monkeypatch, tmp_path):
    from orchestrator.hosted_migration import HostedMigrationJournal
    journal = HostedMigrationJournal(tmp_path)
    _patch_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    removed = []
    live = _live("orphan", "sbx-deleted")
    live.remove = lambda **kwargs: removed.append(kwargs)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [live])))
    saved = []
    class Store:
        async def get(self, _id): return None
        async def get_lifecycle(self, _id): return {"deleted": True, "status": "stopped"}
        async def save(self, value): saved.append(value)
    summary = await reconcile_from_docker(Store())
    assert summary["reaped"] == 1
    assert removed == [{"force": True}]
    assert saved == []
