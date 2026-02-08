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
