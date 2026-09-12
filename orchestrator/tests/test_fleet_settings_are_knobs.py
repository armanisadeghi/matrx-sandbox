"""USD-5 (Arman, 2026-09-10): "Never an env var. Env values are only for
secrets, not for controlling behavior."

The orchestrator's shape is `platform.feature_knob` rows (feature
`infrastructure.sandbox`), read through the store. This is the forcing
function (env-vars-are-values-not-toggles.md §4): the retired MATRX_* names
are inert, the config class no longer carries them, the reader never invents
a value, and — with a live database URL — every key the code reads resolves
at today's value through the real Postgres store.
"""

from __future__ import annotations

import os

import pytest

from orchestrator import knobs
from orchestrator.store import InMemorySandboxStore, KnobSourceUnavailableError

RETIRED = (
    "MATRX_CONTAINER_CPU_LIMIT",
    "MATRX_CONTAINER_MEMORY_LIMIT",
    "MATRX_CONTAINER_DISK_LIMIT",
    "MATRX_MAX_SESSION_DURATION_SECONDS",
    "MATRX_SHUTDOWN_TIMEOUT_SECONDS",
    "MATRX_HEALTHCHECK_INTERVAL_SECONDS",
    "MATRX_MAX_COMMAND_LENGTH",
    "MATRX_COMMAND_TIMEOUT_SECONDS",
    "MATRX_WARM_POOL_SIZE",
    "MATRX_WARM_POOL_TEMPLATE",
    "MATRX_WARM_POOL_TEMPLATES",
    "MATRX_AUTO_MIGRATE",
    "MATRX_MIGRATE_MAX_PER_PASS",
    "MATRX_TERMINAL_RETENTION_DAYS",
    "MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS",
    "MATRX_ENABLE_S3_MIGRATE",
)

KEYS = (
    "container_cpu_limit",
    "container_memory_limit",
    "max_session_duration_seconds",
    "shutdown_timeout_seconds",
    "max_command_length",
    "warm_pool_size",
    "warm_pool_template",
    "warm_pool_templates",
    "auto_migrate",
    "migrate_max_per_pass",
    "terminal_retention_days",
    "migrate_recent_heartbeat_seconds",
    "enable_s3_migrate",
)


def test_retired_env_vars_do_nothing(monkeypatch):
    from orchestrator.config import Settings

    for name in RETIRED:
        monkeypatch.setenv(name, "999")
    s = Settings()
    for field in (
        "container_cpu_limit", "container_memory_limit", "container_disk_limit",
        "max_session_duration_seconds", "shutdown_timeout_seconds",
        "healthcheck_interval_seconds", "max_command_length",
        "command_timeout_seconds", "warm_pool_size", "warm_pool_template",
        "warm_pool_templates", "auto_migrate", "migrate_max_per_pass",
        "terminal_retention_days", "migrate_recent_heartbeat_seconds",
        "enable_s3_migrate",
    ):
        assert not hasattr(s, field), f"Settings.{field} is back — that value is a knob now"


@pytest.mark.asyncio
async def test_memory_store_refuses_to_invent_settings():
    store = InMemorySandboxStore()
    with pytest.raises(KnobSourceUnavailableError) as exc:
        await store.feature_knobs(knobs.FEATURE)
    assert "seed_feature_knobs" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_key_raises_not_registered(monkeypatch):
    store = InMemorySandboxStore()
    store.seed_feature_knobs(knobs.FEATURE, {"warm_pool_size": 2})
    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    knobs.clear_knob_cache()
    assert await knobs.knob_int("warm_pool_size") == 2
    with pytest.raises(knobs.KnobNotRegisteredError):
        await knobs.knob_int("no_such_setting")


_DB_URL = os.environ.get("MATRX_DATABASE_URL", "")


@pytest.mark.skipif(not _DB_URL, reason="MATRX_DATABASE_URL absent — live knob read needs the platform DB")
@pytest.mark.asyncio
async def test_live_rows_resolve_through_the_postgres_store(monkeypatch):
    """Every key the orchestrator reads exists live, at the value production ran on 2026-09-11."""
    from orchestrator.store import PostgresSandboxStore

    store = PostgresSandboxStore(_DB_URL)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    knobs.clear_knob_cache()
    try:
        values = await store.feature_knobs(knobs.FEATURE)
        assert set(KEYS) <= set(values), sorted(set(KEYS) - set(values))
        assert await knobs.knob_float("container_cpu_limit") == 2.0
        assert await knobs.knob_str("container_memory_limit") == "4g"
        assert await knobs.knob_int("max_session_duration_seconds") == 7200
        assert await knobs.knob_int("shutdown_timeout_seconds") == 30
        assert await knobs.knob_int("max_command_length") == 10000
        assert await knobs.knob_int("warm_pool_size") == 2
        assert await knobs.knob_str("warm_pool_template") == "slim"
        assert await knobs.knob_str("warm_pool_templates") == ""
        assert await knobs.knob_bool("auto_migrate") is False
        assert await knobs.knob_int("migrate_max_per_pass") == 2
        assert await knobs.knob_int("terminal_retention_days") == 7
        assert await knobs.knob_int("migrate_recent_heartbeat_seconds") == 120
        assert await knobs.knob_bool("enable_s3_migrate") is False
    finally:
        knobs.clear_knob_cache()
        if store._pool is not None:
            await store._pool.close()
