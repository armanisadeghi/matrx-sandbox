"""Contract tests for canonical cross-project user memory persistence."""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from uuid import UUID

import pytest

from orchestrator import memory_sync
from orchestrator.store import PostgresSandboxStore


USER_ID = "11111111-1111-4111-8111-111111111111"
ORG_ID = "22222222-2222-4222-8222-222222222222"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def _no_retry(operation):
    return await operation()


@pytest.mark.asyncio
async def test_postgres_memory_crud_uses_canonical_owner_org_and_retention_contract(monkeypatch):
    """Wrong schema, owner column, missing org, or hard delete must make this fail."""
    calls: list[tuple[str, tuple]] = []

    class Conn:
        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return [{"path": "notes/one.md", "content": "one", "updated_at": "now"}]

        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            if "ensure_personal_organization" in sql:
                return UUID(ORG_ID)
            return True

        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "UPDATE 1"

    store = PostgresSandboxStore("postgresql://unused")
    async def get_pool():
        return _Pool(Conn())

    monkeypatch.setattr(store, "_get_pool", get_pool)
    monkeypatch.setattr(store, "_execute_with_retry", _no_retry)

    assert await store.memory_list(USER_ID) == [
        {"path": "notes/one.md", "content": "one", "updated_at": "now"},
    ]
    await store.memory_put(USER_ID, "notes/one.md", "updated")
    assert await store.memory_delete(USER_ID, "notes/one.md") is True

    list_sql, list_args = calls[0]
    org_sql, org_args = calls[1]
    put_sql, put_args = calls[2]
    delete_sql, delete_args = calls[3]
    owner = UUID(USER_ID)

    assert "FROM users.user_memory" in list_sql
    assert "created_by = $1" in list_sql and "deleted_at IS NULL" in list_sql
    assert list_args == (owner,)
    assert "public.ensure_personal_organization($1)" in org_sql
    assert org_args == (owner,)
    assert "INSERT INTO users.user_memory" in put_sql
    assert "(created_by, updated_by, organization_id, path, content)" in put_sql
    assert "ON CONFLICT (created_by, path)" in put_sql
    assert "created_by = EXCLUDED" not in put_sql
    assert "organization_id = EXCLUDED" not in put_sql
    assert "deleted_at = NULL" in put_sql
    assert put_args == (owner, UUID(ORG_ID), "notes/one.md", "updated")
    assert "UPDATE users.user_memory" in delete_sql
    assert "deleted_at = NOW()" in delete_sql
    assert "DELETE FROM" not in delete_sql
    assert "created_by = $1" in delete_sql and "deleted_at IS NULL" in delete_sql
    assert delete_args == (owner, "notes/one.md")


@pytest.mark.asyncio
async def test_memory_hydrate_never_recursively_chowns_unrelated_matrx_paths():
    """A root-owned .matrx sentinel must remain outside memory hydration ownership changes."""
    archive: list[bytes] = []
    commands: list[list[str]] = []

    class Store:
        async def memory_list(self, user_id):
            assert user_id == USER_ID
            return [{"path": "projects/acme.md", "content": "remember this"}]

    class Container:
        def put_archive(self, destination, bits):
            assert destination == memory_sync.AGENT_HOME
            archive.append(bits)
            return True

        def exec_run(self, command):
            commands.append(command)
            return SimpleNamespace(exit_code=0)

    assert await memory_sync.hydrate_memory_into_container(Container(), USER_ID, Store()) == 1
    with tarfile.open(fileobj=io.BytesIO(archive[0]), mode="r") as tar:
        members = {member.name: member for member in tar.getmembers()}

    assert set(members) == {
        ".matrx/memory/projects",
        ".matrx/memory/projects/acme.md",
    }
    assert all(member.uid == member.gid == 1000 for member in members.values())
    assert commands == [
        ["install", "-d", "-o", "1000", "-g", "1000", "-m", "755", memory_sync.MEMORY_ABS],
    ]


@pytest.mark.asyncio
async def test_memory_hydrate_never_writes_archive_after_memory_directory_prepare_fails():
    """A failed memory-root ownership prepare must leave archive extraction untouched."""
    class Store:
        async def memory_list(self, _user_id):
            return [{"path": "notes.md", "content": "keep"}]

    class Container:
        def exec_run(self, _command):
            return SimpleNamespace(exit_code=1)

        def put_archive(self, *_args):
            raise AssertionError("must not extract after ownership preparation failed")

    assert await memory_sync.hydrate_memory_into_container(Container(), USER_ID, Store()) == 0
