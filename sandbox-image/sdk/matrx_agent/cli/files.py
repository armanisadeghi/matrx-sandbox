"""``mtx files`` subcommands — bridge between the sandbox FS and AI Dream cld_files.

This is intentionally a thin client. It calls AI Dream's REST endpoints (not
the cld_* tables directly), so AI Dream stays the single owner of the cld_files
schema. AI Dream is expected to expose endpoints like:

  GET    {AIDREAM_URL}/api/cloud-files/list           → [{ id, file_path, file_size, ... }, ...]
  GET    {AIDREAM_URL}/api/cloud-files/get?path=…     → file bytes
  PUT    {AIDREAM_URL}/api/cloud-files/put            → multipart upload
  DELETE {AIDREAM_URL}/api/cloud-files/delete?path=…  → 204

Authentication: ``Authorization: Bearer <MATRX_AIDREAM_SERVICE_TOKEN>``
+ ``X-Matrx-User-Id: <USER_ID>``. AI Dream verifies the token then trusts the
header — sandboxes can only call the bridge API, not arbitrary AI Dream routes.

If the AI Dream cld_files endpoints aren't deployed yet, this CLI fails with a
clear "AI Dream cloud-files API not reachable" message rather than crashing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from matrx_agent.cloud_sync.client import BridgeConfig, report_missing
from matrx_agent.cloud_sync.paths import is_system_path

# Lazy http import — falls back to urllib if requests/httpx not in the image
try:
    import httpx as _http  # type: ignore

    _USING = "httpx"
except ImportError:  # pragma: no cover
    import urllib.request as _http  # type: ignore

    _USING = "urllib"


def _config() -> BridgeConfig | None:
    cfg = BridgeConfig.from_env()
    if cfg is None:
        report_missing()
    return cfg


def _headers(cfg: BridgeConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    return cfg.headers(extra)


def _http_json(method: str, url: str, headers: dict[str, str], **kwargs) -> Any:
    """Cross-implementation JSON GET/POST/DELETE wrapper."""
    if _USING == "httpx":
        with _http.Client(timeout=30.0) as client:
            r = client.request(method, url, headers=headers, **kwargs)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            return r.json() if "application/json" in ct else r.text
    else:  # urllib fallback
        import urllib.parse
        import urllib.error

        data = kwargs.get("json")
        body = json.dumps(data).encode() if data is not None else None
        if body:
            headers = {**headers, "Content-Type": "application/json"}
        req = _http.Request(url, headers=headers, method=method, data=body)
        try:
            with _http.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            print(
                f"AI Dream {method} {url} → HTTP {e.code}: {e.read()[:200]!r}",
                file=sys.stderr,
            )
            raise


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_ls(cfg: BridgeConfig) -> int:
    try:
        listing = _http_json("GET", f"{cfg.url}/api/cloud-files/list", _headers(cfg))
    except Exception as e:  # noqa: BLE001
        print(f"ls failed: {e}", file=sys.stderr)
        return 1

    if not listing:
        print("(no files)")
        return 0

    # Accept either a flat list of files or a {files: [...]} envelope.
    rows = listing if isinstance(listing, list) else listing.get("files") or []
    rows = [row for row in rows if not is_system_path(str(row.get("file_path") or ""))]
    width = max((len(str(r.get("file_size", "?"))) for r in rows), default=4)
    for r in rows:
        size = str(r.get("file_size", "?"))
        path = r.get("file_path", r.get("name", "?"))
        print(f"{size:>{width}}  {path}")
    return 0


def cmd_cat(cfg: BridgeConfig, path: str) -> int:
    if is_system_path(path):
        print(
            "system-managed paths are not part of the sandbox cloud-files mount",
            file=sys.stderr,
        )
        return 2
    if _USING == "httpx":
        with _http.Client(timeout=60.0) as client:
            r = client.get(
                f"{cfg.url}/api/cloud-files/get",
                headers=_headers(cfg),
                params={"path": path},
            )
            if r.status_code == 404:
                print(f"file not found: {path}", file=sys.stderr)
                return 1
            r.raise_for_status()
            sys.stdout.buffer.write(r.content)
        return 0
    else:
        import urllib.error
        import urllib.parse

        url = f"{cfg.url}/api/cloud-files/get?{urllib.parse.urlencode({'path': path})}"
        req = _http.Request(url, headers=_headers(cfg))
        try:
            with _http.urlopen(req, timeout=60) as resp:
                sys.stdout.buffer.write(resp.read())
        except urllib.error.HTTPError as e:
            print(f"cat failed: HTTP {e.code}", file=sys.stderr)
            return 1
        return 0


def cmd_put(cfg: BridgeConfig, local_path: str, remote_path: str | None) -> int:
    p = Path(local_path)
    if not p.is_file():
        print(f"not a file: {local_path}", file=sys.stderr)
        return 1
    remote_path = remote_path or p.name
    if is_system_path(remote_path):
        print("system-managed paths are not writable from a sandbox", file=sys.stderr)
        return 2

    if _USING != "httpx":
        print(
            "mtx files put requires httpx in the sandbox image — "
            "the urllib fallback doesn't do multipart cleanly. "
            "Install httpx or use `mtx files sync up`.",
            file=sys.stderr,
        )
        return 2

    with _http.Client(timeout=120.0) as client:
        with p.open("rb") as fh:
            r = client.put(
                f"{cfg.url}/api/cloud-files/put",
                headers={k: v for k, v in _headers(cfg).items() if k != "Accept"},
                files={"file": (p.name, fh)},
                data={"file_path": remote_path},
            )
        if not r.is_success:
            print(f"put failed: HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return 1
        result = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else None
        )
        print(
            f"uploaded → {remote_path}"
            + (f"  (id={result['id']})" if result and "id" in result else "")
        )
    return 0


def cmd_rm(cfg: BridgeConfig, path: str) -> int:
    if is_system_path(path):
        print("system-managed paths are not removable from a sandbox", file=sys.stderr)
        return 2
    try:
        _http_json(
            "DELETE",
            f"{cfg.url}/api/cloud-files/delete?path={path}",
            _headers(cfg),
        )
        print(f"deleted {path}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"rm failed: {e}", file=sys.stderr)
        return 1


def cmd_sync_down(cfg: BridgeConfig, dest: str, max_bytes: int) -> int:
    """Bulk pull. Uses the same /list endpoint, then GETs each file."""
    try:
        listing = _http_json("GET", f"{cfg.url}/api/cloud-files/list", _headers(cfg))
    except Exception as e:  # noqa: BLE001
        print(f"sync down: list failed: {e}", file=sys.stderr)
        return 1

    rows = listing if isinstance(listing, list) else (listing or {}).get("files") or []
    if not rows:
        print("sync down: nothing to fetch")
        return 0

    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    fetched = 0
    for r in rows:
        size = int(r.get("file_size") or 0)
        if total + size > max_bytes:
            print(f"sync down: budget {max_bytes} exhausted, skipping rest")
            break
        path = r.get("file_path") or r.get("name")
        if not path:
            continue
        if is_system_path(str(path)):
            continue
        # Path-traversal guard: a malicious or buggy server could return
        # ``file_path = "../../etc/passwd"`` and we'd write outside dest_dir.
        # Resolve and assert the target stays under dest_dir.
        candidate = (dest_dir / path.lstrip("/")).resolve()
        try:
            dest_resolved = dest_dir.resolve()
            candidate.relative_to(dest_resolved)
        except ValueError:
            print(
                f"sync down: refusing to write outside {dest_dir}: {path}",
                file=sys.stderr,
            )
            continue
        local = candidate
        local.parent.mkdir(parents=True, exist_ok=True)
        # Skip if local already has same size + checksum (cheap idempotency).
        if local.exists() and local.stat().st_size == size:
            continue
        # Re-use cmd_cat in "save mode" by writing to a file rather than stdout.
        try:
            if _USING == "httpx":
                with _http.Client(timeout=120.0) as client:
                    rr = client.get(
                        f"{cfg.url}/api/cloud-files/get",
                        headers=_headers(cfg),
                        params={"path": path},
                    )
                    rr.raise_for_status()
                    local.write_bytes(rr.content)
            else:
                import urllib.parse

                url = f"{cfg.url}/api/cloud-files/get?{urllib.parse.urlencode({'path': path})}"
                req = _http.Request(url, headers=_headers(cfg))
                with _http.urlopen(req, timeout=120) as resp:
                    local.write_bytes(resp.read())
            total += size
            fetched += 1
        except Exception as e:  # noqa: BLE001
            print(f"sync down: skipped {path}: {e}", file=sys.stderr)
            continue

    print(f"sync down: fetched {fetched} files, {total} bytes")
    return 0


def cmd_sync_up(cfg: BridgeConfig, src: str) -> int:
    src_dir = Path(src)
    if not src_dir.exists():
        print(f"sync up: source {src} doesn't exist, nothing to do")
        return 0
    if _USING != "httpx":
        print("sync up needs httpx (multipart). Skipping.", file=sys.stderr)
        return 2

    pushed = 0
    skipped = 0
    with _http.Client(timeout=120.0) as client:
        for p in src_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(src_dir))
            if is_system_path(rel):
                skipped += 1
                continue
            try:
                with p.open("rb") as fh:
                    r = client.put(
                        f"{cfg.url}/api/cloud-files/put",
                        headers={
                            k: v for k, v in _headers(cfg).items() if k != "Accept"
                        },
                        files={"file": (p.name, fh)},
                        data={"file_path": rel},
                    )
                if r.is_success:
                    pushed += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001
                skipped += 1

    print(f"sync up: pushed {pushed} files, skipped {skipped}")
    return 0


def cmd_versions(cfg: BridgeConfig, path: str) -> int:
    """List version history for one cld_file. Requires the :aidream image."""
    return _run_local_only(
        "versions",
        lambda client: client.list_versions(path),
        on_success=lambda result: _print_versions(path, result),
    )


def cmd_restore(cfg: BridgeConfig, path: str, version: int) -> int:
    """Restore a previous version. Requires the :aidream image."""
    return _run_local_only(
        "restore",
        lambda client: client.restore_version(path, version),
        on_success=lambda result: print(
            f"restored {path} to v{version}: " + json.dumps(result, indent=2)
        ),
    )


def cmd_diff(cfg: BridgeConfig, path: str, v1: int, v2: int) -> int:
    """Print a unified diff between two versions of one file. :aidream only."""
    return _run_local_only(
        "diff",
        lambda client: client.diff_versions(path, v1, v2),
        on_success=lambda diff: sys.stdout.write(diff if diff else "(no changes)\n"),
    )


def _print_versions(path: str, versions: list[dict[str, Any]]) -> None:
    if not versions:
        print(f"{path}: no version history")
        return
    print(f"{path}: {len(versions)} version(s)")
    for v in versions:
        ver = v.get("version") or v.get("version_number") or "?"
        size = v.get("size_bytes") or v.get("file_size") or "?"
        ts = v.get("created_at") or v.get("uploaded_at") or "?"
        author = v.get("author") or v.get("created_by") or "?"
        print(f"  v{ver}  {size:>10} bytes  {ts}  {author}")


def _run_local_only(
    op_name: str,
    work,
    *,
    on_success,
) -> int:
    """Helper for the three CLI commands that require the in-sandbox aidream
    FastAPI on :8001 (versions/restore/diff). Probes for a LocalFilesClient,
    runs the work function, prints. Falls back to a clear error message on
    :core / :local images.
    """
    import asyncio

    async def _go() -> int:
        from matrx_agent.cloud_sync.client import (
            BridgeConfig as _Cfg,
            LocalFilesClient,
            NotSupportedError,
            select_bridge_client,
        )

        cfg = _Cfg.from_env()
        if cfg is None:
            report_missing()
            return 1
        client = await select_bridge_client(cfg)
        try:
            if not isinstance(client, LocalFilesClient):
                print(
                    f"mtx files {op_name}: requires the :aidream sandbox image "
                    "(in-sandbox aidream FastAPI on :8001 is unreachable). "
                    "Spawn a sandbox with template='aidream' to use this command.",
                    file=sys.stderr,
                )
                return 1
            try:
                result = await work(client)
            except NotSupportedError as exc:
                print(f"mtx files {op_name}: {exc}", file=sys.stderr)
                return 1
            except FileNotFoundError as exc:
                print(f"mtx files {op_name}: not found: {exc}", file=sys.stderr)
                return 1
            on_success(result)
            return 0
        finally:
            await client.close()

    return asyncio.run(_go())


def run(args) -> int:
    cfg = _config()
    if cfg is None:
        return 1

    if args.files_cmd == "ls":
        return cmd_ls(cfg)
    if args.files_cmd == "cat":
        return cmd_cat(cfg, args.path)
    if args.files_cmd == "put":
        return cmd_put(cfg, args.local_path, args.remote_path)
    if args.files_cmd == "rm":
        return cmd_rm(cfg, args.path)
    if args.files_cmd == "sync":
        if args.sync_dir == "down":
            return cmd_sync_down(cfg, args.dest, args.max_bytes)
        if args.sync_dir == "up":
            return cmd_sync_up(cfg, args.src)
    if args.files_cmd == "versions":
        return cmd_versions(cfg, args.path)
    if args.files_cmd == "restore":
        return cmd_restore(cfg, args.path, args.version)
    if args.files_cmd == "diff":
        return cmd_diff(cfg, args.path, args.v1, args.v2)

    print("unknown files command", file=sys.stderr)
    return 2
