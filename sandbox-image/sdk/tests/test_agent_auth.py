"""Tests for the daemon-side per-sandbox auth helper (fail-open behavior)."""

from __future__ import annotations

import importlib

import pytest


def _reload_auth(monkeypatch, token: str | None):
    if token is None:
        monkeypatch.delenv("MATRX_AGENT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MATRX_AGENT_TOKEN", token)
    import matrx_agent.api._auth as auth
    return importlib.reload(auth)


def test_fail_open_when_unset(monkeypatch):
    auth = _reload_auth(monkeypatch, None)
    assert auth.enforcement_enabled() is False
    assert auth.token_ok(None) is True
    assert auth.token_ok("anything") is True


def test_enforced_when_set(monkeypatch):
    auth = _reload_auth(monkeypatch, "sekret")
    assert auth.enforcement_enabled() is True
    assert auth.token_ok("sekret") is True
    assert auth.token_ok("wrong") is False
    assert auth.token_ok(None) is False


class _FakeWS:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_ws_token_via_header(monkeypatch):
    auth = _reload_auth(monkeypatch, "sekret")
    assert auth.ws_token_ok(_FakeWS(headers={auth.HEADER_NAME: "sekret"})) is True
    assert auth.ws_token_ok(_FakeWS(headers={auth.HEADER_NAME: "no"})) is False


def test_ws_token_via_query(monkeypatch):
    auth = _reload_auth(monkeypatch, "sekret")
    assert auth.ws_token_ok(_FakeWS(query={auth.QUERY_NAME: "sekret"})) is True
    assert auth.ws_token_ok(_FakeWS()) is False


def test_ws_fail_open(monkeypatch):
    auth = _reload_auth(monkeypatch, None)
    assert auth.ws_token_ok(_FakeWS()) is True


# Restore a clean module for any later imports in the session.
@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("MATRX_AGENT_TOKEN", raising=False)
    import matrx_agent.api._auth as auth
    importlib.reload(auth)
