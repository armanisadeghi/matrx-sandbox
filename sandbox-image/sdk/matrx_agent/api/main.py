import asyncio
import base64
import logging
import os
import shutil
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form
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

    yield

    # Shutdown: best-effort final manifest write. The container may still be
    # killed mid-write — that's fine, the prior checkpoint is the floor.
    try:
        await _checkpoint.stop()
    except Exception as e:  # noqa: BLE001
        _logger.warning("matrx_agent: checkpoint daemon stop error: %s", e)
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
    start: int
    end: int
    replacement: str

class PatchRequest(BaseModel):
    path: str
    edits: List[EditChunk]

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
    st = file_path.stat()
    return {
        "name": file_path.name,
        "path": str(file_path),
        "kind": "file" if file_path.is_file() else "dir" if file_path.is_dir() else "symlink",
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mode": stat.S_IMODE(st.st_mode),
        "target": str(file_path.resolve()) if file_path.is_symlink() else None
    }

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
async def fs_list(path: str, recursive: bool = False, depth: int = 1):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    
    entries = []
    # simplistic listing for depth=1
    for child in p.iterdir():
        try:
            entries.append(get_stat_dict(child))
        except FileNotFoundError:
            continue
            
    return {"entries": entries}

@app.get("/fs/stat")
async def fs_stat(path: str):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    return get_stat_dict(p)

@app.get("/fs/read")
async def fs_read(path: str, encoding: Literal["utf8", "base64"] = "utf8"):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
        
    content = p.read_bytes()
    if encoding == "base64":
        return Response(content=base64.b64encode(content).decode("utf-8"), media_type="text/plain")
    else:
        try:
            return Response(content=content.decode("utf-8"), media_type="text/plain")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File is binary, use encoding=base64")

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
    p = Path(req.path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
        
    content = p.read_text(encoding="utf-8")
    
    # apply edits in reverse order so indices don't shift
    edits = sorted(req.edits, key=lambda x: x.start, reverse=True)
    for edit in edits:
        content = content[:edit.start] + edit.replacement + content[edit.end:]

    _atomic_write(p, content.encode("utf-8"))
    return get_stat_dict(p)

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
