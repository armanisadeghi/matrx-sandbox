"""Image freshness self-report (tracker cf1ab9ab)."""

from __future__ import annotations

import os

from matrx_agent import image_drift, selfupdate

STAMP = {"to_version": "2fd90ec9dc9a76f7185ce0f820d513a761435fb4",
         "refreshed_at": "2026-10-21T17:09:30Z"}


def test_stale_box_says_so_and_why(tmp_path):
    facts = image_drift.image_drift(
        stamp=STAMP, env={"MATRX_IMAGE_VERSION": "1bd5a91df4cde3a550f267a54bee885d63b3c630"},
        opt_out_file=tmp_path / "auto-update.off", probe_system=False)
    assert facts["status"] == "stale"
    assert facts["auto_update_opted_out"] is False
    assert "STALE IMAGE: running 1bd5a91, platform current 2fd90ec" in facts["message"]
    assert "container swap" in facts["message"]
    assert image_drift.warning(facts).startswith("STALE IMAGE")


def test_current_box_is_quiet(tmp_path):
    facts = image_drift.image_drift(stamp=STAMP, env={"MATRX_IMAGE_VERSION": "2fd90ec"},
                                    opt_out_file=tmp_path / "x", probe_system=False)
    assert facts["status"] == "current"
    assert image_drift.warning(facts) is None


def test_unknown_without_stamp(tmp_path):
    facts = image_drift.image_drift(stamp={}, env={"MATRX_IMAGE_VERSION": "abc1234"},
                                    opt_out_file=tmp_path / "x", probe_system=False)
    assert facts["status"] == "unknown"
    assert image_drift.warning(facts) is None


def test_opt_out_is_reported(tmp_path):
    flag = tmp_path / "auto-update.off"
    flag.write_text("")
    facts = image_drift.image_drift(stamp=STAMP, env={"MATRX_IMAGE_VERSION": "1bd5a91"},
                                    opt_out_file=flag, probe_system=False)
    assert facts["auto_update_opted_out"] is True
    assert "opted out" in facts["message"]


def test_opt_out_path_matches_orchestrator_policy():
    # The orchestrator checks the same file (auto_update_policy.OPT_OUT_FILE).
    assert str(image_drift.OPT_OUT_FILE) == "/home/agent/.matrx/auto-update.off"


def test_system_probe_never_raises():
    facts = image_drift.image_drift(stamp=STAMP, env={"MATRX_IMAGE_VERSION": "1bd5a91"})
    assert isinstance(facts["stale_signals"], list)


def test_prune_failed_trees_keeps_newest(tmp_path):
    target = tmp_path / "sdk"
    target.mkdir()
    for ts in (100, 200, 300, 400, 500):
        (tmp_path / f"sdk.failed-{ts}").mkdir()
    (tmp_path / "sdk.failed-notanumber").mkdir()
    removed = selfupdate.prune_failed_trees(str(target), keep=3)
    assert sorted(os.path.basename(p) for p in removed) == ["sdk.failed-100", "sdk.failed-200"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["sdk", "sdk.failed-300", "sdk.failed-400", "sdk.failed-500", "sdk.failed-notanumber"]
