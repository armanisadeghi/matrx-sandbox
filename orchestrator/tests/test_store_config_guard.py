"""MATRX_SANDBOX_STORE must never be reachable by omission.

The defect these tests pin: ``sandbox_store`` defaulted to ``"memory"``, so a
host that never set (or misspelled) the variable booted happily on an in-memory
store, lost every ``sandbox_instances`` row on restart, and said so only in a
``logger.info``. Nothing crashed; the loss was invisible.

Each test below is one way that silence could come back.
"""

from __future__ import annotations

import pytest

from orchestrator.config import Settings


@pytest.fixture
def deployed(monkeypatch):
    """Make the process look like a real host, not a pytest run.

    Deleting PYTEST_CURRENT_TEST is not enough — pytest re-sets it for the call
    phase — so the test-run probe itself is pinned False.
    """
    monkeypatch.setattr(Settings, "is_test_run", property(lambda self: False))


def _settings(**kwargs) -> Settings:
    # _env_file=None so a developer's local .env can't change the outcome.
    return Settings(_env_file=None, **kwargs)


def test_unset_store_refuses_to_start(deployed):
    with pytest.raises(RuntimeError) as exc:
        _settings(sandbox_store="", stage="production").resolve_sandbox_store()
    msg = str(exc.value)
    assert "MATRX_SANDBOX_STORE" in msg          # names the variable
    assert "postgres" in msg and "memory" in msg  # names the accepted values


def test_unset_store_refuses_even_when_stage_is_unknown(deployed):
    """Fail closed: a host that didn't declare its stage is treated as deployed."""
    with pytest.raises(RuntimeError):
        _settings(sandbox_store="", stage="").resolve_sandbox_store()


def test_misspelled_store_refuses_instead_of_degrading(deployed):
    with pytest.raises(RuntimeError) as exc:
        _settings(sandbox_store="postgress", stage="local").resolve_sandbox_store()
    assert "not a recognized store" in str(exc.value)


def test_memory_is_refused_on_a_deployed_host(deployed):
    with pytest.raises(RuntimeError) as exc:
        _settings(sandbox_store="memory", stage="production").resolve_sandbox_store()
    assert "loses EVERY sandbox_instances row" in str(exc.value)


def test_memory_is_refused_on_the_hosted_dev_server(deployed):
    """stage=development is still a deployment — only stage=local may go memory."""
    with pytest.raises(RuntimeError):
        _settings(
            sandbox_store="memory", stage="development", host_tier="hosted"
        ).resolve_sandbox_store()


def test_memory_is_allowed_when_explicitly_local(deployed):
    assert _settings(sandbox_store="memory", stage="local").resolve_sandbox_store() == "memory"


def test_postgres_requires_a_database_url(deployed):
    with pytest.raises(RuntimeError) as exc:
        _settings(sandbox_store="postgres", database_url="", stage="production").resolve_sandbox_store()
    assert "MATRX_DATABASE_URL" in str(exc.value)


def test_postgres_resolves(deployed):
    s = _settings(
        sandbox_store="  Postgres  ",
        database_url="postgresql://u:p@h:5432/db",
        stage="production",
    )
    assert s.resolve_sandbox_store() == "postgres"


def test_pytest_runs_may_default_to_memory():
    """The one implicit path — and only because PYTEST_CURRENT_TEST is set."""
    assert _settings(sandbox_store="", stage="").resolve_sandbox_store() == "memory"
