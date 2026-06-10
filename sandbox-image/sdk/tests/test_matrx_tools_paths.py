"""Tests for matrx_tools workspace confinement + PDF page-range validation.

These cover the two P0s in the audit: arbitrary-path traversal via
``ToolSession.resolve_path`` and shell injection via the PDF ``pages`` arg.
"""

from __future__ import annotations

import os

import pytest

from matrx_tools.session import PathEscapesWorkspaceError, ToolSession
from matrx_tools.tools.file_ops import _parse_pdf_page_range


@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    monkeypatch.setenv("TOOL_WORKSPACE_BASE", str(root))
    return ToolSession(working_dir=str(root))


def test_relative_path_inside_workspace(session):
    resolved = session.resolve_path("sub/a.txt")
    assert resolved.endswith("/ws/sub/a.txt")


def test_absolute_path_inside_workspace_allowed(session):
    resolved = session.resolve_path(session.workspace_root + "/sub/b.txt")
    assert resolved.endswith("/ws/sub/b.txt")


@pytest.mark.parametrize("bad", ["/etc/shadow", "../../etc/passwd", "../evil"])
def test_paths_escaping_workspace_rejected(session, bad):
    with pytest.raises(PathEscapesWorkspaceError):
        session.resolve_path(bad)


def test_symlink_escape_rejected(session):
    link = os.path.join(session.workspace_root, "etclink")
    os.symlink("/etc", link, target_is_directory=True)
    with pytest.raises(PathEscapesWorkspaceError):
        session.resolve_path("etclink/shadow")


# ── PDF page-range parsing ───────────────────────────────────────────────────

@pytest.mark.parametrize("spec,expected", [("3", (3, 3)), ("2-5", (2, 5)), (" 1 - 4 ", (1, 4))])
def test_valid_page_ranges(spec, expected):
    assert _parse_pdf_page_range(spec) == expected


@pytest.mark.parametrize("bad", [
    "1-5 && cat /etc/passwd", "--help", "1;rm -rf /", "abc", "0", "5-2", "", "1-2-3",
])
def test_injection_and_invalid_page_ranges_rejected(bad):
    with pytest.raises(ValueError):
        _parse_pdf_page_range(bad)
