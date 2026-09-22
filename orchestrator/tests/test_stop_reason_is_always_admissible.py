"""Every ``stop_reason`` this repo can write must satisfy the database's CHECK.

``sandbox_instances_stop_reason_check`` admits exactly five values:
``user_requested``, ``expired``, ``error``, ``graceful_shutdown``, ``admin``
(verified against the live schema, 2026-09-22). A write outside that set does
not degrade — it RAISES, the UPDATE is lost, and the row keeps whatever status
it had. Two live instances of that class:

  * ``sandbox_manager`` marked a box whose container vanished during token
    issuance with ``container_missing_at_token_issuance``. The UPDATE raised
    and the dead row stayed ``ready`` forever, holding the person's admission
    slot against a container that no longer exists.
  * ``_wait_for_ready`` writes free-text boot-failure reasons ("stalled in
    home_sync after 3600s, 4210/8630 files"). ``save()`` passes ``stop_reason``
    straight through, so the whole save of a FAILED row raised and the row was
    left in ``creating``.

The fix keeps the sentence — the honest failure line moves to ``config`` and
the log — and puts a canonical reason in the column.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ALLOWED = {"user_requested", "expired", "error", "graceful_shutdown", "admin"}

ORCHESTRATOR = Path(__file__).resolve().parents[1] / "orchestrator"

#: Every function whose ``reason``/``stop_reason`` argument lands in the column.
STOP_REASON_SINKS = {
    "mark_stopped", "mark_stopped_if_active", "_finalize_terminal_status",
    "destroy_sandbox", "_destroy_sandbox_unleased",
}


def _literal_reasons() -> list[tuple[str, int, str]]:
    """Every string literal this repo hands to a stop-reason sink."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in STOP_REASON_SINKS:
                continue
            args = list(node.args)
            args += [kw.value for kw in node.keywords
                     if kw.arg in ("reason", "stop_reason")]
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((str(path), node.lineno, arg.value))
    return found


def test_every_stop_reason_literal_in_the_repo_is_admissible() -> None:
    offenders = [
        (path, line, value) for path, line, value in _literal_reasons()
        if value not in ALLOWED
    ]
    assert not offenders, (
        "these calls write a stop_reason the database CHECK rejects, so the "
        "UPDATE raises and the row keeps its stale status:\n"
        + "\n".join(f"  {p}:{n} -> {v!r}" for p, n, v in offenders)
        + f"\nallowed: {sorted(ALLOWED)}"
    )


def test_the_census_actually_finds_calls() -> None:
    """A census that matches nothing would pass forever."""
    assert len(_literal_reasons()) >= 4


@pytest.mark.asyncio
async def test_a_boot_failure_keeps_its_sentence_without_breaking_the_column() -> None:
    """The honest failure line is the point; it just cannot live in a column
    with a five-value CHECK."""
    from datetime import datetime, timezone

    from orchestrator.models import SandboxResponse, SandboxStatus
    from orchestrator.sandbox_manager import record_boot_failure

    box = SandboxResponse(
        sandbox_id="sbx-boot", user_id="77777777-7777-4777-8777-777777777777",
        organization_id="88888888-8888-4888-8888-888888888888",
        status=SandboxStatus.STARTING, created_at=datetime.now(timezone.utc),
        tier="hosted",
    )
    detail = "stalled in home_sync after 3600s (4210/8630 files)"
    record_boot_failure(box, detail)

    assert box.status == SandboxStatus.FAILED
    assert box.stop_reason in ALLOWED, (
        f"{box.stop_reason!r} would make the save of this FAILED row raise, "
        "leaving it stuck in 'creating'"
    )
    assert box.config.get("stop_detail") == detail, (
        "the failure sentence was thrown away; the screen now says a box "
        "failed and cannot say why"
    )


def _stop_reason_assignments() -> list[tuple[str, int]]:
    """Every place code writes ``<something>.stop_reason = ...``.

    Closing the class means removing the door: a direct assignment bypasses the
    sinks the census above walks, which is exactly how ``_wait_for_ready`` put
    free text into the column for months without anything noticing.
    """
    found: list[tuple[str, int]] = []
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "stop_reason":
                    found.append((str(path), node.lineno))
    return found


def test_only_the_canonical_chokepoints_write_the_column_directly() -> None:
    """Two are allowed: ``record_boot_failure`` (the boot give-up) and the
    in-memory store mirroring the Postgres statements. Anything else must go
    through a sink, or it will put a value the CHECK rejects into the column
    and lose the whole write."""
    allowed_files = {"sandbox_manager.py", "store.py"}
    offenders = [
        (path, line) for path, line in _stop_reason_assignments()
        if Path(path).name not in allowed_files
    ]
    assert not offenders, (
        "these write stop_reason directly, outside the canonical chokepoints:\n"
        + "\n".join(f"  {p}:{n}" for p, n in offenders)
    )


def test_every_direct_assignment_writes_an_admissible_literal() -> None:
    """Inside the allowed files, a literal must still be one of the five."""
    bad: list[str] = []
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Attribute) and t.attr == "stop_reason"
                       for t in node.targets):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if value.value not in ALLOWED:
                    bad.append(f"{path}:{node.lineno} -> {value.value!r}")
            elif isinstance(value, ast.Name) and value.id in {
                    "STOP_REASON_ERROR", "reason"}:
                continue
            elif isinstance(value, ast.Constant) and value.value is None:
                continue
            elif not isinstance(value, ast.Constant):
                # A computed value is the shape that put free text in the
                # column. It is only allowed inside record_boot_failure.
                bad.append(f"{path}:{node.lineno} -> computed expression")
    assert not bad, (
        "a stop_reason write the database CHECK can reject:\n  " + "\n  ".join(bad)
    )
