"""Config attestation wiring for the orchestrator.

- Every ``platform.feature_knob`` read skips archived rows (an archived knob is not a setting).
- The manifest generator declares every knob the code reads, incl. direct ``knobs._raw`` reads.
- Boot attests the baked manifest only for the sha this process runs.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_every_feature_knob_read_skips_archived_rows() -> None:
    offenders = []
    for f in sorted((ROOT / "orchestrator").rglob("*.py")):
        for m in re.finditer(r'"(SELECT[^"]*FROM platform\.feature_knob[^"]*)"', f.read_text()):
            if "archived_at IS NULL" not in m.group(1):
                offenders.append(f"{f.relative_to(ROOT)}: {m.group(1)}")
    assert not offenders, "feature_knob reads that serve archived rows:\n" + "\n".join(offenders)


def _ca():
    return pytest.importorskip(
        "matrx_utils.config_attestation",
        reason="LOUD SKIP: installed matrx-utils predates config_attestation (publish pending)",
    )


def test_manifest_declares_direct_raw_reads() -> None:
    _ca()
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from config_manifest import generate

    manifest, unresolved = generate(ROOT, "a" * 40)
    keys = {(k["feature"], k["key"]) for k in manifest["knobs"]}
    assert ("infrastructure.sandbox", "auto_update_idle_seconds") in keys
    assert ("infrastructure.sandbox", "active_sandbox_capacity") in keys
    assert unresolved == []


def test_boot_attests_baked_manifest_for_running_sha_only(tmp_path: Path, monkeypatch) -> None:
    ca = _ca()
    from orchestrator import config_attestation as oca

    sha = "b" * 40
    manifest = ca.build_manifest(app="matrx-sandbox", app_version="1.0.0", build_sha=sha,
                                 knobs={("infrastructure.sandbox", "k"): ["x.py"]}, app_config_keys={},
                                 bootstrap={"database_url": "", "database_realtime_url": "",
                                            "publishable_key_prefix": "", "source": "t"})
    (tmp_path / ca.BAKED_MANIFEST).write_text(json.dumps(manifest))
    monkeypatch.setattr(oca, "_APP_ROOT", tmp_path)
    attested: list[dict] = []

    class Store:
        async def attest_config_manifest(self, m: dict) -> dict:
            attested.append(m)
            return {"key_count": 1}

        async def stored_config_manifest(self, app: str, s: str) -> dict | None:
            return None

    asyncio.run(oca.attest_baked_manifest(ca, Store(), sha))
    assert oca._state["attestation"]["status"] == "attested" and attested == [manifest]
    asyncio.run(oca.attest_baked_manifest(ca, Store(), "c" * 40))
    assert oca._state["attestation"]["status"] == "failed" and "this process runs" in oca._state["attestation"]["error"]
    assert len(attested) == 1
