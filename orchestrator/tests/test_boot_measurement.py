"""Guards for the boot measurement.

Nothing in this repository measured how long a sandbox takes to become usable
until 2026-09-20. The only real number anywhere was aidream's "a cold aidream
box takes minutes", while the canonical vision doc still advertised "~0.5 s from
the warm pool" — a warm pool retired 2026-09-17. A measurement that can silently
disappear, or that reads as zero when it was never taken, would be worse than
none, so the two ways for that to happen are guarded here.

The real create→ready and resume→ready numbers come from real boxes, not from
this file. These guard the SHAPE that makes such a number survivable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import _row_to_sandbox

STORE = Path(__file__).resolve().parents[1] / "orchestrator" / "store.py"

BOOT_COLUMNS = ("ready_at", "boot_seconds", "boot_kind", "boot_phase_seconds")


def _row(**over):
    base = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "sandbox_id": "sbx-test",
        "user_id": "00000000-0000-0000-0000-0000000000bb",
        "organization_id": "00000000-0000-0000-0000-0000000000cc",
        "status": "ready",
        "container_id": "c1",
        "created_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
        "config": None,
        "ttl_seconds": 7200,
    }
    base.update(over)

    class Row(dict):
        def __getitem__(self, key):
            if key not in self:
                raise KeyError(key)
            return dict.__getitem__(self, key)

    return Row(base)


def test_an_unmeasured_box_reads_as_unmeasured_not_as_instant() -> None:
    """NULL means NOT MEASURED. A default of 0.0 would make every box created
    before 2026-09-20 look like it booted instantly — exactly the "~0.5 s"
    fiction this work exists to replace."""
    sandbox = _row_to_sandbox(_row())
    assert sandbox.ready_at is None
    assert sandbox.boot_seconds is None
    assert sandbox.boot_kind is None
    assert sandbox.boot_phase_seconds is None


def test_phase_seconds_survive_the_json_round_trip() -> None:
    parsed = _row_to_sandbox(
        _row(boot_phase_seconds=json.dumps({"container": 1.5, "home_sync": 42.0}))
    )
    assert parsed.boot_phase_seconds == {"container": 1.5, "home_sync": 42.0}
    # Garbage is absent, never a half-parsed string masquerading as a reading.
    assert _row_to_sandbox(_row(boot_phase_seconds="not json")).boot_phase_seconds is None


def test_a_later_save_can_never_erase_a_measurement() -> None:
    """THE CLASS: every save after the boot (a heartbeat, a status change, the
    reaper's liveness touch) carries ``boot_seconds=None``. Without COALESCE in
    the ON CONFLICT branch, the second save of a box's life would wipe the only
    number the boot produced, and the column would be empty on every live row
    while looking like it worked."""
    sql = STORE.read_text()
    conflict = sql.split("ON CONFLICT (sandbox_id) DO UPDATE SET", 1)[1][:4000]
    for column in BOOT_COLUMNS:
        pattern = rf"{column}\s*=\s*COALESCE\(EXCLUDED\.{column},\s*sandbox_instances\.{column}\)"
        assert re.search(pattern, conflict), (
            f"{column} is not COALESCEd on upsert. Live, the first heartbeat "
            f"after boot would erase the measurement."
        )


def test_the_model_carries_every_measured_column() -> None:
    fields = set(SandboxResponse.model_fields)
    missing = [c for c in BOOT_COLUMNS if c not in fields]
    assert not missing, f"{missing} are stored but not exposed on GET /sandboxes/{{id}}"


def test_boot_kind_distinguishes_create_from_resume() -> None:
    """A resume lands on a retained home; averaging it with a cold create makes
    both numbers meaningless, so the row says which journey it measured."""
    s = SandboxResponse(
        sandbox_id="sbx-x",
        user_id="00000000-0000-0000-0000-0000000000bb",
        organization_id="00000000-0000-0000-0000-0000000000cc",
        status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
        boot_kind="resume",
        boot_seconds=12.5,
    )
    assert s.boot_kind == "resume"
