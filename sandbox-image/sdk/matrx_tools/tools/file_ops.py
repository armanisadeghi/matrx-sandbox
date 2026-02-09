"""File operation tools — Read, Write, Edit, MultiEdit, Glob, Grep, NotebookEdit."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from matrx_tools.session import ToolSession
from matrx_tools.types import ImageData, ToolResult, ToolResultType

logger = logging.getLogger(__name__)

MAX_LINES = 2000
MAX_LINE_LENGTH = 2000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}
PDF_EXTENSIONS = {".pdf"}
NOTEBOOK_EXTENSIONS = {".ipynb"}


# ── Read ───────────────────────────────────────────────────────────────────────


async def tool_read(
    session: ToolSession,
    file_path: str,
    offset: int | None = None,
    limit: int | None = None,
    pages: str | None = None,
) -> ToolResult:
    file_path = session.resolve_path(file_path)

    if not os.path.exists(file_path):
        return ToolResult(type=ToolResultType.ERROR, output=f"File does not exist: {file_path}")

    if os.path.isdir(file_path):
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Cannot read a directory. Use Bash with 'ls' to list directory contents: {file_path}",
        )

    ext = Path(file_path).suffix.lower()
    session.mark_file_read(file_path)

    if ext in IMAGE_EXTENSIONS:
        return await _read_image(file_path)
    if ext in PDF_EXTENSIONS:
        return await _read_pdf(file_path, pages)
    if ext in NOTEBOOK_EXTENSIONS:
        return await _read_notebook(file_path)

    return await _read_text(file_path, offset, limit)


async def _read_text(file_path: str, offset: int | None, limit: int | None) -> ToolResult:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return ToolResult(type=ToolResultType.ERROR, output=f"Permission denied: {file_path}")

    if not lines:
        return ToolResult(output=f"[File is empty: {file_path}]")

    start = (offset or 1) - 1
    start = max(0, start)
    end = start + (limit or MAX_LINES)
    selected = lines[start:end]

    formatted: list[str] = []
    width = len(str(start + len(selected)))
    for i, line in enumerate(selected, start=start + 1):
        line = line.rstrip("\n").rstrip("\r")
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + "... [truncated]"
        formatted.append(f"{i:>{width}}\t{line}")

    return ToolResult(output="\n".join(formatted))


async def _read_image(file_path: str) -> ToolResult:
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except PermissionError:
        return ToolResult(type=ToolResultType.ERROR, output=f"Permission denied: {file_path}")

    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    size_kb = len(raw) / 1024

    return ToolResult(
        output=f"Image file: {os.path.basename(file_path)} ({size_kb:.1f} KB, {mime_type})",
        image=ImageData(media_type=mime_type, base64_data=encoded),
    )


async def _read_pdf(file_path: str, pages: str | None) -> ToolResult:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=(
                "PDF reading requires poppler-utils (pdftotext). "
                "Install with: apt-get install -y poppler-utils"
            ),
        )

    cmd_parts = ["pdftotext"]
    if pages:
        parts = pages.split("-")
        if len(parts) == 2:
            cmd_parts.extend(["-f", parts[0].strip(), "-l", parts[1].strip()])
        elif len(parts) == 1:
            cmd_parts.extend(["-f", parts[0].strip(), "-l", parts[0].strip()])
    cmd_parts.extend([shlex.quote(file_path), "-"])

    proc = await asyncio.create_subprocess_shell(
        " ".join(cmd_parts),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Failed to read PDF: {stderr.decode('utf-8', errors='replace')}",
        )

    text = stdout.decode("utf-8", errors="replace")
    if not text.strip():
        return ToolResult(output=f"[PDF file has no extractable text: {file_path}]")

    return ToolResult(output=text)


async def _read_notebook(file_path: str) -> ToolResult:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except (json.JSONDecodeError, PermissionError) as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to read notebook: {e}")

    cells = nb.get("cells", [])
    if not cells:
        return ToolResult(output=f"[Empty notebook: {file_path}]")

    parts: list[str] = []
    for i, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "unknown")
        cell_id = cell.get("id", f"cell_{i}")
        source = "".join(cell.get("source", []))
        parts.append(f"--- Cell {i} [{cell_type}] (id: {cell_id}) ---")
        parts.append(source)

        outputs = cell.get("outputs", [])
        for out in outputs:
            if out.get("output_type") == "stream":
                parts.append(f"[Output: stream]")
                parts.append("".join(out.get("text", [])))
            elif out.get("output_type") in ("execute_result", "display_data"):
                data = out.get("data", {})
                if "text/plain" in data:
                    parts.append(f"[Output: {out['output_type']}]")
                    parts.append("".join(data["text/plain"]))
            elif out.get("output_type") == "error":
                parts.append(f"[Error: {out.get('ename', 'Unknown')}]")
                parts.append("\n".join(out.get("traceback", [])))
        parts.append("")

    return ToolResult(output="\n".join(parts))


# ── Write ──────────────────────────────────────────────────────────────────────


async def tool_write(
    session: ToolSession,
    file_path: str,
    content: str,
) -> ToolResult:
    file_path = session.resolve_path(file_path)

    if os.path.exists(file_path) and not session.has_read_file(file_path):
        return ToolResult(
            type=ToolResultType.ERROR,
            output=(
                f"File already exists but has not been read in this session: {file_path}\n"
                f"You must Read the file first before overwriting it."
            ),
        )

    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        dir_name = os.path.dirname(file_path)
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, file_path)
    except PermissionError:
        return ToolResult(type=ToolResultType.ERROR, output=f"Permission denied: {file_path}")
    except OSError as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to write file: {e}")

    byte_count = len(content.encode("utf-8"))
    return ToolResult(output=f"Successfully wrote {byte_count} bytes to {file_path}")


# ── Edit ───────────────────────────────────────────────────────────────────────


async def tool_edit(
    session: ToolSession,
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    file_path = session.resolve_path(file_path)

    if not os.path.exists(file_path):
        return ToolResult(type=ToolResultType.ERROR, output=f"File does not exist: {file_path}")

    if not session.has_read_file(file_path):
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"You must Read the file before editing it: {file_path}",
        )

    if old_string == new_string:
        return ToolResult(
            type=ToolResultType.ERROR,
            output="old_string and new_string must be different.",
        )

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except PermissionError:
        return ToolResult(type=ToolResultType.ERROR, output=f"Permission denied: {file_path}")

    count = content.count(old_string)

    if count == 0:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"old_string not found in {file_path}",
        )

    if count > 1 and not replace_all:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=(
                f"old_string found {count} times in {file_path}. "
                f"Provide a larger, more unique string or use replace_all=true."
            ),
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    try:
        dir_name = os.path.dirname(file_path)
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            tmp.write(new_content)
            tmp_path = tmp.name
        os.replace(tmp_path, file_path)
    except OSError as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to write edited file: {e}")

    replacements = count if replace_all else 1
    return ToolResult(output=f"Successfully edited {file_path} ({replacements} replacement(s))")


# ── MultiEdit ──────────────────────────────────────────────────────────────────


async def tool_multi_edit(
    session: ToolSession,
    file_path: str,
    edits: list[dict],
) -> ToolResult:
    file_path = session.resolve_path(file_path)

    if not os.path.exists(file_path):
        return ToolResult(type=ToolResultType.ERROR, output=f"File does not exist: {file_path}")

    if not session.has_read_file(file_path):
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"You must Read the file before editing it: {file_path}",
        )

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except PermissionError:
        return ToolResult(type=ToolResultType.ERROR, output=f"Permission denied: {file_path}")

    total_replacements = 0
    for i, edit in enumerate(edits):
        old_str = edit.get("old_string", "")
        new_str = edit.get("new_string", "")
        do_all = edit.get("replace_all", False)

        if not old_str:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Edit {i}: old_string must not be empty.",
            )
        if old_str == new_str:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Edit {i}: old_string and new_string must be different.",
            )

        count = content.count(old_str)
        if count == 0:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Edit {i}: old_string not found in file.",
            )
        if count > 1 and not do_all:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Edit {i}: old_string found {count} times. Use replace_all or be more specific.",
            )

        if do_all:
            content = content.replace(old_str, new_str)
            total_replacements += count
        else:
            content = content.replace(old_str, new_str, 1)
            total_replacements += 1

    try:
        dir_name = os.path.dirname(file_path)
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, file_path)
    except OSError as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to write edited file: {e}")

    return ToolResult(
        output=f"Successfully applied {len(edits)} edit(s) to {file_path} ({total_replacements} total replacement(s))"
    )


# ── Glob ───────────────────────────────────────────────────────────────────────


async def tool_glob(
    session: ToolSession,
    pattern: str,
    path: str | None = None,
) -> ToolResult:
    search_dir = session.resolve_path(path) if path else session.cwd

    if not os.path.isdir(search_dir):
        return ToolResult(type=ToolResultType.ERROR, output=f"Directory does not exist: {search_dir}")

    fd_bin = shutil.which("fd") or shutil.which("fdfind")
    if fd_bin:
        return await _glob_with_fd(pattern, search_dir, fd_bin)
    return _glob_with_python(pattern, search_dir)


async def _glob_with_fd(pattern: str, search_dir: str, fd_bin: str) -> ToolResult:
    cmd = f"{fd_bin} --glob {shlex.quote(pattern)} {shlex.quote(search_dir)} --type f --absolute-path"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0 and not stdout:
        err = stderr.decode("utf-8", errors="replace").strip()
        if err:
            return ToolResult(type=ToolResultType.ERROR, output=f"Glob search failed: {err}")
        return ToolResult(output="No files matched the pattern.")

    paths = [p for p in stdout.decode("utf-8", errors="replace").strip().split("\n") if p]
    return _sort_and_format_paths(paths)


def _glob_with_python(pattern: str, search_dir: str) -> ToolResult:
    base = Path(search_dir)
    try:
        paths = [str(p) for p in base.glob(pattern) if p.is_file()]
    except ValueError as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Invalid glob pattern: {e}")

    return _sort_and_format_paths(paths)


def _sort_and_format_paths(paths: list[str]) -> ToolResult:
    if not paths:
        return ToolResult(output="No files matched the pattern.")

    def safe_mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    paths.sort(key=safe_mtime, reverse=True)
    return ToolResult(output="\n".join(paths))


# ── Grep ───────────────────────────────────────────────────────────────────────


async def tool_grep(
    session: ToolSession,
    pattern: str,
    path: str | None = None,
    output_mode: str = "files_with_matches",
    glob: str | None = None,
    type: str | None = None,
    multiline: bool = False,
    head_limit: int = 0,
    offset: int = 0,
    context: int | None = None,
    **kwargs,
) -> ToolResult:
    search_path = session.resolve_path(path) if path else session.cwd

    rg_bin = shutil.which("rg")
    if not rg_bin:
        return ToolResult(
            type=ToolResultType.ERROR,
            output="ripgrep (rg) is not installed.",
        )

    cmd_parts = [rg_bin]

    if output_mode == "files_with_matches":
        cmd_parts.append("-l")
    elif output_mode == "count":
        cmd_parts.append("-c")

    if kwargs.get("-i"):
        cmd_parts.append("-i")

    if output_mode == "content":
        if kwargs.get("-n", True):
            cmd_parts.append("-n")

        after = kwargs.get("-A")
        before = kwargs.get("-B")
        ctx = kwargs.get("-C") or context

        if ctx is not None:
            cmd_parts.extend(["-C", str(int(ctx))])
        else:
            if after is not None:
                cmd_parts.extend(["-A", str(int(after))])
            if before is not None:
                cmd_parts.extend(["-B", str(int(before))])

    if multiline:
        cmd_parts.extend(["-U", "--multiline-dotall"])

    if glob:
        cmd_parts.extend(["--glob", glob])

    if type:
        cmd_parts.extend(["--type", type])

    cmd_parts.extend(["--", pattern, search_path])

    proc = await asyncio.create_subprocess_exec(
        *cmd_parts,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 2:
        err = stderr.decode("utf-8", errors="replace").strip()
        return ToolResult(type=ToolResultType.ERROR, output=f"Grep error: {err}")

    if proc.returncode == 1 and not stdout:
        return ToolResult(output="No matches found.")

    output = stdout.decode("utf-8", errors="replace")
    lines = output.split("\n")

    if lines and lines[-1] == "":
        lines = lines[:-1]

    if offset > 0:
        lines = lines[offset:]

    if head_limit > 0:
        lines = lines[:head_limit]

    return ToolResult(output="\n".join(lines))


# ── NotebookEdit ───────────────────────────────────────────────────────────────


async def tool_notebook_edit(
    session: ToolSession,
    notebook_path: str,
    new_source: str,
    cell_id: str | None = None,
    cell_type: str | None = None,
    edit_mode: str = "replace",
) -> ToolResult:
    notebook_path = session.resolve_path(notebook_path)

    if not os.path.exists(notebook_path):
        return ToolResult(type=ToolResultType.ERROR, output=f"Notebook not found: {notebook_path}")

    try:
        with open(notebook_path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except (json.JSONDecodeError, PermissionError) as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to read notebook: {e}")

    cells = nb.get("cells", [])

    if edit_mode == "replace":
        return _notebook_replace(nb, cells, cell_id, new_source, notebook_path)
    elif edit_mode == "insert":
        if not cell_type:
            return ToolResult(
                type=ToolResultType.ERROR,
                output="cell_type is required for edit_mode=insert",
            )
        return _notebook_insert(nb, cells, cell_id, new_source, cell_type, notebook_path)
    elif edit_mode == "delete":
        return _notebook_delete(nb, cells, cell_id, notebook_path)
    else:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Unknown edit_mode: {edit_mode}. Use 'replace', 'insert', or 'delete'.",
        )


def _find_cell_index(cells: list[dict], cell_id: str | None) -> int | None:
    if cell_id is None:
        return 0 if cells else None
    for i, cell in enumerate(cells):
        if cell.get("id") == cell_id:
            return i
    return None


def _source_to_lines(source: str) -> list[str]:
    lines = source.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            result.append(line + "\n")
        else:
            result.append(line)
    return result


def _notebook_replace(
    nb: dict, cells: list, cell_id: str | None, new_source: str, path: str,
) -> ToolResult:
    idx = _find_cell_index(cells, cell_id)
    if idx is None:
        return ToolResult(type=ToolResultType.ERROR, output=f"Cell not found: {cell_id}")

    cells[idx]["source"] = _source_to_lines(new_source)
    return _save_notebook(nb, path, f"Replaced cell {cell_id or 0}")


def _notebook_insert(
    nb: dict, cells: list, cell_id: str | None, new_source: str, cell_type: str, path: str,
) -> ToolResult:
    import uuid as uuid_mod

    new_cell = {
        "cell_type": cell_type,
        "id": str(uuid_mod.uuid4())[:8],
        "metadata": {},
        "source": _source_to_lines(new_source),
    }
    if cell_type == "code":
        new_cell["execution_count"] = None
        new_cell["outputs"] = []

    if cell_id is None:
        cells.insert(0, new_cell)
        insert_pos = 0
    else:
        idx = _find_cell_index(cells, cell_id)
        if idx is None:
            return ToolResult(type=ToolResultType.ERROR, output=f"Cell not found: {cell_id}")
        cells.insert(idx + 1, new_cell)
        insert_pos = idx + 1

    return _save_notebook(nb, path, f"Inserted {cell_type} cell at position {insert_pos}")


def _notebook_delete(
    nb: dict, cells: list, cell_id: str | None, path: str,
) -> ToolResult:
    idx = _find_cell_index(cells, cell_id)
    if idx is None:
        return ToolResult(type=ToolResultType.ERROR, output=f"Cell not found: {cell_id}")

    removed = cells.pop(idx)
    return _save_notebook(nb, path, f"Deleted cell {cell_id or 0} (was {removed.get('cell_type', 'unknown')})")


def _save_notebook(nb: dict, path: str, message: str) -> ToolResult:
    try:
        dir_name = os.path.dirname(path)
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(nb, tmp, indent=1, ensure_ascii=False)
            tmp.write("\n")
            tmp_path = tmp.name
        os.replace(tmp_path, path)
    except OSError as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Failed to save notebook: {e}")

    return ToolResult(output=f"Successfully edited notebook: {message}")
