"""The sandbox fleet's settings — ``platform.feature_knob`` rows, read-only.

Authority: common-docs/projects/unified-settings-platform/DECISIONS.md USD-5
(Arman, 2026-09-10): *"Settings system. 1 system. Never an env var. Env values
are only for secrets, not for controlling behavior."*

Until 2026-09-11 the orchestrator's shape — container limits, session
lifetime, warm-pool size, retention, the auto-migrate gates — rode
``MATRX_*`` environment variables (orchestrator/config.py). Two orchestrators
running identical code could disagree about all of it, invisibly, and nobody
outside the host's systemd unit could see the values. They are rows now
(feature ``infrastructure.sandbox``, seeded by aidream migration 0636), read
through the store — the orchestrator's ONE platform-database connection — with
a short process cache, so an admin's change lands on every orchestrator within
:data:`KNOB_CACHE_TTL_SECONDS` and no deploy.

**No fallback constants.** A missing row RAISES :class:`KnobNotRegisteredError`;
a store with no database RAISES :class:`KnobSourceUnavailableError` (the
in-memory store, until a test seeds it). A default frozen here would be
exactly the env var in a new coat, and an admin turning the knob would change
nothing. Modelled on aidream's ``packages/matrx-seo/matrx_seo/knobs.py``.
"""

from __future__ import annotations

import time
from typing import Any

from orchestrator.store import KnobSourceUnavailableError  # re-exported

FEATURE = "infrastructure.sandbox"

#: How long a resolved feature is trusted before it is re-read.
KNOB_CACHE_TTL_SECONDS = 60.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class KnobNotRegisteredError(RuntimeError):
    """The code asked for a knob with no row. Loud on purpose: the code and
    the registry disagree, and guessing here would make the settings UI a lie."""

    def __init__(self, feature: str, key: str) -> None:
        self.feature = feature
        self.key = key
        super().__init__(
            f"feature knob {feature!r}.{key!r} is not registered in "
            f"platform.feature_knob. Seed the row (aidream db/migrations, the "
            f"0636 shape) with its value, range, basis and review date rather "
            f"than restoring a constant or an environment variable."
        )


def clear_knob_cache() -> None:
    """Drop the process cache. Tests, and any caller that just wrote a knob."""
    _cache.clear()


async def _feature_values(feature: str) -> dict[str, Any]:
    cached = _cache.get(feature)
    now = time.monotonic()
    if cached is not None and now - cached[0] < KNOB_CACHE_TTL_SECONDS:
        return cached[1]
    from orchestrator.sandbox_manager import _get_store

    values = await _get_store().feature_knobs(feature)
    _cache[feature] = (time.monotonic(), values)
    return values


async def _raw(feature: str, key: str) -> Any:
    values = await _feature_values(feature)
    if key not in values:
        # One retry against a cold/stale cache before declaring it missing: a
        # knob seeded seconds ago must not raise for a whole TTL.
        _cache.pop(feature, None)
        values = await _feature_values(feature)
        if key not in values:
            raise KnobNotRegisteredError(feature, key)
    return values[key]


async def knob_int(key: str, feature: str = FEATURE) -> int:
    return int(await _raw(feature, key))


async def knob_float(key: str, feature: str = FEATURE) -> float:
    return float(await _raw(feature, key))


async def knob_str(key: str, feature: str = FEATURE) -> str:
    return str(await _raw(feature, key))


async def knob_bool(key: str, feature: str = FEATURE) -> bool:
    return bool(await _raw(feature, key))


__all__ = [
    "FEATURE",
    "KNOB_CACHE_TTL_SECONDS",
    "KnobNotRegisteredError",
    "KnobSourceUnavailableError",
    "clear_knob_cache",
    "knob_bool",
    "knob_float",
    "knob_int",
    "knob_str",
]
