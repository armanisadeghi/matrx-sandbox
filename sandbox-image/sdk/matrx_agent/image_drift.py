"""Is this box on the platform's current image? (tracker cf1ab9ab)

A running container cannot change its own image. The SDK refresh
(``selfupdate``) carries ``/opt/sandbox/sdk`` and ``mtx`` forward in place,
and its stamp records the image the host currently serves (``to_version``).
Comparing that with the image this container was BUILT from
(``MATRX_IMAGE_VERSION`` / ``/etc/sandbox-image-version``) tells every box,
with no network and no repo checkout, whether its OS, interpreter and system
packages are stale. ``mtx whoami``, ``mtx self-update --status`` and the
session brief surface it, so an agent never has to guess.

The opt-out file is the same path the orchestrator's automatic-update policy
checks (orchestrator/auto_update_policy.py, ``OPT_OUT_FILE``).

Stdlib only; must run on every image's interpreter, including 3.11.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

OPT_OUT_FILE = Path("/home/agent/.matrx/auto-update.off")
FRESHNESS_DOC = "matrx-sandbox docs/SANDBOX_FRESHNESS.md"


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _os_release() -> str | None:
    text = _read("/etc/os-release") or ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
    return m.group(1) if m else None


def _python3_version() -> str | None:
    try:
        out = subprocess.run(  # noqa: PLW1510 — output read, exit code irrelevant
            ["python3", "--version"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip().replace("Python ", "") or None


def _same(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def image_drift(*, stamp: dict | None = None, env: dict | None = None,
                opt_out_file: Path = OPT_OUT_FILE, probe_system: bool = True) -> dict[str, Any]:
    """Return the box's image-freshness facts. Never raises."""
    env = os.environ if env is None else env
    baked = env.get("MATRX_IMAGE_VERSION") or _read("/etc/sandbox-image-version")
    if stamp is None:
        try:
            from matrx_agent import selfupdate

            stamp = selfupdate.read_stamp() or {}
        except Exception:  # noqa: BLE001 — diagnostics only
            stamp = {}
    current = stamp.get("to_version")
    if baked and current:
        status = "current" if _same(baked, current) else "stale"
    else:
        status = "unknown"
    facts: dict[str, Any] = {
        "status": status,
        "running_image_version": baked,
        "platform_image_version": current,
        "platform_version_seen_at": stamp.get("refreshed_at"),
        "basis": "SDK refresh stamp (to_version) vs MATRX_IMAGE_VERSION",
        "sdk_refreshed_in_place": bool(current),
        "auto_update_opted_out": opt_out_file.exists(),
        "opt_out_file": str(opt_out_file),
    }
    signals: list[str] = []
    if probe_system:
        facts["os"] = _os_release()
        facts["python3"] = _python3_version()
        py = facts["python3"] or ""
        if re.search(r"\d(rc|a|b)\d", py):
            signals.append(f"python3 {py} is a pre-release")
        m = re.match(r"(\d+)\.(\d+)", py)
        if m and (int(m.group(1)), int(m.group(2))) < (3, 12):
            signals.append(f"python3 {py} is older than the current image's 3.12")
        if not Path("/usr/share/zoneinfo").is_dir():
            signals.append("no /usr/share/zoneinfo (tzdata missing)")
        if not Path("/etc/sandbox-image-version").exists():
            signals.append("no /etc/sandbox-image-version (pre-September image)")
    facts["stale_signals"] = signals
    facts["message"] = message(facts)
    return facts


def message(facts: dict[str, Any]) -> str:
    status = facts.get("status")
    run = (facts.get("running_image_version") or "unknown")[:7]
    cur = (facts.get("platform_image_version") or "unknown")[:7]
    if status == "current":
        return f"image {run} is the platform's current image"
    if status == "unknown":
        return (f"image {run}; the platform's current image is unknown here "
                "(no SDK refresh stamp yet)")
    tail = ("this box has opted out of automatic updates" if facts.get("auto_update_opted_out")
            else "a full update needs a container swap: automatic only when the box is idle "
                 "and the staged rollout is on, otherwise the owner's Update in the Manager")
    return (f"STALE IMAGE: running {run}, platform current {cur}. The SDK and mtx are "
            f"refreshed in place; the OS, Python and system packages are not; {tail} "
            f"(see {FRESHNESS_DOC})")


def warning(facts: dict[str, Any] | None = None) -> str | None:
    """One line for briefs/status when the image is stale; None otherwise."""
    facts = facts if facts is not None else image_drift()
    if facts.get("status") != "stale":
        return None
    sig = facts.get("stale_signals") or []
    return facts["message"] + (f" [{'; '.join(sig)}]" if sig else "")
