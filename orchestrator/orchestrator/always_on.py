"""The always-on mark: a workspace box that must never be allowed to stay down.

An enrolled Personal Staff person does not "switch in and out of the sandbox".
Their workspace box is ALWAYS ON. The only two honest states are *up* and *an
outage something is actively repairing* — never "you have no box, press a
button". aidream writes the mark on the row and passes it in the create
payload; this module is the orchestrator's half.

THE MARK IS ONE PREDICATE, WRITTEN ONCE. ``labels->>'always_on' = 'true'`` on
``public.sandbox_instances``. No new column: the existing ``labels`` jsonb is
the carrier, so the contract between the two repos needs no DDL. Four call
sites depend on it (the TTL sweep, the retention purge, the container restart
policy, and the revive pass below), which is exactly how a predicate drifts —
so :data:`ALWAYS_ON_SQL_IS` / :data:`ALWAYS_ON_SQL_IS_NOT` and
:func:`is_always_on` are the only spellings any of them may use.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The label key aidream writes, and the create payload carries.
ALWAYS_ON_LABEL = "always_on"

#: SQL for "this row is marked always-on". ``COALESCE`` because ``labels`` is
#: nullable and a missing key yields NULL, which would otherwise make the
#: NEGATED form below silently drop every unmarked row.
ALWAYS_ON_SQL_IS = "COALESCE(labels->>'always_on','') = 'true'"

#: SQL for "this row is NOT marked always-on" — the exclusion the destructive
#: sweeps add to their WHERE.
ALWAYS_ON_SQL_IS_NOT = "COALESCE(labels->>'always_on','') <> 'true'"

#: Statuses a revive may act on: the container is gone, the home survives.
REVIVABLE_STATUSES = frozenset({"stopped", "expired", "failed"})

#: Statuses that mean somebody already has a box up for this person.
LIVE_STATUSES = frozenset({"creating", "starting", "ready", "running"})

#: The two knobs, read the way every other orchestrator knob is read. There is
#: deliberately NO Python default: a missing row must raise
#: ``KnobNotRegisteredError`` (orchestrator/knobs.py) rather than let a
#: constant here become the env var the settings system replaced.
MAX_PER_PASS_KNOB = "always_on_revive_max_per_pass"
MIN_INTERVAL_KNOB = "always_on_revive_min_interval_seconds"


def is_always_on(labels: Any) -> bool:
    """The Python spelling of :data:`ALWAYS_ON_SQL_IS`, and nothing else.

    ``jsonb ->> 'always_on'`` renders both the JSON string ``"true"`` and the
    JSON boolean ``true`` as the text ``'true'``, so both count here. Anything
    else — absent, ``"false"``, ``"True"``, a number — does not, exactly as in
    the SQL. Keep the two in step or the sweeps and the revive will disagree
    about which boxes exist.
    """
    if not isinstance(labels, dict):
        return False
    value = labels.get(ALWAYS_ON_LABEL)
    return value is True or value == "true"


def _pair(sandbox: Any) -> tuple[str, str]:
    return (str(sandbox.user_id), str(getattr(sandbox, "organization_id", "") or ""))


def _status(sandbox: Any) -> str:
    return str(getattr(sandbox.status, "value", sandbox.status))


async def revive_always_on(store) -> dict:
    """Bring every marked box that is DOWN back up. One pass, rate-limited.

    Runs inside the existing 60-second reaper tick — deliberately no new
    scheduler and no new loop, because a second timer is a second thing that
    can be down while the first looks healthy.

    The rules, each of which exists because of a specific way an automatic
    revive goes wrong:

    * **Tier-scoped.** ``sandbox_instances`` is shared by the EC2 and hosted
      orchestrators. A hosted orchestrator reviving an ``ec2`` row would try to
      spawn a container for a home that lives on another host.
    * **Newest row per (user, organization).** A person's history is a chain of
      rows; only the last one names their current workspace.
    * **Never when a live row exists.** Somebody — the person, aidream, a
      concurrent tick — already brought it back. A second box would burn their
      admission slot and split their attention across two machines.
    * **Never twice inside the minimum interval.** THE ONLY LOOP BRAKE, and it
      is unconditional on purpose: a box that cannot boot must not be
      resurrected every 60 seconds forever. Bounding on the newest row's age
      bounds the rate to one revive per person per interval whatever the
      failure mode is — including failure modes nobody has thought of yet.
    * **At most ``max_per_pass``, and the remainder is LOGGED.** A silent
      truncation is the difference between "we are working through a backlog"
      and "we forgot about you".

    Returns a summary dict; raises only if the knobs cannot be read (which is
    correct and loud — the reaper's caller logs it and keeps sweeping).
    """
    from orchestrator.config import settings
    from orchestrator.knobs import knob_int

    max_per_pass = await knob_int(MAX_PER_PASS_KNOB)
    min_interval = await knob_int(MIN_INTERVAL_KNOB)
    tier = settings.host_tier

    summary: dict[str, Any] = {
        "candidates": 0, "revived": [], "failed": 0,
        "skipped_live": 0, "skipped_recent": 0, "left_for_next_tick": 0,
    }

    # ``list()`` already excludes soft-deleted rows, which is the ``deleted_at
    # IS NULL`` half of the selection.
    rows = [
        row for row in await store.list()
        if str(getattr(row.tier, "value", row.tier) or "") == tier
    ]

    newest: dict[tuple[str, str], Any] = {}
    live_pairs: set[tuple[str, str]] = set()
    for row in rows:
        key = _pair(row)
        # A live box counts whether or not IT carries the mark: the question is
        # "does this person have a workspace up", not "whose row is it".
        if _status(row) in LIVE_STATUSES:
            live_pairs.add(key)
        current = newest.get(key)
        if current is None or row.created_at > current.created_at:
            newest[key] = row

    now = _utcnow()
    candidates: list[Any] = []
    for key, row in newest.items():
        if not is_always_on(row.labels):
            continue
        if _status(row) not in REVIVABLE_STATUSES:
            continue
        summary["candidates"] += 1
        if key in live_pairs:
            summary["skipped_live"] += 1
            continue
        age = (now - row.created_at).total_seconds()
        if age < min_interval:
            summary["skipped_recent"] += 1
            logger.info(
                "Always-on: holding off on %s for %s/%s — its newest workspace "
                "row is only %.0fs old (%s=%ds).",
                row.sandbox_id, key[0], key[1], age, MIN_INTERVAL_KNOB, min_interval,
            )
            continue
        candidates.append(row)

    # Longest-down first: the person who has been without a box the longest is
    # served before the one who just lost theirs.
    candidates.sort(key=lambda row: row.created_at)

    if len(candidates) > max_per_pass:
        summary["left_for_next_tick"] = len(candidates) - max_per_pass
        logger.warning(
            "Always-on: %d workspace(s) are down; reviving %d this pass "
            "(%s=%d) and leaving %d for the next tick.",
            len(candidates), max_per_pass, MAX_PER_PASS_KNOB, max_per_pass,
            summary["left_for_next_tick"],
        )
        candidates = candidates[:max_per_pass]

    for row in candidates:
        try:
            new_id = await _revive_one(store, row)
        except Exception as exc:  # noqa: BLE001 — one bad revive never ends the tick
            summary["failed"] += 1
            logger.warning(
                "ALWAYS-ON REVIVE FAILED for %s (user=%s org=%s): %s. The next "
                "reaper tick will retry no sooner than %s=%ds from now.",
                row.sandbox_id, row.user_id, row.organization_id, exc,
                MIN_INTERVAL_KNOB, min_interval,
            )
            continue
        if new_id is None:
            continue
        summary["revived"].append(new_id)
        # WARNING, not INFO: this is an automatic intervention on somebody's
        # machine. An intervention that does not announce itself is the
        # silent-failure class.
        logger.warning(
            "ALWAYS-ON REVIVE: workspace for user=%s org=%s was %s as %s; "
            "brought back up as %s.",
            row.user_id, row.organization_id, _status(row), row.sandbox_id, new_id,
        )
    return summary


async def _revive_one(store, row) -> str | None:
    """Confirm the row is still down under the lifecycle fence, then resume.

    🚨 THE LEASE CANNOT WRAP THE RESUME. Every lifecycle lease takes
    ``lifecycle-<home_key>`` EXCLUSIVE, and a resume mints a NEW sandbox_id on
    the SAME home, so its own ``create_sandbox`` lease would block on ours
    forever (``flock`` conflicts between two descriptors in one process) — and
    the journal's ``_pending_conflict`` matches on ``home_key`` as well, so
    even a non-blocking path would be denied. So the lease fences the DECISION
    — the re-read that proves the row is still terminal and still ours — and
    the resume then takes the identical lease itself, one operation at a time,
    exactly as when a person presses the button.
    """
    from orchestrator.home_identity import home_key
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    from orchestrator.routes.sandboxes import resume_sandbox

    sandbox_id = row.sandbox_id
    async with hosted_operation_lease(sandbox_id, home_key(row), lifecycle=True):
        fresh = await store.get(sandbox_id)
        life = await store.get_lifecycle(sandbox_id)
        still_down = bool(
            fresh is not None and life is not None and not life.get("deleted")
            and life.get("status") in REVIVABLE_STATUSES
            and _status(fresh) in REVIVABLE_STATUSES
        )
    if not still_down:
        logger.info(
            "Always-on: %s was already brought back between selection and "
            "revive; leaving it alone.", sandbox_id,
        )
        return None

    # The orchestrator's OWN resume path — the same code POST
    # /sandboxes/{id}/resume runs. A revive must be indistinguishable from a
    # person pressing resume: same admission accounting, same per-user volume,
    # a new row with the old one kept as history. A second implementation here
    # would be a second set of rules to keep in step.
    new_sandbox = await resume_sandbox(sandbox_id)
    return new_sandbox.sandbox_id


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


__all__ = [
    "ALWAYS_ON_LABEL",
    "ALWAYS_ON_SQL_IS",
    "ALWAYS_ON_SQL_IS_NOT",
    "LIVE_STATUSES",
    "MAX_PER_PASS_KNOB",
    "MIN_INTERVAL_KNOB",
    "REVIVABLE_STATUSES",
    "is_always_on",
    "revive_always_on",
]
