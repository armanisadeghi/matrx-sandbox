from types import SimpleNamespace

from orchestrator.routes import health


def test_shared_ledger_health_counts_only_the_current_host_tier(monkeypatch):
    monkeypatch.setattr(health.settings, "host_tier", "hosted")
    rows = [
        SimpleNamespace(tier="hosted"),
        SimpleNamespace(tier=SimpleNamespace(value="hosted")),
        SimpleNamespace(tier="ec2"),
        SimpleNamespace(tier=None),
    ]

    assert health._sandboxes_for_this_host(rows) == rows[:2]


def test_local_unscoped_health_preserves_all_rows(monkeypatch):
    monkeypatch.setattr(health.settings, "host_tier", "")
    rows = [SimpleNamespace(tier="hosted"), SimpleNamespace(tier="ec2")]

    assert health._sandboxes_for_this_host(rows) == rows
