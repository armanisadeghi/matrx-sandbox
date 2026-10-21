"""SessionStart hook: kick the in-box repo tidy + session brief on binding.

Tracker 1734465c. The SDK (``matrx_agent.session_brief``) tidies the
checkouts under ``~/repos`` (fetch --prune, leave long-merged remote-deleted
branches, fast-forward a clean default branch; never touches dirty trees,
unpushed or diverged work, or stashes) and writes ``~/.matrx/session-brief.md``
plus a JSON twin that aidream folds into the agent's start-of-session context.

The daemon already runs it once per boot. This hook runs it again whenever an
agent binds, so a long-lived box is brought current at the moment someone
starts working on it. It runs AFTER ``sdk_refresh`` so an old box has the
command, as the ``agent`` user (never root: the files live in the home), and
DETACHED: the exec returns at once and a binding never waits on a fetch.

* **A knob**, ``infrastructure.sandbox.session_brief_on_binding``. Unreadable
  or off -> skipped, loudly in the log, never fatal.
* **Never fatal.** Every failure becomes a status dict.
"""

from __future__ import annotations

import contextlib
import logging

from orchestrator import activity, sandbox_manager
from orchestrator.knobs import knob_bool
from orchestrator.models import SandboxResponse

logger = logging.getLogger(__name__)

KNOB = "session_brief_on_binding"

#: `mtx session start --detach` forks the work into its own session and
#: returns; older SDKs without the command fall through to a clear status.
COMMAND = (
    "if command -v mtx >/dev/null 2>&1 && mtx session start --help >/dev/null 2>&1; then "
    "mtx session start --detach --trigger binding >/dev/null 2>&1 && echo started; "
    "else echo unsupported; fi"
)


def _result(status: str, **extra) -> dict:
    return {"hook": "session_start.session_brief", "status": status, **extra}


async def kick_session_brief(sandbox: SandboxResponse) -> dict:
    """Always returns a dict; never raises."""
    sandbox_id = sandbox.sandbox_id
    try:
        enabled = await knob_bool(KNOB)
    except Exception as exc:  # noqa: BLE001 — KnobNotRegistered/SourceUnavailable or a store fault
        logger.warning(
            "SESSION BRIEF HOOK UNAVAILABLE for %s: %s. Seed platform.feature_knob "
            "'infrastructure.sandbox.%s'; until then the brief refreshes only at boot.",
            sandbox_id, exc, KNOB,
        )
        return _result("unavailable", reason=f"knob {KNOB} unreadable: {exc}")
    if not enabled:
        return _result("disabled", reason=f"infrastructure.sandbox.{KNOB} is off")
    if activity.is_migrating(sandbox_id):
        return _result("skipped", reason="the box is migrating")
    try:
        with _preserved_cwd(sandbox_id):
            exit_code, stdout, stderr, _cwd = await sandbox_manager.exec_in_sandbox(
                sandbox_id=sandbox_id,
                command=COMMAND,
                timeout=20,
                user="agent",
                cwd="/home/agent",
            )
    except Exception as exc:  # noqa: BLE001 — never fatal to a binding
        logger.warning("session brief kick failed for %s: %s", sandbox_id, exc)
        return _result("failed", reason=f"{type(exc).__name__}: {exc}")
    out = (stdout or "").strip()
    if exit_code == 0 and out.endswith("started"):
        return _result("started")
    if out.endswith("unsupported"):
        return _result("unsupported", reason="the box's SDK has no `mtx session start` yet")
    return _result("failed", exit_code=exit_code, reason=(stderr or out or "")[-500:])


@contextlib.contextmanager
def _preserved_cwd(sandbox_id: str):
    """The exec helper caches the directory each call lands in, and that cache
    is the user's shell location. A background kick must not move it."""
    prior = sandbox_manager._sandbox_cwd.get(sandbox_id)
    try:
        yield
    finally:
        if prior is None:
            sandbox_manager._sandbox_cwd.pop(sandbox_id, None)
        else:
            sandbox_manager._sandbox_cwd[sandbox_id] = prior
