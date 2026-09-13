"""Trusted host state for hosted migration admission and crash fencing.

This module intentionally contains no sandbox-config state and no filesystem
restore logic.  Backup capture/restore belongs to ``hosted_backup``; this
journal only records its verified receipt after that helper returns.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

STATE_DIR = Path("/var/lib/matrx-sandbox/hosted-migrations")
TERMINAL_PHASES = frozenset({"committed", "recovered"})
VALID_PHASES = frozenset({
    "admitted", "old_stopped", "backup_verified", "target_create_intent",
    "target_created", "target_start_intent", "target_ready", "target_quiesce_intent",
    "postboot_verified", "names_cut_over", "commit_intent", "activation_intent",
    "commit_uncertain", "committed", "recovered", "recovery_required",
    "backup_intent", "rename_intent", "rollback_intent", "restore_intent",
    "network_disconnect_intent", "network_disconnected",
})
_RECORD_SCHEMA_VERSION = 2
_SUPPORTED_RECORD_SCHEMA_VERSIONS = frozenset({1, _RECORD_SCHEMA_VERSION})
_REQUIRED_RECORD_FIELDS = frozenset({
    "sandbox_id", "old_id", "old_name", "old_image", "source_volume",
    "source_identity", "row_identity", "target_name", "target_image",
    "operation_label", "backup_name", "helper_image", "rollback_name",
    "verify_timeout", "stop_timeout", "phase", "schema_version",
})
_V2_REQUIRED_RECORD_FIELDS = frozenset({
    "state_volume_name", "state_volume_creation_intent",
    "helper_image_pin", "helper_image_pin_creation_intent",
})


def _unescape_mountinfo(value: str) -> str:
    """Decode Linux mountinfo's octal path escaping without shell parsing."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _mountinfo_has_mountpoint(mountinfo: str, path: Path) -> bool:
    """Recognize bind mounts, which deliberately fail ``os.path.ismount``."""
    # Mount namespaces address the literal mount target.  Resolving host-side
    # symlinks (for example macOS /var -> /private/var in tests) would compare
    # a different namespace path.
    expected = os.path.abspath(path)
    for line in mountinfo.splitlines():
        fields = line.split()
        # mountinfo has mandatory fields through mount-point at index 4; later
        # optional propagation fields cannot move that position.
        if len(fields) >= 6 and _unescape_mountinfo(fields[4]) == expected:
            return True
    return False


def _has_required_state_mount(path: Path) -> bool:
    try:
        return _mountinfo_has_mountpoint(Path("/proc/self/mountinfo").read_text(), path)
    except OSError:
        return False


def recovery_action(record: dict[str, Any], *, db_container_id: str | None,
                    target_exists_ready: bool) -> str:
    """Choose recovery without ever inventing a backup or trusting DB alone.

    ``resume_old`` means the original *paused* process only; it never starts a
    container.  ``preserve_fenced`` is
    deliberately boring: ambiguous CAS/cutover state must remain inspectable.
    """
    phase = record.get("phase")
    if phase == "recovery_required":
        phase = record.get("resume_phase")
    if db_container_id not in {record.get("old_id"), record.get("target_id")} or db_container_id is None:
        return "preserve_fenced"
    if record.get("target_id") and db_container_id == record["target_id"]:
        return "finalize_committed" if target_exists_ready else "preserve_fenced"
    if phase in {"admitted", "old_stopped", "backup_intent"} and not record.get("backup_receipt"):
        return "resume_pre_copy_source"
    if phase in {"backup_verified",
                 "network_disconnect_intent", "network_disconnected", "target_create_intent",
                 "target_created", "target_start_intent", "target_ready", "target_quiesce_intent",
                 "postboot_verified", "rename_intent", "names_cut_over", "commit_intent",
                 "activation_intent", "commit_uncertain", "rollback_intent", "restore_intent"}:
        return "resume_old" if record.get("backup_receipt") else "preserve_fenced"
    return "preserve_fenced"


def transition(record: dict[str, Any], phase: str, **fields: Any) -> dict[str, Any]:
    """Validate required phase evidence before it reaches durable journal state."""
    if phase not in VALID_PHASES:
        raise HostedMigrationStateError("unknown hosted migration phase")
    next_record = dict(record, **fields, phase=phase)
    required = {
        "admitted": {"old_id", "old_name", "source_volume"},
        "backup_verified": {"backup_receipt"},
        "target_create_intent": {"target_name", "target_image", "operation_label"},
        "target_created": {"target_id"},
        "target_start_intent": {"target_id"},
        "target_quiesce_intent": {"target_id"},
        "postboot_verified": {"target_id", "postboot_verified_receipt"},
        "commit_intent": {"target_id"},
        "activation_intent": {"target_id"},
        "network_disconnect_intent": {"source_endpoint"},
        "network_disconnected": {"source_endpoint", "network_disconnect_receipt"},
    }
    missing = [key for key in required.get(phase, set()) if not next_record.get(key)]
    if missing:
        raise HostedMigrationStateError(f"phase {phase} missing {','.join(sorted(missing))}")
    if (phase == "target_quiesce_intent" and next_record.get("state_volume_creation_intent")
            and not next_record.get("activation_home_preflight_receipt")):
        raise HostedMigrationStateError("phase target_quiesce_intent missing lifecycle-path preflight")
    return next_record


class HostedMigrationStateError(RuntimeError):
    pass


def validate_record(record: dict[str, Any]) -> None:
    """Reject partial or stale durable state before it can drive Docker recovery."""
    if not isinstance(record, dict):
        raise HostedMigrationStateError("hosted migration journal record is not an object")
    schema_version = record.get("schema_version")
    if schema_version not in _SUPPORTED_RECORD_SCHEMA_VERSIONS:
        raise HostedMigrationStateError("hosted migration journal has an unsupported schema")
    missing = _REQUIRED_RECORD_FIELDS.difference(record)
    if missing:
        raise HostedMigrationStateError(
            f"hosted migration journal is missing {','.join(sorted(missing))}"
        )
    if schema_version == _RECORD_SCHEMA_VERSION:
        missing = _V2_REQUIRED_RECORD_FIELDS.difference(record)
        if missing:
            raise HostedMigrationStateError(
                f"hosted migration journal schema 2 is missing {','.join(sorted(missing))}"
            )
        expected_state_volume = f"matrx-migration-state-{record.get('sandbox_id', '')}"
        expected_helper_pin = f"matrx-migration-helper:{record.get('operation_label', '')}"
        if (record.get("state_volume_name") != expected_state_volume
                or record.get("state_volume_creation_intent") != {
                    "name": expected_state_volume, "sandbox_id": record.get("sandbox_id"),
                }):
            raise HostedMigrationStateError(
                "hosted migration journal schema 2 has invalid state-volume intent"
            )
        if (record.get("helper_image_pin") != expected_helper_pin
                or record.get("helper_image_pin_creation_intent") != {
                    "pin": expected_helper_pin, "image": record.get("helper_image"),
                }):
            raise HostedMigrationStateError(
                "hosted migration journal schema 2 has invalid helper-pin intent"
            )
    if record.get("phase") not in VALID_PHASES:
        raise HostedMigrationStateError("hosted migration journal has an invalid phase")
    for key in ("sandbox_id", "old_id", "old_name", "old_image", "source_volume",
                "target_name", "target_image", "operation_label", "backup_name",
                "helper_image", "rollback_name"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise HostedMigrationStateError(f"hosted migration journal has invalid {key}")
    if not isinstance(record.get("row_identity"), dict) or not isinstance(record.get("source_identity"), dict):
        raise HostedMigrationStateError("hosted migration journal has invalid durable identities")
    if record["source_identity"].get("name") != record["source_volume"]:
        raise HostedMigrationStateError("hosted migration journal source identity does not match volume")
    if record["row_identity"].get("sandbox_id") != record["sandbox_id"]:
        raise HostedMigrationStateError("hosted migration journal row identity does not match sandbox")
    process = record.get("old_process_identity")
    if process is not None and (not isinstance(process, dict) or not isinstance(process.get("pid"), int)
                                or process["pid"] <= 0 or not isinstance(process.get("started_at"), str)
                                or not process["started_at"]):
        raise HostedMigrationStateError("hosted migration journal has invalid original process identity")
    if "row_persistence_volume" in record and record["row_persistence_volume"] is not None and not isinstance(record["row_persistence_volume"], str):
        raise HostedMigrationStateError("hosted migration journal has invalid row persistence identity")
    if record.get("storage_kind") == "ec2_writable_layer":
        if (record.get("source_home_key") != "layer-" + record["sandbox_id"]
                or not isinstance(record.get("source_graph_driver"), dict)
                or record["source_volume"] != "matrx-ec2-home-" + record["sandbox_id"]):
            raise HostedMigrationStateError("writable-layer migration has invalid source identity")
    elif record.get("storage_kind") not in {None, "named_volume"}:
        raise HostedMigrationStateError("unknown migration storage kind")
    if record["phase"] in {"network_disconnect_intent", "network_disconnected", "target_create_intent",
                            "target_created", "target_start_intent", "target_ready", "target_quiesce_intent",
                            "postboot_verified", "rename_intent", "names_cut_over", "commit_intent",
                            "activation_intent", "commit_uncertain", "committed", "rollback_intent",
                            "restore_intent"}:
        endpoint = record.get("source_endpoint")
        if (not isinstance(endpoint, dict) or not isinstance(endpoint.get("network"), str)
                or not endpoint["network"] or not isinstance(endpoint.get("network_id"), str)
                or not endpoint["network_id"]):
            raise HostedMigrationStateError("migration journal has no source network identity")
    if record["phase"] in {"network_disconnected", "target_create_intent", "target_created",
                            "target_start_intent", "target_ready", "target_quiesce_intent", "postboot_verified",
                            "rename_intent", "names_cut_over", "commit_intent", "activation_intent",
                            "commit_uncertain", "committed", "rollback_intent", "restore_intent"}:
        receipt = record.get("network_disconnect_receipt")
        if (not isinstance(receipt, dict) or receipt.get("old_id") != record["old_id"]
                or receipt.get("network_id") != record["source_endpoint"]["network_id"]):
            raise HostedMigrationStateError("migration journal has no verified network disconnect receipt")
    if not isinstance(record.get("verify_timeout"), int) or record["verify_timeout"] <= 0:
        raise HostedMigrationStateError("hosted migration journal has invalid verify timeout")
    if not isinstance(record.get("stop_timeout"), int) or record["stop_timeout"] <= 0:
        raise HostedMigrationStateError("hosted migration journal has invalid stop timeout")
    post_backup = {
        "backup_verified", "target_create_intent", "target_created", "target_start_intent",
        "target_ready", "target_quiesce_intent", "postboot_verified", "rename_intent", "names_cut_over", "commit_intent", "activation_intent",
        "commit_uncertain", "committed", "rollback_intent", "restore_intent",
    }
    if record["phase"] in post_backup and not isinstance(record.get("backup_receipt"), dict):
        raise HostedMigrationStateError("hosted migration journal has no verified backup receipt")
    if (record.get("storage_kind") != "ec2_writable_layer"
            and record["phase"] in {"postboot_verified", "rename_intent", "names_cut_over",
                                    "commit_intent", "activation_intent", "commit_uncertain", "committed"}
            and not isinstance(record.get("pre_cas_home_receipt"), dict)):
        raise HostedMigrationStateError("hosted migration has no held-target home manifest receipt")
    target_known = {
        "target_created", "target_start_intent", "target_ready", "target_quiesce_intent",
        "postboot_verified", "rename_intent", "names_cut_over", "commit_intent", "activation_intent", "commit_uncertain", "committed",
    }
    if record["phase"] in target_known and not isinstance(record.get("target_id"), str):
        raise HostedMigrationStateError("hosted migration journal has no target identity")
    preflight_phases = {
        "target_quiesce_intent", "postboot_verified", "rename_intent", "names_cut_over",
        "commit_intent", "activation_intent", "commit_uncertain", "committed",
    }
    if record["phase"] in preflight_phases and (
        schema_version == _RECORD_SCHEMA_VERSION
        or record.get("state_volume_creation_intent") is not None
        or record.get("activation_home_preflight_receipt") is not None
    ):
        receipt = record.get("activation_home_preflight_receipt")
        if (not isinstance(receipt, dict)
                or receipt.get("target_id") != record.get("target_id")
                or receipt.get("agent_lifecycle_paths_writable") is not True):
            raise HostedMigrationStateError("migration journal has no valid lifecycle-path preflight")
    state_intent = record.get("state_volume_creation_intent")
    if state_intent is not None and (
        not isinstance(state_intent, dict)
        or state_intent.get("name") != record.get("state_volume_name")
        or state_intent.get("sandbox_id") != record.get("sandbox_id")
    ):
        raise HostedMigrationStateError("migration journal has invalid state-volume creation intent")
    pin_intent = record.get("helper_image_pin_creation_intent")
    if pin_intent is not None and (
        not isinstance(pin_intent, dict)
        or pin_intent.get("pin") != record.get("helper_image_pin")
        or pin_intent.get("image") != record.get("helper_image")
    ):
        raise HostedMigrationStateError("migration journal has invalid helper-pin creation intent")
    if record.get("storage_kind") == "ec2_writable_layer":
        postboot_phases = {
            "postboot_verified", "rename_intent", "names_cut_over", "commit_intent",
            "commit_uncertain", "activation_intent", "committed",
        }
        if record["phase"] in postboot_phases:
            receipt = record.get("postboot_verified_receipt")
            backup = record.get("backup_receipt")
            if not isinstance(receipt, dict) or not isinstance(backup, dict):
                raise HostedMigrationStateError("EC2 promotion has no durable postboot verification receipt")
            if (receipt.get("ok") is not True
                    or receipt.get("operation") != record["operation_label"]
                    or receipt.get("target_id") != record.get("target_id")
                    or receipt.get("target_image") != record["target_image"]
                    or receipt.get("manifest_sha256") != backup.get("manifest_sha256")):
                raise HostedMigrationStateError("EC2 postboot verification receipt does not bind copied target")
        if record["phase"] == "committed":
            activation = record.get("activation_receipt")
            if (not isinstance(activation, dict) or activation.get("target_id") != record.get("target_id")
                    or activation.get("migration_state") != "active"):
                raise HostedMigrationStateError("EC2 promotion has no durable active-target receipt")


def _safe_component(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", value):
        raise HostedMigrationStateError("unsafe hosted migration state key")


class HostedMigrationJournal:
    def __init__(self, root: Path = STATE_DIR):
        self.root = root

    def ensure_ready(self) -> None:
        """Require real mounted state in production; tests use an injected root."""
        if self.root == STATE_DIR and not _has_required_state_mount(self.root):
            raise HostedMigrationStateError("hosted migration state mount is absent")
        try:
            st = self.root.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise HostedMigrationStateError("hosted migration state ownership/mode is unsafe")
            fd, probe_name = tempfile.mkstemp(prefix=".journal-probe-", dir=self.root)
            probe = Path(probe_name)
            try:
                os.write(fd, b"ok")
                os.fsync(fd)
            finally:
                os.close(fd)
            probe.unlink()
        except OSError as exc:
            raise HostedMigrationStateError(f"hosted migration state unavailable: {exc}") from exc

    def _path(self, sandbox_id: str) -> Path:
        _safe_component(sandbox_id)
        return self.root / f"{sandbox_id}.json"

    def read(self, sandbox_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._path(sandbox_id).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise HostedMigrationStateError("hosted migration journal is corrupt") from exc
        if not isinstance(value, dict) or value.get("sandbox_id") != sandbox_id:
            raise HostedMigrationStateError("hosted migration journal has invalid identity/phase")
        validate_record(value)
        return value

    def write(self, record: dict[str, Any]) -> None:
        sandbox_id = str(record.get("sandbox_id") or "")
        validate_record(record)
        path = self._path(sandbox_id)
        fd, temporary = tempfile.mkstemp(prefix=f".{sandbox_id}.", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
                output.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextmanager
    def lock(self, key: str, *, shared: bool = False) -> Iterator[None]:
        self.ensure_ready()
        _safe_component(key)
        fd = os.open(self.root / f"{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostedMigrationStateError("hosted migration already admitted") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def fenced(self, sandbox_id: str) -> bool:
        record = self.read(sandbox_id)
        return bool(record and record["phase"] not in TERMINAL_PHASES)

    def pending(self) -> list[dict[str, Any]]:
        return [record for record in self.records() if record["phase"] not in TERMINAL_PHASES]

    def records(self) -> list[dict[str, Any]]:
        self.ensure_ready()
        return [record for path in self.root.glob("*.json")
                if (record := self.read(path.stem))]

    def retained_container_ids(self) -> set[str]:
        """Return only recovery-owned artifacts, never the usable current runtime."""
        retained: set[str] = set()
        for record in self.records():
            if record.get("cleanup_complete"):
                continue
            phase = record["phase"]
            if phase == "committed":
                retained.add(record["old_id"])
            elif phase == "recovered":
                target_id = record.get("target_id")
                if isinstance(target_id, str) and target_id:
                    retained.add(target_id)
            else:
                retained.add(record["old_id"])
                target_id = record.get("target_id")
                if isinstance(target_id, str) and target_id:
                    retained.add(target_id)
        return retained


def hosted_fenced(sandbox_id: str) -> bool:
    """Cross-process fence; unavailable/corrupt hosted state is denial, never absence."""
    from orchestrator.config import settings
    if settings.host_tier not in {"hosted", "ec2"}:
        return False
    try:
        journal = HostedMigrationJournal()
        journal.ensure_ready()
        return journal.fenced(sandbox_id)
    except HostedMigrationStateError:
        return True


def hosted_volume_fenced(volume: str | None) -> bool:
    """Deny sibling lifecycle writes while any hosted migration owns its home."""
    from orchestrator.config import settings
    if settings.host_tier not in {"hosted", "ec2"} or not volume:
        return False
    try:
        journal = HostedMigrationJournal(); journal.ensure_ready()
        return any(volume in {record.get("source_volume"), record.get("source_home_key")}
                   for record in journal.pending())
    except HostedMigrationStateError:
        return True
