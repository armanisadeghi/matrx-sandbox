"""Readiness is a phase the box reports — never a wall clock the host guesses.

**The class this replaces.** ``_wait_for_ready`` polled for
``/tmp/.sandbox_ready`` under a hardcoded ``timeout = 120``. Everything the
entrypoint does before that marker — for the EC2 tier, the S3 hot-home
down-sync — had to fit inside those two minutes or the orchestrator declared
the box ``failed``, tore the create down, and burned one of the caller's
admission slots. It did this *while the box was booting normally*: sbx-d374bfeef3dd
(2026-09-15) and every subsequent EC2 create for admin@admin.com logged
``[5/5] Sandbox is READY`` in its own entrypoint log minutes AFTER the
orchestrator had already given up, because that user's home is ~8,600 files.
The number 120 was never a property of the system; it was a property of a
small test home.

**The rule now.** The box publishes which phase of boot it is in, and how far
through a long phase it has got. The orchestrator waits on the *phase signal*,
with a per-phase budget that is an operator knob
(``infrastructure.sandbox.ready_timeout_seconds`` and
``…home_sync_timeout_seconds``). A phase that is visibly advancing is never
killed by another phase's clock, and a box counts as up the moment its SDK
answers — even while the home sync is still running, whose progress is
surfaced to the caller rather than being a reason to kill the box. Any timeout
that does happen names the phase it died in and the count it reached.

Backward compatibility is deliberate: a container built from an image that
predates the phase markers reports no phase, and is then judged by the
(generous, operator-owned) ``ready_timeout_seconds`` budget against the same
readiness signals the old code used. The 120-second wall is gone either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Phase names the image publishes into ``/tmp/matrx-boot/phase``.
#: Keep in sync with ``sandbox-image/scripts/boot-phase.sh``.
PHASE_CONTAINER = "container"
PHASE_HOME_SYNC = "home_sync"
PHASE_COLD_MOUNT = "cold_mount"
PHASE_ENVIRONMENT = "environment"
PHASE_SDK = "sdk"
PHASE_CLOUD_FILES = "cloud_files"
PHASE_READY = "ready"

KNOWN_PHASES = (
    PHASE_CONTAINER, PHASE_HOME_SYNC, PHASE_COLD_MOUNT, PHASE_ENVIRONMENT,
    PHASE_SDK, PHASE_CLOUD_FILES, PHASE_READY,
)

#: Phases whose budget comes from ``home_sync_timeout_seconds``. Everything
#: else — including an unreported phase from a pre-marker image — is budgeted
#: by ``ready_timeout_seconds``.
LONG_HAUL_PHASES = (PHASE_HOME_SYNC,)

BOOT_DIR = "/tmp/matrx-boot"

#: One exec, no dependency on anything the image has to provide. An image
#: without the phase files answers with empty values rather than failing, so
#: this probe works against every container in the fleet today.
PROBE_SCRIPT = (
    f'p=$(cat {BOOT_DIR}/phase 2>/dev/null || true); '
    f'g=$(cat {BOOT_DIR}/progress 2>/dev/null || true); '
    'r=no; [ -f /tmp/.sandbox_ready ] && r=yes; '
    's=no; curl -sf --max-time 2 -o /dev/null http://127.0.0.1:8000/health 2>/dev/null && s=yes; '
    'echo "phase=$p"; echo "progress=$g"; echo "ready=$r"; echo "sdk=$s"'
)

_PROGRESS = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


@dataclass(frozen=True)
class BootSnapshot:
    """What the box says about itself right now."""

    phase: str | None = None
    files_done: int | None = None
    files_total: int | None = None
    sdk_up: bool = False
    ready_marker: bool = False
    raw: str = field(default="", repr=False)

    @property
    def usable(self) -> bool:
        """The box can be handed to its owner.

        The SDK answering is the real signal — it is what every client call
        goes through. The entrypoint's own ready marker is honoured too, for
        the aidream/slim boxes whose daemon start is the last boot step.
        """
        return self.sdk_up or self.ready_marker

    @property
    def budget_phase(self) -> str:
        return self.phase or PHASE_CONTAINER

    def progress_text(self) -> str | None:
        if self.files_total:
            return f"{self.files_done or 0}/{self.files_total} files"
        if self.files_done:
            return f"{self.files_done} files"
        return None

    def briefing(self) -> str | None:
        """The line a person reads while the box is still coming up."""
        if self.phase == PHASE_HOME_SYNC:
            counted = self.progress_text()
            return (
                f"home sync in progress: {counted}" if counted
                else "home sync in progress"
            )
        if self.phase and self.phase != PHASE_READY:
            counted = self.progress_text()
            return f"{self.phase.replace('_', ' ')} in progress" + (
                f": {counted}" if counted else ""
            )
        return None


def parse_probe(text: str) -> BootSnapshot:
    """Read the probe's ``key=value`` lines. Unknown/garbled input is inert."""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()

    phase = values.get("phase") or None
    if phase is not None and phase not in KNOWN_PHASES:
        # A newer image naming a phase this orchestrator has never heard of is
        # not an error — it is simply budgeted as an ordinary phase.
        phase = phase[:64]

    done = total = None
    match = _PROGRESS.match(values.get("progress", ""))
    if match:
        done, total = int(match.group(1)), int(match.group(2))

    return BootSnapshot(
        phase=phase,
        files_done=done,
        files_total=total or None,
        sdk_up=values.get("sdk") == "yes",
        ready_marker=values.get("ready") == "yes",
        raw=(text or "")[:400],
    )


def phase_budget(phase: str | None, *, ready_budget: float, home_sync_budget: float) -> float:
    """Seconds this phase alone is allowed, before it must show movement."""
    return home_sync_budget if phase in LONG_HAUL_PHASES else ready_budget


def advanced(previous: BootSnapshot | None, current: BootSnapshot) -> bool:
    """True when the box is demonstrably doing something since last poll.

    Movement resets the phase clock. This is what makes the budget a
    *liveness* budget instead of a wall clock: a home sync of any size passes
    as long as it keeps copying files, and a sync that has genuinely wedged
    still dies on schedule.
    """
    if previous is None:
        return True
    return (
        previous.phase != current.phase
        or (previous.files_done or 0) != (current.files_done or 0)
        or previous.sdk_up != current.sdk_up
        or previous.ready_marker != current.ready_marker
    )


def timeout_reason(snapshot: BootSnapshot | None, budget: float, elapsed: float) -> str:
    """Why this box was given up on — phase and count, never a bare number."""
    if snapshot is None:
        return (
            f"sandbox never answered the boot probe (no phase reported) after "
            f"{elapsed:.0f}s; phase budget {budget:.0f}s"
        )
    phase = snapshot.budget_phase
    counted = snapshot.progress_text()
    reached = f", reached {counted}" if counted else ""
    return (
        f"sandbox stalled in boot phase {phase!r}{reached}: no progress for "
        f"{budget:.0f}s (total boot {elapsed:.0f}s). "
        + (
            "Raise infrastructure.sandbox.home_sync_timeout_seconds if this "
            "user's home legitimately takes longer."
            if phase in LONG_HAUL_PHASES
            else "Raise infrastructure.sandbox.ready_timeout_seconds if this "
                 "phase legitimately takes longer."
        )
    )


__all__ = [
    "BOOT_DIR", "KNOWN_PHASES", "LONG_HAUL_PHASES", "PROBE_SCRIPT",
    "PHASE_CLOUD_FILES", "PHASE_COLD_MOUNT", "PHASE_CONTAINER",
    "PHASE_ENVIRONMENT", "PHASE_HOME_SYNC", "PHASE_READY", "PHASE_SDK",
    "BootSnapshot", "advanced", "parse_probe", "phase_budget", "timeout_reason",
]
