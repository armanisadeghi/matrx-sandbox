"""Pytest configuration and shared fixtures."""

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test (requires Docker + LocalStack)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration flag is passed."""
    if not config.getoption("--run-integration", default=False):
        skip = pytest.mark.skip(reason="need --run-integration to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests (requires Docker + docker-compose up)",
    )


# ── Fleet settings for tests ────────────────────────────────────────────────
# The orchestrator's shape (container limits, warm pool, retention, migrate
# gates) is `platform.feature_knob` rows under `infrastructure.sandbox`, read
# through the store (orchestrator/knobs.py). Tests run on the in-memory store,
# which has no database, so this is the ONE test seam: the values aidream
# migration 0636 seeded, primed into the knob cache for every test. Change a
# value here only when the migration changes; a test that needs a different
# value overrides it explicitly with `seed_sandbox_knobs`.
SANDBOX_KNOB_TEST_VALUES = {
    "container_cpu_limit": 2.0,
    "container_memory_limit": "4g",
    "max_session_duration_seconds": 7200,
    "shutdown_timeout_seconds": 30,
    "max_command_length": 10000,
    "warm_pool_size": 2,
    "warm_pool_template": "slim",
    "warm_pool_templates": "",
    "auto_migrate": False,
    "migrate_max_per_pass": 2,
    "terminal_retention_days": 7,
    "migrate_recent_heartbeat_seconds": 120,
    "enable_s3_migrate": False,
}


def seed_sandbox_knobs(values: dict | None = None) -> None:
    """Prime the knob cache for a test (far-future stamp: never re-read)."""
    import time

    from orchestrator import knobs

    merged = {**SANDBOX_KNOB_TEST_VALUES, **(values or {})}
    knobs._cache[knobs.FEATURE] = (time.monotonic() + 10**9, merged)


@pytest.fixture(autouse=True)
def _sandbox_knobs_seeded():
    from orchestrator import knobs

    seed_sandbox_knobs()
    yield
    knobs.clear_knob_cache()
