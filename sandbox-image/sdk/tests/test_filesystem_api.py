"""Focused contract tests for bounded daemon filesystem inspection."""

from __future__ import annotations

import base64
from pathlib import Path

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
