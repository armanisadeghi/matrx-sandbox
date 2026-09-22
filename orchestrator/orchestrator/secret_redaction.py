"""Secret values never reach the database.

A ``sandbox_instances`` row is readable by far more eyes than the box it
describes: the two admin portals, MCP ``execute_sql``, exports, log
pipelines, agent transcripts, and anybody holding table read access. On
2026-09-22 three live rows were found carrying the owner's DECRYPTED vault
secrets in plain text under ``config.env`` — a GitHub token and a Bright
Data API key among them — because ``POST /sandboxes`` accepts a caller
``config.env`` block for the ``docker run -e KEY=value`` merge and the whole
``config`` dict was then persisted verbatim.

The rule this module enforces, at the ONE point where ``config`` becomes a
database value (:func:`orchestrator.store._config_with_boot`):

    A secret VALUE is fetched from the vault at boot and injected into the
    container's process environment. It is NEVER written back to a row.
    What a row may carry is the NAME, the count, and when it happened.

``config.env`` therefore never survives serialization: it is replaced by
``config.env_names``, which is a diagnostic (names, count) and not a
credential. The same shape is what the production scrub migration
``0925_redact_sandbox_config_env_secret_values`` leaves behind on rows
written before this guard existed, with a ``redacted_at`` stamp.

Guard: ``aidream/scripts/check_no_secret_values_at_rest.py --live``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Keys on ``config`` whose CONTENTS are secret values, not descriptions.
#: Everything else on ``config`` (``secrets_injection``, ``platform_env``,
#: ``browser_profile``, ``organization_id``, ``labels``…) is names/status
#: only and is deliberately left alone.
SECRET_VALUE_BLOCKS: tuple[str, ...] = ("env",)

#: Where the names go once the values are dropped. Suffixed ``_names`` so
#: the guard's rule stays a one-liner: no ``config.env`` at rest, ever.
NAMES_SUFFIX = "_names"

REDACTION_NOTE = (
    "values are never stored at rest — they are fetched from the user's "
    "vault at boot and injected into the container process environment"
)


def redact_secret_values(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a shallow copy of ``config`` that is safe to store at rest.

    Every block named in :data:`SECRET_VALUE_BLOCKS` is removed and replaced
    by a ``<block>_names`` diagnostic carrying the sorted NAMES, the count
    and the time of redaction. A config with no such block round-trips
    unchanged (same keys, same values), so this is safe to apply to every
    write.
    """
    payload: dict[str, Any] = dict(config or {})
    for block in SECRET_VALUE_BLOCKS:
        if block not in payload:
            continue
        raw = payload.pop(block)
        names: list[str]
        if isinstance(raw, Mapping):
            names = sorted(str(k) for k in raw.keys())
        elif raw in (None, {}, []):
            names = []
        else:
            # Never silently drop a shape we did not expect: say so, and
            # still refuse to persist whatever it was.
            logger.warning(
                "config.%s was %s, not a mapping; dropped from the persisted "
                "row rather than stored",
                block, type(raw).__name__,
            )
            names = []
        if not names:
            # Nothing to describe — do not leave an empty ornament behind.
            continue
        payload[f"{block}{NAMES_SUFFIX}"] = {
            "names": names,
            "count": len(names),
            "redacted_at": datetime.now(timezone.utc).isoformat(),
            "note": REDACTION_NOTE,
        }
        logger.info(
            "config.%s carried %d value(s); persisting names only",
            block, len(names),
        )
    return payload


def config_carries_secret_values(config: Mapping[str, Any] | None) -> bool:
    """True when ``config`` would write secret VALUES to a row.

    The predicate the unit guards and the live checker share, so "what
    counts as a leak" is defined exactly once.
    """
    payload = config or {}
    for block in SECRET_VALUE_BLOCKS:
        raw = payload.get(block)
        if isinstance(raw, Mapping) and len(raw) > 0:
            return True
        if raw not in (None, {}, [], "") and not isinstance(raw, Mapping):
            return True
    return False
