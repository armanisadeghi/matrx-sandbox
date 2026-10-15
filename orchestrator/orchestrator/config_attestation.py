"""The orchestrator's config attestation: report which ``platform.feature_knob`` rows it read, per build.

System of record: common-docs/systems/platform/app-config/attestation/FEATURE.md. The implementation is
the shared ``matrx_utils.config_attestation`` (one Python implementation for every server); this module only
wires it: app ``matrx-sandbox``, version = the package version, build sha = the deployer-stamped
``SOURCE_SHA``, sink = ``platform.config_reads_report_server`` on the store's database connection.

Nothing fails silently: a process that cannot report (no stamped sha, the shared module not installed, an
in-memory store) says why at boot and on every ``GET /health/config-attestation``.
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Any

APP = "matrx-sandbox"
_log = logging.getLogger(__name__)
_state: dict[str, Any] = {"enabled": False, "disabled_reason": "not started", "version_floor": {}}


def _recorder_module():
    try:
        from matrx_utils import config_attestation
    except ImportError as exc:
        raise RuntimeError(f"matrx_utils.config_attestation is not installed ({exc}); upgrade matrx-utils") from exc
    return config_attestation


def record_read(feature: str, key: str, source: str = "db") -> None:
    if _state["enabled"]:
        _recorder_module().record_read("knob", feature, key, source)


def record_missing(feature: str, key: str) -> None:
    if _state["enabled"]:
        _recorder_module().record_missing("knob", feature, key)


def instance_key() -> str:
    return re.sub(r"[^A-Za-z0-9._:-]", "-", socket.gethostname())[:100] or "unknown-host"


async def start(service_version: str, source_sha: str) -> None:
    """Install and start the recorder (lifespan). Never raises; a refusal is loud and on /health."""
    from orchestrator.sandbox_manager import _get_store

    try:
        ca = _recorder_module()
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise RuntimeError(f"no stamped build sha (SOURCE_SHA={source_sha!r}); deploy stamps MATRX_SOURCE_SHA")
        store = _get_store()

        async def sink(report: dict[str, Any]) -> None:
            await store.report_config_reads(report, {"instance_key": instance_key(), "hostname": socket.gethostname()})

        async def flush_minutes() -> float:
            return float((await store.feature_knobs("platform.config_attestation"))["flush_interval_minutes"])

        recorder = ca.ConfigReadRecorder(app=APP, app_version=service_version, build_sha=source_sha,
                                         sink=sink, flush_minutes=flush_minutes)
    except Exception as exc:  # noqa: BLE001 — announced at boot and on every health read
        _state.update(enabled=False, disabled_reason=f"{type(exc).__name__}: {exc}")
        _log.error("config attestation DISABLED for this orchestrator: %s", _state["disabled_reason"])
        return
    ca.install(recorder)
    _state.update(enabled=True, disabled_reason=None)
    recorder.start()
    await _check_version_floor(recorder, store)


async def _check_version_floor(recorder: Any, store: Any) -> None:
    """The one app_config key the orchestrator reads: its own row's floor. Below it = counted, never refused."""
    try:
        floor = await store.app_config_floor(APP)
    except Exception as exc:  # noqa: BLE001 — counted on health
        recorder.count("version_floor_check_failures")
        _state["version_floor"] = {"error": str(exc)[:200]}
        _log.error("config attestation: version floor check failed: %s", exc)
        return
    ok = bool(re.fullmatch(r"\d+\.\d+\.\d+", floor or ""))
    recorder.read("app_config", None, "min_supported_app_version", "db", invalid=not ok)
    _state["version_floor"] = {"floor": floor, "version": recorder.app_version}
    if ok and tuple(map(int, recorder.app_version.split("."))) < tuple(map(int, floor.split("."))):
        recorder.count("version_floor_violations")
        _log.error("config attestation: orchestrator %s is BELOW min_supported_app_version %s",
                   recorder.app_version, floor)


async def stop() -> None:
    if not _state["enabled"]:
        return
    ca = _recorder_module()
    r = ca.active()
    if r is not None:
        await r.stop()
        ca.install(None)
    _state["enabled"] = False


def health() -> dict[str, Any]:
    if not _state["enabled"]:
        return {"app": APP, "enabled": False, "disabled_reason": _state["disabled_reason"]}
    r = _recorder_module().active()
    return {"enabled": True, **(r.health() if r else {}), "version_floor": _state["version_floor"]}
