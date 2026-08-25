from pathlib import Path

import pytest

from orchestrator.config import HostTierUnconfiguredError, Settings
from orchestrator import storage_layout


def test_resolver_is_actionable_and_safe_path_survives() -> None:
    settings = Settings(host_tier="", sandbox_store="memory", stage="local")
    with pytest.raises(HostTierUnconfiguredError) as exc:
        settings.resolve_host_tier()
    message = str(exc.value)
    assert "MATRX_HOST_TIER" in message
    assert "'ec2' or 'hosted'" in message
    assert settings.resolve_host_tier("hosted") == "hosted"


def test_real_storage_call_refuses_ec2_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_layout.settings, "host_tier", "")
    user_id = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(HostTierUnconfiguredError):
        storage_layout.resolve_user_storage(user_id, None)
    assert storage_layout.resolve_user_storage(user_id, "hosted").tier == "hosted"


def test_source_guard_bans_ec2_fallbacks() -> None:
    root = Path(__file__).resolve().parents[1] / "orchestrator"
    source = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert 'host_tier or "ec2"' not in source
    assert 'else "ec2"' not in source


def test_deploys_refuse_missing_or_wrong_host_tier() -> None:
    repo = Path(__file__).resolve().parents[2]
    ec2_deploy = (repo / "scripts" / "deploy-ec2.sh").read_text()
    hosted_deploy = (repo / "scripts" / "deploy-hosted.sh").read_text()

    assert 'HOST_TIER=$(resolve_setting MATRX_HOST_TIER)' in ec2_deploy
    assert '[ "$HOST_TIER" = "ec2" ]' in ec2_deploy
    assert "^MATRX_HOST_TIER=hosted" in hosted_deploy
