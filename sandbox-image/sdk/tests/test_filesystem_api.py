"""Focused contract tests for bounded daemon filesystem inspection."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from matrx_agent.api import main as api_main
from matrx_agent.api.main import DEFAULT_FS_READ_LIMIT, app


client = TestClient(app)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_list_honors_recursion_depth_pattern_and_pagination(tmp_path: Path):
    _write(tmp_path / "a.py", "a")
    _write(tmp_path / "z.txt", "z")
    _write(tmp_path / "nested" / "b.py", "b")
    _write(tmp_path / "nested" / "deep" / "c.py", "c")

    first = client.get(
        "/fs/list",
        params={
            "path": str(tmp_path),
            "recursive": "true",
            "depth": 2,
            "pattern": "*.py",
            "limit": 1,
        },
    )

    assert first.status_code == 200
    assert [entry["name"] for entry in first.json()["entries"]] == ["a.py"]
    assert first.json()["truncated"] is True
    assert first.json()["nextPageToken"]

    second = client.get(
        "/fs/list",
        params={
            "path": str(tmp_path),
            "recursive": "true",
            "depth": 2,
            "pattern": "*.py",
            "limit": 1,
            "pageToken": first.json()["nextPageToken"],
        },
    )

    assert second.status_code == 200
    assert [entry["name"] for entry in second.json()["entries"]] == ["b.py"]
    assert second.json()["truncated"] is False
    assert second.json()["nextPageToken"] is None


def test_list_depth_one_remains_backward_compatible(tmp_path: Path):
    _write(tmp_path / "top.txt", "top")
    _write(tmp_path / "nested" / "hidden.txt", "nested")

    response = client.get("/fs/list", params={"path": str(tmp_path)})

    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()["entries"]] == [
        "nested",
        "top.txt",
    ]
    assert response.json()["truncated"] is False


def test_list_depth_three_reaches_deeper_entries(tmp_path: Path):
    _write(tmp_path / "nested" / "deep" / "target.py", "target")

    response = client.get(
        "/fs/list",
        params={
            "path": str(tmp_path),
            "recursive": "true",
            "depth": 3,
            "pattern": "nested/**/*.py",
        },
    )

    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()["entries"]] == ["target.py"]


def test_list_rejects_page_token_reused_for_another_query(tmp_path: Path):
    _write(tmp_path / "a.txt", "a")
    _write(tmp_path / "b.txt", "b")

    first = client.get("/fs/list", params={"path": str(tmp_path), "limit": 1})
    token = first.json()["nextPageToken"]
    response = client.get(
        "/fs/list",
        params={
            "path": str(tmp_path),
            "limit": 1,
            "pattern": "*.txt",
            "pageToken": token,
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Invalid or expired pageToken for this directory listing"
    }


def test_list_enforces_depth_and_result_bounds(tmp_path: Path):
    _write(tmp_path / "a.txt", "a")

    too_deep = client.get(
        "/fs/list",
        params={"path": str(tmp_path), "recursive": "true", "depth": 33},
    )
    too_many = client.get(
        "/fs/list",
        params={"path": str(tmp_path), "limit": 5_001},
    )

    assert too_deep.status_code == 422
    assert too_many.status_code == 422


def test_list_reports_symlinks_without_traversing_them(tmp_path: Path):
    target = tmp_path / "target"
    _write(target / "inside.txt", "inside")
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return  # Some Windows test environments do not grant symlink privileges.

    response = client.get(
        "/fs/list",
        params={"path": str(tmp_path), "recursive": "true", "depth": 3},
    )

    assert response.status_code == 200
    entries = response.json()["entries"]
    linked = next(entry for entry in entries if entry["name"] == "linked")
    assert linked["kind"] == "symlink"
    assert not any(link in Path(entry["path"]).parents for entry in entries)


def test_stat_and_delete_address_dangling_symlinks(tmp_path: Path):
    missing_target = tmp_path / "missing-target"
    dangling_link = tmp_path / "dangling-link"
    try:
        dangling_link.symlink_to(missing_target)
    except OSError:
        return  # Some Windows test environments do not grant symlink privileges.

    stat_response = client.get("/fs/stat", params={"path": str(dangling_link)})

    assert stat_response.status_code == 200
    assert stat_response.json()["kind"] == "symlink"
    assert stat_response.json()["target"] == str(missing_target)

    delete_response = client.delete(
        "/fs/delete", params={"path": str(dangling_link)}
    )

    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True}
    assert not dangling_link.is_symlink()


def test_list_large_directory_is_deterministic_and_work_bounded(
    tmp_path: Path,
    monkeypatch,
):
    # A small budget makes this a fast regression for the same behavior a
    # million-entry directory exercises in production. The first request must
    # yield instead of materializing/sorting the entire directory in memory.
    monkeypatch.setattr(api_main, "MAX_FS_LIST_SCAN_PER_PAGE", 20)
    expected = [f"item-{index:02d}.txt" for index in range(25)]
    for name in reversed(expected):
        _write(tmp_path / name, name)

    params = {"path": str(tmp_path), "limit": 3}
    first = client.get("/fs/list", params=params)
    assert first.status_code == 200
    assert first.json()["entries"] == []
    assert first.json()["scanned"] == 20
    assert first.json()["nextPageToken"]

    names: list[str] = []
    token = first.json()["nextPageToken"]
    seen_tokens = {token}
    while token:
        page = client.get("/fs/list", params={**params, "pageToken": token})
        assert page.status_code == 200
        body = page.json()
        assert body["scanned"] <= 20
        names.extend(entry["name"] for entry in body["entries"])
        token = body["nextPageToken"]
        if token:
            assert token not in seen_tokens
            seen_tokens.add(token)

    assert names == expected


def test_list_page_tokens_are_single_use(tmp_path: Path):
    _write(tmp_path / "a.txt", "a")
    _write(tmp_path / "b.txt", "b")
    first = client.get("/fs/list", params={"path": str(tmp_path), "limit": 1})
    token = first.json()["nextPageToken"]

    continued = client.get(
        "/fs/list",
        params={"path": str(tmp_path), "limit": 1, "pageToken": token},
    )
    replayed = client.get(
        "/fs/list",
        params={"path": str(tmp_path), "limit": 1, "pageToken": token},
    )

    assert continued.status_code == 200
    assert replayed.status_code == 400


def test_list_snapshot_disk_usage_is_capped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_main, "MAX_FS_LIST_SNAPSHOT_ENTRIES", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(tmp_path / name, name)

    response = client.get("/fs/list", params={"path": str(tmp_path)})

    assert response.status_code == 413
    assert "snapshot limit of 2 entries" in response.json()["detail"]


@pytest.mark.asyncio
async def test_list_snapshot_work_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch,
):
    _write(tmp_path / "entry.txt", "content")
    original_page = api_main._DirectoryListSession.page

    def slow_page(session, limit):
        # Model a slow filesystem/SQLite page without relying on host disk
        # timing. A loop-bound implementation delays the independent pulse.
        time.sleep(0.15)
        return original_page(session, limit)

    monkeypatch.setattr(api_main._DirectoryListSession, "page", slow_page)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pulse_at: list[float] = []

        async def pulse() -> None:
            await asyncio.sleep(0.02)
            pulse_at.append(loop.time())

        response, _ = await asyncio.gather(
            async_client.get("/fs/list", params={"path": str(tmp_path)}),
            pulse(),
        )

    assert response.status_code == 200
    assert pulse_at[0] - started < 0.10


@pytest.mark.asyncio
async def test_cancelled_list_waits_for_worker_before_closing_session(
    tmp_path: Path,
    monkeypatch,
):
    _write(tmp_path / "entry.txt", "content")
    original_page = api_main._DirectoryListSession.page
    original_close = api_main._DirectoryListSession.close
    loop = asyncio.get_running_loop()
    page_started = asyncio.Event()
    page_finished = False
    closed_after_page: list[bool] = []

    def slow_page(session, limit):
        nonlocal page_finished
        loop.call_soon_threadsafe(page_started.set)
        time.sleep(0.10)
        result = original_page(session, limit)
        page_finished = True
        return result

    def tracked_close(session):
        closed_after_page.append(page_finished)
        return original_close(session)

    monkeypatch.setattr(api_main._DirectoryListSession, "page", slow_page)
    monkeypatch.setattr(api_main._DirectoryListSession, "close", tracked_close)
    request = asyncio.create_task(
        api_main.fs_list(
            str(tmp_path),
            recursive=False,
            depth=1,
            pattern=None,
            limit=1_000,
            page_token=None,
        )
    )
    await page_started.wait()
    request.cancel()
    await asyncio.sleep(0.01)
    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request

    assert closed_after_page == [True]


@pytest.mark.asyncio
async def test_list_session_cap_is_admitted_before_constructor_offload(
    tmp_path: Path,
    monkeypatch,
):
    gate = threading.Event()
    counter_lock = threading.Lock()
    started = 0
    active = 0
    peak_active = 0

    class ControlledSession:
        def __init__(self, *_args, scope, lease, **_kwargs):
            nonlocal started, active, peak_active
            self.scope = scope
            self._lease = lease
            self._closed = False
            with counter_lock:
                started += 1
                active += 1
                peak_active = max(peak_active, active)
            gate.wait(timeout=2.0)

        def page(self, _limit):
            return [], False, 0

        def close(self):
            nonlocal active
            if self._closed:
                return
            self._closed = True
            with counter_lock:
                active -= 1
            self._lease.release()

    monkeypatch.setattr(api_main, "_DirectoryListSession", ControlledSession)
    requests = [
        asyncio.create_task(
            api_main.fs_list(
                str(tmp_path),
                recursive=False,
                depth=1,
                pattern=None,
                limit=1,
                page_token=None,
            )
        )
        for _ in range(api_main.MAX_FS_LIST_SESSIONS + 1)
    ]
    deadline = asyncio.get_running_loop().time() + 1.0
    while started < api_main.MAX_FS_LIST_SESSIONS:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    assert started == api_main.MAX_FS_LIST_SESSIONS
    assert peak_active == api_main.MAX_FS_LIST_SESSIONS

    gate.set()
    await asyncio.gather(*requests)

    assert started == api_main.MAX_FS_LIST_SESSIONS + 1
    assert peak_active == api_main.MAX_FS_LIST_SESSIONS
    assert active == 0


@pytest.mark.asyncio
async def test_cancelled_continuation_closes_popped_and_expired_sessions(
    tmp_path: Path,
    monkeypatch,
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        _write(root / "a.txt", "a")
        _write(root / "b.txt", "b")

    first = await api_main.fs_list(
        str(first_root), False, 1, None, 1, None
    )
    second = await api_main.fs_list(
        str(second_root), False, 1, None, 1, None
    )
    first_token = first["nextPageToken"]
    second_token = second["nextPageToken"]
    with api_main._fs_list_sessions_lock:
        expired_session = api_main._fs_list_sessions[first_token]
        popped_session = api_main._fs_list_sessions[second_token]
        expired_session.expires_at = 0
    expired_snapshot = Path(expired_session.walker._tempdir.name)
    popped_snapshot = Path(popped_session.walker._tempdir.name)
    original_close = api_main._DirectoryListSession.close
    loop = asyncio.get_running_loop()
    expired_close_started = asyncio.Event()

    def slow_expired_close(session):
        if session is expired_session:
            loop.call_soon_threadsafe(expired_close_started.set)
            time.sleep(0.10)
        return original_close(session)

    monkeypatch.setattr(api_main._DirectoryListSession, "close", slow_expired_close)
    continuation = asyncio.create_task(
        api_main.fs_list(
            str(second_root), False, 1, None, 1, second_token
        )
    )
    await expired_close_started.wait()
    continuation.cancel()
    await asyncio.sleep(0.01)
    continuation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await continuation

    with api_main._fs_list_sessions_lock:
        assert first_token not in api_main._fs_list_sessions
        assert second_token not in api_main._fs_list_sessions
    assert not expired_snapshot.exists()
    assert not popped_snapshot.exists()


def test_read_honors_text_offset_and_limit_server_side(tmp_path: Path):
    path = tmp_path / "letters.txt"
    _write(path, "abcdef")

    response = client.get(
        "/fs/read",
        params={"path": str(path), "offset": 2, "limit": 3},
    )

    assert response.status_code == 200
    assert response.text == "cde"
    assert response.headers["x-matrx-file-size"] == "6"
    assert response.headers["x-matrx-next-offset"] == "5"
    assert response.headers["x-matrx-truncated"] == "true"


def test_read_text_continuation_offset_is_safe_for_multibyte_utf8(tmp_path: Path):
    path = tmp_path / "unicode.txt"
    _write(path, "éab")

    first = client.get(
        "/fs/read",
        params={"path": str(path), "limit": 1},
    )
    second = client.get(
        "/fs/read",
        params={
            "path": str(path),
            "offset": first.headers["x-matrx-next-offset"],
            "limit": 2,
        },
    )

    assert first.status_code == 200
    assert first.text == "é"
    assert first.headers["x-matrx-next-offset"] == "2"
    assert second.status_code == 200
    assert second.text == "ab"


def test_read_base64_offset_and_limit_are_byte_exact(tmp_path: Path):
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\x00\x01\x02\xff")

    response = client.get(
        "/fs/read",
        params={"path": str(path), "encoding": "base64", "offset": 1, "limit": 2},
    )

    assert response.status_code == 200
    assert base64.b64decode(response.text) == b"\x01\x02"
    assert response.headers["x-matrx-read-length"] == "2"
    assert response.headers["x-matrx-next-offset"] == "3"


def test_read_supports_documented_inclusive_range(tmp_path: Path):
    path = tmp_path / "letters.txt"
    _write(path, "abcdef")

    response = client.get(
        "/fs/read",
        params={"path": str(path), "range": "1-3"},
    )

    assert response.status_code == 200
    assert response.text == "bcd"
    assert response.headers["x-matrx-next-offset"] == "4"


def test_read_without_limit_is_still_bounded(tmp_path: Path):
    path = tmp_path / "large.txt"
    _write(path, "x" * (DEFAULT_FS_READ_LIMIT + 17))

    response = client.get("/fs/read", params={"path": str(path)})

    assert response.status_code == 200
    assert len(response.text) == DEFAULT_FS_READ_LIMIT
    assert response.headers["x-matrx-truncated"] == "true"
    assert response.headers["x-matrx-next-offset"] == str(DEFAULT_FS_READ_LIMIT)


def test_read_rejects_unbounded_limits_and_conflicting_ranges(tmp_path: Path):
    path = tmp_path / "letters.txt"
    _write(path, "abcdef")

    oversized = client.get(
        "/fs/read",
        params={"path": str(path), "limit": 4_194_305},
    )
    conflicting = client.get(
        "/fs/read",
        params={"path": str(path), "range": "0-1", "offset": 0},
    )

    assert oversized.status_code == 422
    assert conflicting.status_code == 400
