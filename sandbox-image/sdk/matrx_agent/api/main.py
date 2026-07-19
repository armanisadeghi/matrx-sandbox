import asyncio
import base64
import fnmatch
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from matrx_agent.api import _auth

from matrx_agent.cloud_sync import CloudFilesWatcher
from matrx_agent.persistence import CheckpointDaemon, read_prior_manifest, render_report
from matrx_agent.persistence.checkpoint import DEFAULT_INTERVAL_SECONDS
from matrx_agent.persistence.git_autostash import auto_stash_all_repos
from matrx_agent.persistence.manifest import (
    HOME,
    MANIFEST_PATH,
    MATRX_DIR,
    _find_git_repos,
    collect_manifest,
    write_manifest,
)
from matrx_agent.persistence.session_report import REPORT_PATH

_logger = logging.getLogger("matrx_agent")
_checkpoint = CheckpointDaemon(interval_seconds=int(os.environ.get(
    "MATRX_CHECKPOINT_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS),
)))
_cloud_watcher = CloudFilesWatcher()


# Filesystem inspection endpoints are agent-facing and can be invoked on trees
# or files whose size is not known in advance. Keep every response bounded.
# Callers can paginate list results or continue reads from the returned offset.
DEFAULT_FS_LIST_LIMIT = 1_000
MAX_FS_LIST_LIMIT = 5_000
MAX_FS_LIST_DEPTH = 32
MAX_FS_LIST_SCAN_PER_PAGE = 20_000
MAX_FS_LIST_SNAPSHOT_ENTRIES = 250_000
MAX_FS_LIST_SESSIONS = 16
FS_LIST_SESSION_TTL_SECONDS = 300
DEFAULT_FS_READ_LIMIT = 1_048_576  # characters for utf8; bytes for base64
MAX_FS_READ_LIMIT = 4_194_304
MAX_FS_OFFSET = 9_223_372_036_854_775_807


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Daemon lifespan — render the prior session's report on startup and
    fire one final manifest write on shutdown.

    The CheckpointDaemon owns the periodic 5-minute manifest writes between
    those two events. The ``/internal/shutdown`` route can be hit before the
    container exits to also run the auto-stash pass.
    """
    # Startup: render the welcome / session-report.md from any prior manifest.
    try:
        prior = read_prior_manifest()
        render_report(prior)
        _logger.info("matrx_agent: persistence module ready (prior=%s)", "yes" if prior else "no")
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: failed to render session report: %s", e)

    # Start the periodic checkpoint loop.
    try:
        await _checkpoint.start()
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: failed to start checkpoint daemon: %s", e)

    # Start the cloud-files real-time watcher (no-ops if AI Dream env is unset).
    # Runs as a background task so a slow down-marker wait doesn't block the
    # daemon from accepting requests.
    cloud_watcher_starter = asyncio.create_task(_cloud_watcher.start())
    list_session_reaper = asyncio.create_task(_reap_list_sessions())

    yield

    # Shutdown: best-effort final manifest write. The container may still be
    # killed mid-write — that's fine, the prior checkpoint is the floor.
    try:
        await _checkpoint.stop()
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: checkpoint daemon stop error: %s", e)
    try:
        list_session_reaper.cancel()
        try:
            await list_session_reaper
        except asyncio.CancelledError:
            pass
        with _fs_list_sessions_lock:
            sessions = list(_fs_list_sessions.values())
            _fs_list_sessions.clear()
        await _close_list_sessions(sessions)
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: list-session cleanup error: %s", e)
    try:
        # Cancel the starter in case it's still waiting on the down-marker; then
        # stop the watcher itself (idempotent if it never started).
        cloud_watcher_starter.cancel()
        try:
            await cloud_watcher_starter
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _cloud_watcher.stop()
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: cloud-files watcher stop error: %s", e)
    try:
        manifest = collect_manifest(graceful=True)
        try:
            manifest.cloud_sync = _cloud_watcher.get_stats()
        except Exception as e:  # noqa: BLE001
            _logger.warning("matrx_agent: cloud_sync stats splice failed: %s", e)
        write_manifest(manifest)
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: final manifest write failed: %s", e)


app = FastAPI(title="Matrx Sandbox Agent API", lifespan=lifespan)


# Per-sandbox daemon auth (fail-open when MATRX_AGENT_TOKEN is unset — see
# matrx_agent.api._auth). Exempt:
#   /health      — the container HEALTHCHECK curls it with no token.
#   /internal/*  — lifecycle hooks the in-container entrypoint/shutdown scripts
#                  call over localhost without a token. (WebSocket tool routes
#                  enforce the token inside their own handlers, since HTTP
#                  middleware doesn't see WS connections.)
_AGENT_AUTH_EXEMPT_PREFIXES = ("/health", "/internal/")


@app.middleware("http")
async def _agent_token_guard(request, call_next):
    if _auth.enforcement_enabled():
        path = request.url.path
        if not path.startswith(_AGENT_AUTH_EXEMPT_PREFIXES) and request.method != "OPTIONS":
            if not _auth.token_ok(request.headers.get(_auth.HEADER_NAME)):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid or missing X-Matrx-Agent-Token"},
                )
    return await call_next(request)

# --- Models ---

class WriteRequest(BaseModel):
    path: str
    content: str
    encoding: Literal["utf8", "base64"] = "utf8"
    mode: Optional[int] = None
    create_parents: bool = False

class EditChunk(BaseModel):
    """Anchor-based (search-and-replace) edit — matches matrx-ai's fs_patch/fs_edit
    tool contract (old_text -> new_text), NOT the legacy offset-based
    start/end/replacement shape this daemon used to require. That mismatch was
    causing every sandbox-bound fs_edit/fs_patch call to fail with
    "Field required: edits.0.start", since the client never sends offsets."""
    old_text: str
    new_text: str
    replace_all: bool = False

class PatchRequest(BaseModel):
    path: str
    edits: List[EditChunk]
    create_if_missing: bool = False

class MkdirRequest(BaseModel):
    path: str
    parents: bool = False

class RenameRequest(BaseModel):
    from_path: str
    to_path: str
    overwrite: bool = False

class CopyRequest(BaseModel):
    from_path: str
    to_path: str
    recursive: bool = False

# --- Helpers ---

def _atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Write ``data`` to ``path`` atomically without a fixed temp name.

    The old code wrote to ``path.with_suffix(".tmp")`` — a single shared name —
    so two concurrent writes to the same file raced on the same temp path and
    silently corrupted or lost one writer's data. Use a unique temp file in the
    same directory (so os.replace stays atomic on one filesystem), then rename.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Don't leave the unique temp behind on any failure.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def get_stat_dict(file_path: Path) -> dict:
    """Return one stable filesystem entry without following symlinks."""
    st = file_path.lstat()
    is_symlink = file_path.is_symlink()
    kind = "symlink" if is_symlink else "dir" if file_path.is_dir() else "file"
    return {
        "name": file_path.name,
        "path": str(file_path),
        "kind": kind,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mode": stat.S_IMODE(st.st_mode),
        "target": str(file_path.resolve()) if is_symlink else None,
    }


class _SnapshotFrame:
    def __init__(self, frame_id: int, directory: Path, level: int) -> None:
        self.frame_id = frame_id
        self.directory = directory
        self.level = level
        self.iterator: os.ScandirIterator | None = None
        self.scanned = False
        self.after_name = ""
        self.after_path = ""


class _DirectoryWalker:
    """Bounded-work deterministic traversal backed by a disk snapshot.

    Lexical order requires seeing all siblings before emitting the first one.
    Doing that with ``sorted(directory.iterdir())`` made a one-item request
    allocate memory proportional to directory size. This walker incrementally
    snapshots at most a fixed number of entries per request into a temporary
    SQLite file, then keyset-pages that snapshot in binary lexical order.
    Memory and request work stay bounded even for million-entry directories.
    """

    def __init__(
        self,
        root: Path,
        *,
        recursive: bool,
        depth: int,
        pattern: str | None,
    ) -> None:
        self._recursive = recursive
        self._depth = depth
        self._pattern = pattern
        self._tempdir = tempfile.TemporaryDirectory(prefix="matrx-fs-list-")
        # A session is single-consumer but successive HTTP pages can run on
        # different worker threads. The session registry guarantees exclusive
        # ownership; disabling SQLite's thread affinity lets that ownership
        # move between workers without moving blocking I/O back to the loop.
        self._db = sqlite3.connect(
            str(Path(self._tempdir.name) / "listing.sqlite3"),
            check_same_thread=False,
        )
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute(
            """CREATE TABLE entries (
                   frame_id INTEGER NOT NULL,
                   name TEXT NOT NULL COLLATE BINARY,
                   path TEXT NOT NULL COLLATE BINARY,
                   payload TEXT NOT NULL,
                   descend INTEGER NOT NULL,
                   matches INTEGER NOT NULL,
                   PRIMARY KEY (frame_id, name, path)
               ) WITHOUT ROWID"""
        )
        self._next_frame_id = 1
        self._snapshot_entries = 0
        self._stack = [_SnapshotFrame(0, root, 1)]
        self._closed = False

    @staticmethod
    def _open(frame: _SnapshotFrame) -> None:
        try:
            frame.iterator = os.scandir(frame.directory)
        except (FileNotFoundError, NotADirectoryError):
            frame.scanned = True
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {frame.directory}",
            ) from exc

    def _snapshot_one(self, frame: _SnapshotFrame) -> bool:
        """Snapshot one raw sibling. Return false when the frame is complete."""
        if frame.iterator is None:
            self._open(frame)
        if frame.scanned:
            return False
        assert frame.iterator is not None
        try:
            entry = next(frame.iterator)
        except StopIteration:
            frame.iterator.close()
            frame.iterator = None
            frame.scanned = True
            self._db.commit()
            return False
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {frame.directory}",
            ) from exc

        path = Path(entry.path)
        try:
            payload = get_stat_dict(path)
            descend = (
                self._recursive
                and frame.level < self._depth
                and payload["kind"] == "dir"
            )
            matches = _matches_list_pattern(path, self._stack[0].directory, self._pattern)
        except (FileNotFoundError, NotADirectoryError):
            return True
        # Unmatched leaf files cannot affect recursive traversal order, so a
        # narrow pattern should also narrow snapshot disk usage.
        if not matches and not descend:
            return True
        if self._snapshot_entries >= MAX_FS_LIST_SNAPSHOT_ENTRIES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Directory listing exceeds the deterministic snapshot limit "
                    f"of {MAX_FS_LIST_SNAPSHOT_ENTRIES} entries; narrow the path "
                    "or pattern"
                ),
            )
        self._db.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
            (
                frame.frame_id,
                path.name,
                str(path),
                json.dumps(payload, separators=(",", ":")),
                int(descend),
                int(matches),
            ),
        )
        self._snapshot_entries += 1
        return True

    def next(self, work_remaining: int) -> tuple[dict | None, int, bool]:
        """Find the next matching entry within ``work_remaining`` operations.

        Returns ``(entry, work_used, finished)``. ``entry`` is ``None`` when
        the budget ended while snapshotting or skipping unmatched entries.
        """
        used = 0
        while used < work_remaining and self._stack:
            frame = self._stack[-1]
            if not frame.scanned:
                consumed = self._snapshot_one(frame)
                if consumed:
                    used += 1
                continue

            row = self._db.execute(
                """SELECT name, path, payload, descend, matches
                     FROM entries
                    WHERE frame_id = ?
                      AND (name > ? OR (name = ? AND path > ?))
                 ORDER BY name COLLATE BINARY, path COLLATE BINARY
                    LIMIT 1""",
                (frame.frame_id, frame.after_name, frame.after_name, frame.after_path),
            ).fetchone()
            if row is None:
                deleted = self._db.execute(
                    "SELECT COUNT(*) FROM entries WHERE frame_id = ?",
                    (frame.frame_id,),
                ).fetchone()[0]
                self._db.execute("DELETE FROM entries WHERE frame_id = ?", (frame.frame_id,))
                self._snapshot_entries -= deleted
                self._db.commit()
                self._stack.pop()
                continue

            name, path_value, payload_json, descend, matches = row
            frame.after_name = name
            frame.after_path = path_value
            used += 1
            if descend:
                child = _SnapshotFrame(self._next_frame_id, Path(path_value), frame.level + 1)
                self._next_frame_id += 1
                self._stack.append(child)
            if matches:
                return json.loads(payload_json), used, False

        return None, used, not self._stack

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for frame in self._stack:
            if frame.iterator is not None:
                frame.iterator.close()
        self._stack.clear()
        self._db.close()
        self._tempdir.cleanup()


class _DirectoryListSession:
    def __init__(
        self,
        root: Path,
        *,
        recursive: bool,
        depth: int,
        pattern: str | None,
        scope: str,
    ) -> None:
        self.root = root
        self.pattern = pattern
        self.scope = scope
        self.walker = _DirectoryWalker(
            root,
            recursive=recursive,
            depth=depth,
            pattern=pattern,
        )
        self.pending: dict | None = None
        self.expires_at = time.monotonic() + FS_LIST_SESSION_TTL_SECONDS

    def page(self, limit: int) -> tuple[list[dict], bool, int]:
        """Return at most ``limit`` entries after bounded traversal work."""
        entries: list[dict] = []
        work = 0
        if self.pending is not None:
            entries.append(self.pending)
            self.pending = None

        while work < MAX_FS_LIST_SCAN_PER_PAGE:
            item, used, finished = self.walker.next(MAX_FS_LIST_SCAN_PER_PAGE - work)
            work += used
            if finished:
                return entries, False, work
            if item is None:
                break
            if len(entries) < limit:
                entries.append(item)
            else:
                self.pending = item
                return entries, True, work

        # The scan budget can be exhausted before a sparse pattern matches.
        # Returning an empty page with a token is intentional: it makes work
        # per request finite without falsely claiming the traversal is done.
        return entries, True, work

    def close(self) -> None:
        self.walker.close()


_fs_list_sessions: dict[str, _DirectoryListSession] = {}
_fs_list_sessions_lock = threading.Lock()


def _take_expired_list_sessions(now: float) -> list[_DirectoryListSession]:
    """Remove and return expired sessions while the registry lock is held."""
    expired = [
        token for token, session in _fs_list_sessions.items()
        if session.expires_at <= now
    ]
    return [_fs_list_sessions.pop(token) for token in expired]


def _store_list_session(
    session: _DirectoryListSession,
) -> tuple[str, list[_DirectoryListSession]]:
    """Store one session and return any evictions for off-loop cleanup."""
    now = time.monotonic()
    evicted = _take_expired_list_sessions(now)
    while len(_fs_list_sessions) >= MAX_FS_LIST_SESSIONS:
        oldest_token = min(
            _fs_list_sessions,
            key=lambda token: _fs_list_sessions[token].expires_at,
        )
        evicted.append(_fs_list_sessions.pop(oldest_token))
    session.expires_at = now + FS_LIST_SESSION_TTL_SECONDS
    token = secrets.token_urlsafe(24)
    _fs_list_sessions[token] = session
    return token, evicted


async def _close_list_sessions(sessions: list[_DirectoryListSession]) -> None:
    """Close directory handles and SQLite snapshots outside the event loop."""
    if not sessions:
        return

    def close_all() -> None:
        for session in sessions:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("matrx_agent: list-session close error: %s", exc)

    await asyncio.to_thread(close_all)


async def _reap_list_sessions() -> None:
    """Close expired directory handles and delete their disk snapshots."""
    while True:
        await asyncio.sleep(min(60, FS_LIST_SESSION_TTL_SECONDS))
        with _fs_list_sessions_lock:
            expired = _take_expired_list_sessions(time.monotonic())
        await _close_list_sessions(expired)


def _list_scope(path: Path, *, recursive: bool, depth: int, pattern: str | None) -> str:
    payload = json.dumps(
        {
            "path": os.path.abspath(path),
            "recursive": recursive,
            "depth": depth,
            "pattern": pattern or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _matches_list_pattern(path: Path, root: Path, pattern: str | None) -> bool:
    if not pattern:
        return True
    relative = path.relative_to(root).as_posix()
    # fnmatchcase keeps matching behavior identical on Windows and Unix.
    return fnmatch.fnmatchcase(path.name, pattern) or fnmatch.fnmatchcase(
        relative,
        pattern,
    )


def _parse_byte_range(value: str) -> tuple[int, int]:
    """Parse the documented inclusive ``range=start-end`` compatibility form."""
    try:
        raw_start, raw_end = value.split("-", 1)
        start = int(raw_start)
        end = int(raw_end)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=416, detail="range must be start-end") from exc
    if start < 0 or end < start:
        raise HTTPException(
            status_code=416,
            detail="range must satisfy 0 <= start <= end",
        )
    limit = end - start + 1
    if limit > MAX_FS_READ_LIMIT:
        raise HTTPException(
            status_code=416,
            detail=f"range exceeds the {MAX_FS_READ_LIMIT}-byte maximum",
        )
    return start, limit

# --- Health ---

@app.get("/health")
async def health() -> dict:
    """Liveness probe consumed by the container HEALTHCHECK and operators.
    Returns 200 as long as the matrx_agent daemon is responding. Doesn't
    walk the filesystem or call other services — keep it cheap so the
    healthcheck never adds load."""
    return {
        "status": "ok",
        "service": "matrx_agent",
        "sandbox_id": os.environ.get("SANDBOX_ID", ""),
    }


# --- FS Routes ---

@app.get("/fs/list")
async def fs_list(
    path: str,
    recursive: bool = False,
    depth: int = Query(default=1, ge=1, le=MAX_FS_LIST_DEPTH),
    pattern: str | None = None,
    limit: int = Query(default=DEFAULT_FS_LIST_LIMIT, ge=1, le=MAX_FS_LIST_LIMIT),
    page_token: str | None = Query(default=None, alias="pageToken"),
):
    """Return a bounded, optionally recursive page of directory entries.

    ``depth`` counts the requested directory's children as level one. Symlinked
    directories are returned as entries but never traversed. Results use a
    cursor-stable depth-first order so ``nextPageToken`` continues the exact
    open traversal without materializing or rescanning an unbounded tree.
    """
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    scope = _list_scope(p, recursive=recursive, depth=depth, pattern=pattern)
    expired: list[_DirectoryListSession] = []
    if page_token is None:
        session = await asyncio.to_thread(
            _DirectoryListSession,
            p,
            recursive=recursive,
            depth=depth,
            pattern=pattern,
            scope=scope,
        )
    else:
        with _fs_list_sessions_lock:
            expired = _take_expired_list_sessions(time.monotonic())
            session = _fs_list_sessions.pop(page_token, None)
        await _close_list_sessions(expired)
        if session is None or session.scope != scope:
            if session is not None:
                await asyncio.to_thread(session.close)
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired pageToken for this directory listing",
            )

    try:
        entries, truncated, scanned = await asyncio.to_thread(session.page, limit)
    except BaseException:
        await asyncio.to_thread(session.close)
        raise

    evicted: list[_DirectoryListSession] = []
    if truncated:
        with _fs_list_sessions_lock:
            next_page_token, evicted = _store_list_session(session)
    else:
        next_page_token = None
        await asyncio.to_thread(session.close)
    await _close_list_sessions(evicted)

    return {
        "entries": entries,
        "truncated": truncated,
        "nextPageToken": next_page_token,
        "scanned": scanned,
    }

@app.get("/fs/stat")
async def fs_stat(path: str):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    return get_stat_dict(p)

@app.get("/fs/read")
async def fs_read(
    path: str,
    encoding: Literal["utf8", "base64"] = "utf8",
    offset: int | None = Query(default=None, ge=0, le=MAX_FS_OFFSET),
    limit: int | None = Query(default=None, ge=1, le=MAX_FS_READ_LIMIT),
    byte_range: str | None = Query(default=None, alias="range"),
):
    """Read a bounded file segment without loading the whole file.

    ``offset`` is a byte offset. For UTF-8 responses, ``limit`` bounds decoded
    characters (matching the filesystem tool contract); for base64 it bounds
    source bytes. The older inclusive ``range=start-end`` query remains
    supported as an alias for byte-oriented consumers.
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    is_range_read = byte_range is not None
    if is_range_read:
        if offset is not None or limit is not None:
            raise HTTPException(
                status_code=400,
                detail="Use either range or offset/limit, not both",
            )
        offset, limit = _parse_byte_range(byte_range)
    else:
        offset = offset or 0
        limit = limit or DEFAULT_FS_READ_LIMIT

    size = p.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "X-Matrx-File-Size": str(size),
        "X-Matrx-Read-Offset": str(offset),
        "X-Matrx-Read-Limit": str(limit),
    }

    if encoding == "base64":
        with p.open("rb") as file:
            file.seek(offset)
            content = file.read(limit)
        next_offset = offset + len(content)
        headers.update(
            {
                "X-Matrx-Read-Length": str(len(content)),
                "X-Matrx-Next-Offset": str(next_offset),
                "X-Matrx-Truncated": str(next_offset < size).lower(),
            }
        )
        return Response(
            content=base64.b64encode(content).decode("ascii"),
            media_type="text/plain",
            headers=headers,
        )

    if is_range_read:
        with p.open("rb") as file:
            file.seek(offset)
            raw_content = file.read(limit)
        next_offset = offset + len(raw_content)
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="File is binary or range is not on UTF-8 boundaries; use encoding=base64",
            ) from exc
        headers.update(
            {
                "X-Matrx-Read-Length": str(len(raw_content)),
                "X-Matrx-Next-Offset": str(next_offset),
                "X-Matrx-Truncated": str(next_offset < size).lower(),
            }
        )
        return Response(content=content, media_type="text/plain", headers=headers)

    try:
        with p.open("r", encoding="utf-8") as file:
            file.seek(offset)
            content = file.read(limit)
            next_offset = file.tell()
        headers.update(
            {
                "X-Matrx-Read-Length": str(len(content)),
                "X-Matrx-Next-Offset": str(next_offset),
                "X-Matrx-Truncated": str(next_offset < size).lower(),
            }
        )
        return Response(content=content, media_type="text/plain", headers=headers)
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="File is binary or offset is not on a UTF-8 boundary; use encoding=base64",
        ) from exc

@app.put("/fs/write")
async def fs_write(req: WriteRequest):
    p = Path(req.path)
    if req.create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
        
    data = base64.b64decode(req.content) if req.encoding == "base64" else req.content.encode("utf-8")

    _atomic_write(p, data, req.mode)
    return get_stat_dict(p)

@app.post("/fs/patch")
async def fs_patch(req: PatchRequest):
    """Apply 1+ sequential search-and-replace edits (old_text -> new_text).

    Mirrors matrx-ai's local (non-sandbox) fs_patch semantics exactly, so a
    sandbox-bound chat and a non-sandbox chat behave identically: each edit's
    old_text must appear exactly once in the current content unless
    replace_all is set; edits are applied in order (each sees the previous
    edit's result); the file is only written if at least one edit succeeds.
    """
    p = Path(req.path)
    existed = p.is_file()

    if not existed:
        if not req.create_if_missing:
            raise HTTPException(status_code=404, detail="File not found")
        if not req.edits or req.edits[0].old_text != "":
            raise HTTPException(
                status_code=400,
                detail="create_if_missing=True requires the first edit to have empty old_text (insert mode).",
            )
        content = ""
    else:
        content = p.read_text(encoding="utf-8")

    applied = []
    failures = []
    for i, edit in enumerate(req.edits):
        if not existed and i == 0 and edit.old_text == "":
            content = edit.new_text
            applied.append({"edit_index": i, "mode": "create"})
            continue

        count = content.count(edit.old_text)
        if count == 0:
            failures.append({"edit_index": i, "reason": "old_text not found"})
            continue
        if count > 1 and not edit.replace_all:
            failures.append({
                "edit_index": i,
                "reason": f"old_text matches {count} locations — add context or set replace_all=True",
            })
            continue

        if edit.replace_all:
            content = content.replace(edit.old_text, edit.new_text)
            applied.append({"edit_index": i, "mode": "replace_all", "matches_replaced": count})
        else:
            content = content.replace(edit.old_text, edit.new_text, 1)
            applied.append({"edit_index": i, "mode": "replace"})

    if not applied:
        raise HTTPException(
            status_code=422,
            detail={"message": f"All {len(req.edits)} edit(s) failed; file unchanged.", "failures": failures},
        )

    if not existed:
        p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, content.encode("utf-8"))
    result = get_stat_dict(p)
    result["edits_applied"] = applied
    result["edits_failed"] = failures
    return result

@app.delete("/fs/delete")
async def fs_delete(path: str, recursive: bool = False):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
        
    if p.is_file() or p.is_symlink():
        p.unlink()
    elif p.is_dir():
        if recursive:
            shutil.rmtree(p)
        else:
            try:
                p.rmdir()
            except OSError:
                raise HTTPException(status_code=400, detail="Directory not empty")
    return {"deleted": True}

@app.post("/fs/mkdir")
async def fs_mkdir(req: MkdirRequest):
    p = Path(req.path)
    p.mkdir(parents=req.parents, exist_ok=True)
    return get_stat_dict(p)

@app.post("/fs/rename")
async def fs_rename(req: RenameRequest):
    from_p = Path(req.from_path)
    to_p = Path(req.to_path)
    
    if not from_p.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if to_p.exists() and not req.overwrite:
        raise HTTPException(status_code=400, detail="Destination exists")
        
    from_p.rename(to_p)
    return get_stat_dict(to_p)

@app.post("/fs/copy")
async def fs_copy(req: CopyRequest):
    from_p = Path(req.from_path)
    to_p = Path(req.to_path)
    
    if not from_p.exists():
        raise HTTPException(status_code=404, detail="Source not found")
        
    if from_p.is_file():
        shutil.copy2(from_p, to_p)
    elif from_p.is_dir():
        if not req.recursive:
            raise HTTPException(status_code=400, detail="Use recursive=true for directories")
        shutil.copytree(from_p, to_p, dirs_exist_ok=True)
    return get_stat_dict(to_p)

@app.post("/fs/upload")
async def fs_upload(path: str = Form(...), file: UploadFile = File(...)):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    
    with p.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    return get_stat_dict(p)

@app.get("/fs/download")
async def fs_download(path: str, format: Literal["raw", "zip"] = "raw"):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
        
    if format == "raw":
        if not p.is_file():
            raise HTTPException(status_code=400, detail="Cannot download a directory as raw format, use format=zip")
        return FileResponse(p)
    elif format == "zip":
        import tempfile
        import zipfile
        
        # Create a temp file
        fd, temp_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            if p.is_file():
                zipf.write(p, arcname=p.name)
            else:
                for root, dirs, files in os.walk(p):
                    for file in files:
                        file_path = Path(root) / file
                        arcname = file_path.relative_to(p.parent)
                        zipf.write(file_path, arcname=arcname)
                        
        return FileResponse(temp_path, media_type="application/zip", filename=f"{p.name}.zip")

class BatchRequest(BaseModel):
    # Extremely simplified batch operation list
    operations: List[dict]

@app.post("/fs/batch")
async def fs_batch(req: BatchRequest):
    results = []
    for op in req.operations:
        action = op.get("action")
        try:
            if action == "delete":
                p = Path(op["path"])
                if p.is_file(): p.unlink()
                elif p.is_dir(): shutil.rmtree(p)
                results.append({"status": "success", "action": action, "path": op["path"]})
            elif action == "mkdir":
                Path(op["path"]).mkdir(parents=True, exist_ok=True)
                results.append({"status": "success", "action": action, "path": op["path"]})
            else:
                results.append({"status": "error", "error": "Unknown action"})
        except Exception as e:
            results.append({"status": "error", "error": str(e)})
            
    return {"results": results}

# Import routers
from matrx_agent.api.exec import router as exec_router
from matrx_agent.api.pty import router as pty_router
from matrx_agent.api.git import router as git_router
from matrx_agent.api.credentials import router as credentials_router
from matrx_agent.api.watch import router as watch_router
from matrx_agent.api.search import router as search_router
from matrx_agent.api.processes import router as processes_router

app.include_router(exec_router)
app.include_router(pty_router)
app.include_router(git_router)
app.include_router(credentials_router)
app.include_router(watch_router)
app.include_router(search_router)
app.include_router(processes_router)


# ─────────────────────────────────────────────────────────────────────────────
# /internal/* — persistence integration points
# ─────────────────────────────────────────────────────────────────────────────
# Called by:
#   - The orchestrator's ``shutdown.sh`` (before ``docker stop``) → /internal/shutdown
#   - The orchestrator's ``entrypoint.sh`` after the daemon comes up → /internal/startup
# Marked /internal/ so it's clear they're not for end users — they don't have
# auth right now (the daemon listens only on the container's internal network),
# but if we ever expose port 8000 externally, gate these behind a shared secret.

@app.post("/internal/startup")
def internal_startup() -> dict:
    """Idempotent startup hook — re-renders session-report.md from the prior manifest."""
    try:
        prior = read_prior_manifest()
        report = render_report(prior)
        return {
            "ok": True,
            "had_prior_session": prior is not None,
            "report_path": str(REPORT_PATH),
            "report_chars": len(report),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/internal/shutdown")
def internal_shutdown(graceful: bool = True, auto_stash: bool = True,
                      push_remote: bool = True) -> dict:
    """Run the full shutdown persistence pass.

    1. Walk for git repos and auto-stash dirty ones.
    2. Collect a final manifest including the auto-stash results.
    3. Write the manifest atomically.
    """
    autostashes: dict[str, dict] = {}
    if auto_stash:
        try:
            repos = _find_git_repos(HOME)
            autostashes = auto_stash_all_repos(repos, push_remote=push_remote)
        except Exception as e:  # noqa: BLE001
            autostashes = {"_error": {"error": str(e)}}

    try:
        manifest = collect_manifest(graceful=graceful)
        # Splice auto-stash results back into the manifest under each repo
        for repo in manifest.repos:
            stash_result = autostashes.get(repo.path)
            if stash_result is not None:
                repo.auto_stash = stash_result
        # Splice cloud-sync watcher stats so the session report can render them.
        try:
            manifest.cloud_sync = _cloud_watcher.get_stats()
        except Exception as e:  # noqa: BLE001
            _logger.warning("matrx_agent: cloud_sync stats splice failed: %s", e)
        write_manifest(manifest)
        return {
            "ok": True,
            "manifest_path": str(MANIFEST_PATH),
            "repos_scanned": len(manifest.repos),
            "auto_stashes": {k: v for k, v in autostashes.items() if not k.startswith("_")},
            "cloud_sync": manifest.cloud_sync,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "auto_stashes": autostashes}


@app.get("/internal/manifest")
def internal_manifest_get() -> dict:
    """Return the current (most recent) session manifest."""
    if not MANIFEST_PATH.exists():
        raise HTTPException(status_code=404, detail="No manifest yet (sandbox just started)")
    import json as _json
    try:
        return _json.loads(MANIFEST_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Manifest unreadable: {e}")


@app.get("/internal/cloud-sync-status")
def internal_cloud_sync_status() -> dict:
    """Return the cloud-files watcher's current state, queue, and metrics.

    Frontend uses this to render a "syncing… N queued · last sync 3s ago"
    indicator. Same shape regardless of mode (dormant/waiting/degraded/active).
    """
    return _cloud_watcher.get_status()


@app.get("/internal/session-report", response_class=PlainTextResponse)
def internal_session_report() -> str:
    """Return the rendered session-report.md as plain text. The frontend
    fetches this on connect and renders it as a welcome panel.
    """
    if REPORT_PATH.exists():
        return REPORT_PATH.read_text()
    return render_report(read_prior_manifest())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
