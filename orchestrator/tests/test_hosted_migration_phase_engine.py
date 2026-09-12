from orchestrator.hosted_migration import HostedMigrationJournal, recovery_action, transition, HostedMigrationStateError, validate_record
from orchestrator.hosted_migration import hosted_fenced, hosted_volume_fenced
from orchestrator.hosted_runtime import _assert_no_unowned_home_writer, _quiesce_target
import pytest


def _base():
    return {
        "schema_version": 1, "sandbox_id": "sbx", "old_id": "old", "old_name": "sbx",
        "old_image": "sha256:" + "a" * 64, "source_volume": "home",
        "source_identity": {"name": "home"}, "row_identity": {"sandbox_id": "sbx"},
        "target_name": "sbx-mig-op", "target_image": "sha256:" + "b" * 64,
        "operation_label": "op", "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": "sbx-old-op", "verify_timeout": 1, "stop_timeout": 1,
    }


def test_prebackup_crash_never_restores_missing_backup():
    assert recovery_action(dict(_base(), phase="admitted"), db_container_id="old", target_exists_ready=False) == "restart_old"
    assert recovery_action(dict(_base(), phase="old_stopped"), db_container_id="old", target_exists_ready=False) == "restart_old"


def test_postbackup_precommit_requires_receipt_before_restore():
    assert recovery_action(dict(_base(), phase="target_created"), db_container_id="old", target_exists_ready=False) == "preserve_fenced"
    assert recovery_action(dict(_base(), phase="target_created", backup_receipt={"schema_version": 1}), db_container_id="old", target_exists_ready=False) == "restore_then_restart_old"


def test_db_target_match_alone_is_not_commit():
    record = dict(_base(), phase="commit_uncertain", target_id="new", backup_receipt={"x": 1})
    assert recovery_action(record, db_container_id="new", target_exists_ready=False) == "preserve_fenced"
    assert recovery_action(record, db_container_id="new", target_exists_ready=True) == "finalize_committed"


def test_target_id_must_be_journaled_before_start_intent():
    try:
        transition(_base(), "target_start_intent")
    except HostedMigrationStateError:
        pass
    else:
        raise AssertionError("mutant without target ID was accepted")


@pytest.mark.parametrize("phase,expected", [
    ("admitted", "restart_old"), ("old_stopped", "restart_old"),
    ("backup_verified", "restore_then_restart_old"), ("target_create_intent", "restore_then_restart_old"),
    ("target_created", "restore_then_restart_old"), ("target_start_intent", "restore_then_restart_old"),
    ("target_ready", "restore_then_restart_old"), ("names_cut_over", "restore_then_restart_old"),
    ("commit_intent", "restore_then_restart_old"), ("commit_uncertain", "restore_then_restart_old"),
    ("committed", "preserve_fenced"), ("recovered", "preserve_fenced"), ("recovery_required", "preserve_fenced"),
])
def test_every_supported_phase_has_a_non_destructive_recovery_decision(phase, expected):
    record = dict(_base(), phase=phase, backup_receipt={"schema_version": 1}, target_id="new")
    assert recovery_action(record, db_container_id="old", target_exists_ready=False) == expected


def test_durable_record_rejects_partial_crash_artifact():
    """Break caught: a valid phase alone drove Docker recovery with missing identities."""
    with pytest.raises(HostedMigrationStateError, match="missing"):
        validate_record({"schema_version": 1, "sandbox_id": "sbx", "phase": "admitted"})


def test_durable_record_accepts_admitted_full_intent():
    validate_record(dict(_base(), phase="admitted"))


@pytest.mark.parametrize("phase", ["rollback_intent", "restore_intent"])
def test_verified_backup_rollback_can_be_durable_before_target_exists(phase):
    """Break caught: recovery cannot persist its restore intent before target creation."""
    validate_record(
        dict(_base(), phase=phase, backup_receipt={"verified": True})
    )


@pytest.mark.parametrize("phase", ["target_created", "target_start_intent", "commit_intent"])
def test_target_phase_still_refuses_missing_target_identity(phase):
    with pytest.raises(HostedMigrationStateError, match="target identity"):
        validate_record(dict(_base(), phase=phase, backup_receipt={"verified": True}))


@pytest.mark.parametrize(
    ("phase", "cleanup_complete", "expected"),
    [
        ("admitted", False, {"old", "new"}),
        ("committed", False, {"old"}),
        ("recovered", False, {"new"}),
        ("committed", True, set()),
    ],
)
def test_retained_artifacts_exclude_the_current_runtime(tmp_path, phase, cleanup_complete, expected):
    """Break caught: reconcile hides the usable committed/recovered container."""
    journal = HostedMigrationJournal(tmp_path)
    record = dict(_base(), phase=phase, cleanup_complete=cleanup_complete)
    if phase != "recovered":
        record.update(target_id="new", backup_receipt={"verified": True})
    else:
        record.update(target_id="new")
    journal.write(record)
    assert journal.retained_container_ids() == expected


class _Container:
    def __init__(self, identity, *, status="exited", writable_home=False):
        self.id, self.status = identity, status
        self.attrs = {"Mounts": [{"Type": "volume", "Name": "home", "Destination": "/home/agent", "RW": writable_home}]}
        self.stop_calls = 0

    def reload(self):
        return None

    def stop(self, **_):
        self.stop_calls += 1
        self.status = "exited"


class _ContainerList:
    def __init__(self, containers):
        self._containers = containers

    def list(self, **_):
        return self._containers


@pytest.mark.asyncio
async def test_recovery_does_not_stop_an_already_exited_target():
    """Break caught: Docker stop-on-exited aborts recovery before rollback."""
    target = _Container("new", status="exited", writable_home=True)
    await _quiesce_target(target, {"stop_timeout": 1})
    assert target.stop_calls == 0


@pytest.mark.asyncio
async def test_recovery_refuses_a_running_sibling_writer_before_restore():
    """Break caught: restoring a shared home overwrites a sibling's live write."""
    sibling = _Container("sibling", status="running", writable_home=True)
    client = type("Client", (), {"containers": _ContainerList([sibling])})()
    with pytest.raises(HostedMigrationStateError, match="another running container"):
        await _assert_no_unowned_home_writer(client, _base(), allowed_ids={"old", "new"})


def test_missing_hosted_journal_fails_closed(monkeypatch):
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    assert hosted_fenced("sbx")
    assert hosted_volume_fenced("matrx-user-u")


def test_nonhosted_journal_does_not_block(monkeypatch):
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "ec2")
    assert not hosted_fenced("sbx")
    assert not hosted_volume_fenced("matrx-user-u")
