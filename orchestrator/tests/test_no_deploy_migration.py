"""Control-plane deployment must never invoke a user sandbox image swap."""
from pathlib import Path

import pytest
from fastapi import HTTPException


def test_deploy_scripts_have_no_implicit_fleet_migration_call():
    root = Path(__file__).resolve().parents[2]
    paths = list((root / "scripts").rglob("*.sh")) + list((root / ".github/workflows").glob("*.yml"))
    offenders = [str(p.relative_to(root)) for p in paths if "/migrate-all" in p.read_text()]
    assert not offenders, f"Implicit migration hook in deployment: {offenders}"


@pytest.mark.asyncio
async def test_bulk_migration_route_fails_closed_during_preservation_hold():
    """A Manager bulk action cannot bypass the disabled migration gates."""
    from orchestrator.main import migrate_all

    with pytest.raises(HTTPException) as raised:
        await migrate_all()

    assert raised.value.status_code == 409
    assert raised.value.detail["status"] == "fleet_migration_disabled"
