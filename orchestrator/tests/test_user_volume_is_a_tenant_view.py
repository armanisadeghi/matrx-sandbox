"""The hosted home is one tenant's mirror, not one user's drawer.

``/home/agent/cloud-files`` mirrors the user's AI Dream files, and the bridge
became organization-scoped on 2026-09-17: ``/list`` and ``/changes`` now answer
for ONE organization. The volume was still keyed by user alone, so a file
belonging to another of that user's organizations stayed on disk, was never
listed and never reported deleted, and its next edit was refused 409 by the
server — with nothing on the sandbox side able to explain it. Live, 7 users
hold files spanning more than one organization.

The key is therefore (user, organization). These tests hold that line at the
one place the name is made AND at every place it is read, because a single
reader that still derives the name from the user alone puts one tenant's files
under another tenant's mount.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.storage_layout import (
    is_legacy_user_volume,
    resolve_user_storage,
    user_volume_name,
)

USER = "00000000-0000-4000-8000-000000000001"
ORG_A = "00000000-0000-4000-8000-0000000000aa"
ORG_B = "00000000-0000-4000-8000-0000000000bb"

ORCHESTRATOR_PACKAGE = Path(__file__).resolve().parents[1] / "orchestrator"


def test_two_organizations_for_one_user_get_two_homes() -> None:
    assert user_volume_name(USER, ORG_A) != user_volume_name(USER, ORG_B)
    assert ORG_A in user_volume_name(USER, ORG_A)
    assert USER in user_volume_name(USER, ORG_A)


def test_the_same_user_and_organization_always_get_the_same_home() -> None:
    """Idempotent: a second sandbox in the same tenant reopens the same files."""
    assert user_volume_name(USER, ORG_A) == user_volume_name(USER.upper(), ORG_A.upper())


@pytest.mark.parametrize(
    ("user_id", "organization_id"),
    [
        (USER, ""),
        (USER, None),
        (USER, "not-a-uuid"),
        (USER, "../escape"),
        ("", ORG_A),
    ],
    ids=["blank-org", "none-org", "bad-org", "traversal-org", "blank-user"],
)
def test_a_home_is_never_named_without_both_halves(user_id, organization_id) -> None:
    with pytest.raises(ValueError):
        user_volume_name(user_id, organization_id)


def test_the_hosted_resolver_refuses_to_pick_a_tenant() -> None:
    with pytest.raises(ValueError, match="organization"):
        resolve_user_storage(USER, "hosted")

    assert resolve_user_storage(USER, "hosted", ORG_A).volume_name == user_volume_name(
        USER, ORG_A
    )


def test_pre_organization_volumes_are_recognised_and_never_produced() -> None:
    """The old per-user volumes stay on disk, untouched — they are only ever
    NAMED (docs/OPERATIONS.md § Pre-organization per-user volumes)."""
    assert is_legacy_user_volume(f"matrx-user-{USER}")
    assert not is_legacy_user_volume(user_volume_name(USER, ORG_A))
    assert not is_legacy_user_volume("matrx-ec2-home-sbx-abcabcabcabc")


def test_the_lock_identity_of_a_hosted_home_is_the_tenant_home() -> None:
    from orchestrator.home_identity import home_key

    sandbox = SimpleNamespace(
        persistence_volume=None,
        tier="hosted",
        sandbox_id="sbx-abcabcabcabc",
        user_id=USER,
        organization_id=ORG_A,
    )

    assert home_key(sandbox) == user_volume_name(USER, ORG_A)


def test_no_reader_anywhere_derives_a_home_from_the_user_alone() -> None:
    """Fix the class, not the instance.

    Every call site is checked in the AST, so a new one added next month cannot
    quietly reintroduce the cross-tenant mount. ``user_volume_name`` takes two
    positional arguments and nothing may call it with one.
    """
    offenders: list[str] = []
    for path in ORCHESTRATOR_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "user_volume_name":
                continue
            supplied = len(node.args) + len(node.keywords)
            if supplied < 2:
                offenders.append(f"{path}:{node.lineno}")

    assert not offenders, (
        "these call sites name a hosted home from the user alone, which mounts "
        "one tenant's files under another tenant's home: " + ", ".join(offenders)
    )


def test_every_volume_helper_takes_the_organization() -> None:
    """The three functions an operator or a route reaches for."""
    import inspect

    from orchestrator import sandbox_manager
    from orchestrator.storage_layout import ensure_user_volume

    for fn in (
        sandbox_manager.delete_user_volume,
        sandbox_manager.get_user_volume_size,
        ensure_user_volume,
    ):
        assert "organization_id" in inspect.signature(fn).parameters, fn.__name__
