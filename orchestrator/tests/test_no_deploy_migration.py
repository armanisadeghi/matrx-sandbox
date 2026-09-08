"""Control-plane deployment must never invoke a user sandbox image swap."""
from pathlib import Path


def test_deploy_scripts_have_no_implicit_fleet_migration_call():
    root = Path(__file__).resolve().parents[2]
    paths = list((root / "scripts").rglob("*.sh")) + list((root / ".github/workflows").glob("*.yml"))
    offenders = [str(p.relative_to(root)) for p in paths if "/migrate-all" in p.read_text()]
    assert not offenders, f"Implicit migration hook in deployment: {offenders}"
