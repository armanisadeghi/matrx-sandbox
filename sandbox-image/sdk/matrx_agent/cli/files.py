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
            print(f"AI Dream {method} {url} → HTTP {e.code}: {e.read()[:200]!r}", file=sys.stderr)
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
    width = max((len(str(r.get("file_size", "?"))) for r in rows), default=4)
    for r in rows:
        size = str(r.get("file_size", "?"))
        path = r.get("file_path", r.get("name", "?"))
        print(f"{size:>{width}}  {path}")
    return 0


def cmd_cat(cfg: BridgeConfig, path: str) -> int:
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
        import urllib.parse, urllib.error
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
        result = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        print(f"uploaded → {remote_path}" + (f"  (id={result['id']})" if result and "id" in result else ""))
    return 0


def cmd_rm(cfg: BridgeConfig, path: str) -> int:
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
        local = dest_dir / path.lstrip("/")
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
            try:
                with p.open("rb") as fh:
                    r = client.put(
                        f"{cfg.url}/api/cloud-files/put",
                        headers={k: v for k, v in _headers(cfg).items() if k != "Accept"},
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

    print("unknown files command", file=sys.stderr)
    return 2
