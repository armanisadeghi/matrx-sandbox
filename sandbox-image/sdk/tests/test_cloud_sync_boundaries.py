from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from matrx_agent.cli.files import cmd_put
from matrx_agent.cloud_sync.client import BridgeConfig
from matrx_agent.cloud_sync.paths import is_system_path
from matrx_agent.cloud_sync.watcher import (
    CloudFilesWatcher,
    _is_retryable_bridge_error,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("report.md", False),
        ("projects/report.md", False),
        ("system-files/scraper/body.html", True),
        ("/generations/images/render.png", True),
        ("system-files-backup/notes.txt", False),
    ],
)
def test_system_path_boundary_is_segment_exact(path: str, expected: bool) -> None:
    assert is_system_path(path) is expected


def test_seed_hashes_never_tracks_system_managed_files(tmp_path: Path) -> None:
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "report.md").write_text("user", encoding="utf-8")
    (tmp_path / "system-files" / "scraper").mkdir(parents=True)
    (tmp_path / "system-files" / "scraper" / "body.html").write_text(
        "evidence",
        encoding="utf-8",
    )
    watcher = CloudFilesWatcher(cloud_root=tmp_path)

    watcher._seed_hashes()

    assert set(watcher._last_hash) == {"projects/report.md"}


def test_persisted_system_path_event_is_retired_without_bridge_call(
    tmp_path: Path,
) -> None:
    watcher = CloudFilesWatcher(cloud_root=tmp_path)

    asyncio.run(watcher._flush_upsert("system-files/scraper/body.html", "mem-old"))

    assert watcher._metrics.errors_total == 0


@pytest.mark.parametrize("status_code", [400, 403, 409, 422])
def test_permanent_client_rejections_are_not_retried(status_code: int) -> None:
    request = httpx.Request("PUT", "https://server.example/cloud-files/put")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("rejected", request=request, response=response)

    assert _is_retryable_bridge_error(error) is False


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_transient_bridge_failures_remain_retryable(status_code: int) -> None:
    request = httpx.Request("PUT", "https://server.example/cloud-files/put")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("temporary", request=request, response=response)

    assert _is_retryable_bridge_error(error) is True


def test_bridge_config_headers_carry_organization() -> None:
    """Positive control: a fully-configured BridgeConfig sends
    X-Organization-Id alongside identity — aidream's AuthMiddleware refuses
    any authenticated request missing it."""
    cfg = BridgeConfig(
        url="https://server.example",
        token="token",
        user_id="user",
        organization_id="org-1",
    )

    headers = cfg.headers()

    assert headers["X-Organization-Id"] == "org-1"
    assert headers["X-Matrx-User-Id"] == "user"
    assert headers["Authorization"] == "Bearer token"


def test_bridge_config_from_env_refuses_without_organization_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator already injects ORGANIZATION_ID into every sandbox
    container (sandbox_manager.py). A container missing it fails closed here
    — same pattern as a missing URL/token/user id — never a fallback org."""
    monkeypatch.setenv("MATRX_AIDREAM_URL", "https://server.example")
    monkeypatch.setenv("MATRX_AIDREAM_SERVICE_TOKEN", "token")
    monkeypatch.setenv("USER_ID", "user")
    monkeypatch.delenv("ORGANIZATION_ID", raising=False)

    assert BridgeConfig.from_env() is None


def test_bridge_config_from_env_succeeds_with_organization_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the refusal test above."""
    monkeypatch.setenv("MATRX_AIDREAM_URL", "https://server.example")
    monkeypatch.setenv("MATRX_AIDREAM_SERVICE_TOKEN", "token")
    monkeypatch.setenv("USER_ID", "user")
    monkeypatch.setenv("ORGANIZATION_ID", "org-1")

    cfg = BridgeConfig.from_env()

    assert cfg is not None
    assert cfg.organization_id == "org-1"


def test_cli_refuses_system_path_before_network(tmp_path: Path) -> None:
    local = tmp_path / "body.html"
    local.write_text("changed evidence", encoding="utf-8")
    cfg = BridgeConfig(
        url="https://server.example",
        token="token",
        user_id="user",
        organization_id="org-1",
    )

    assert cmd_put(cfg, str(local), "system-files/scraper/body.html") == 2


def test_identity_headers_refuse_to_omit_the_organization() -> None:
    """The ONE header builder never sends half the request context: it names
    the missing environment variable instead (law: context-is-carried-never-
    rebuilt, rule 1 — a server-to-server call forwards BOTH)."""
    from matrx_agent.bridge_headers import BridgeIdentityMissing, identity_headers

    with pytest.raises(BridgeIdentityMissing, match="ORGANIZATION_ID"):
        identity_headers(token="token", user_id="user", organization_id="")


def test_every_bridge_request_carries_the_organization_on_the_wire() -> None:
    """Forcing function: drive a real put through the transport and read the
    headers the server would receive — a per-call header copy that dropped the
    organization would pass a config-level assertion and fail here."""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={})

    cfg = BridgeConfig(
        url="https://server.example",
        token="token",
        user_id="user",
        organization_id="org-1",
    )

    async def run() -> None:
        from matrx_agent.cloud_sync.client import AsyncBridgeClient

        client = AsyncBridgeClient(cfg)
        client._client = httpx.AsyncClient(
            headers=client._client.headers,
            transport=httpx.MockTransport(handler),
        )
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("body")
            local = Path(fh.name)
        await client.put_one(local, "notes.md")
        await client.delete_one("notes.md")
        await client.close()

    asyncio.run(run())

    assert seen, "no request reached the transport"
    for headers in seen:
        assert headers["x-organization-id"] == "org-1"
        assert headers["x-matrx-user-id"] == "user"


REPO_ROOT = Path(__file__).resolve().parents[3]

#: Everything that can talk to AI Dream: the image (SDK + lifecycle scripts),
#: the orchestrator, and the local-tier scripts. Until 2026-09-17 this guard
#: scanned only the first two directories of the image, so the orchestrator's
#: hand-written X-Matrx-User-Id / X-Organization-Id pair in
#: ``sandbox_manager.create_sandbox`` sat outside every check, and so did
#: everything under ``sandbox-local/``.
SCAN_ROOTS = (
    REPO_ROOT / "sandbox-image" / "sdk",
    REPO_ROOT / "sandbox-image" / "scripts",
    REPO_ROOT / "orchestrator" / "orchestrator",
    REPO_ROOT / "sandbox-local" / "scripts",
    REPO_ROOT / "scripts",
)

#: The builders. Everything else asks one of them for its headers.
HEADER_BUILDERS = (
    REPO_ROOT / "sandbox-image" / "sdk" / "matrx_agent" / "bridge_headers.py",
    REPO_ROOT / "sandbox-image" / "scripts" / "bridge-headers.sh",
    REPO_ROOT / "orchestrator" / "orchestrator" / "bridge_headers.py",
)

#: How a caller proves it used a builder (Python or shell).
BUILDER_MARKERS = (
    "identity_headers",
    "bridge_headers",
    "MATRX_BRIDGE_HEADERS",
    "matrx_bridge_ready",
    ".headers(",
)

#: Naming the aidream base URL. A call site that has this and makes a request
#: is a bridge call, whatever it calls itself.
AIDREAM_URL_MARKERS = ("MATRX_AIDREAM_URL", "aidream_url", "resolve_aidream_url")

#: An unauthenticated call that deliberately carries no identity (a public
#: probe) declares itself on the line or in its window.
EXEMPT_MARKER = "bridge-headers: exempt"


def _scan_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc"}:
                continue
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            try:
                yield path, path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue


def test_no_second_header_builder_exists_anywhere_that_calls_ai_dream() -> None:
    """Fix the class: identity headers are built in ONE place per language. A
    new call site that hand-writes X-Matrx-User-Id would drop the organization
    again the next time somebody adds an endpoint."""
    sdk_root = Path(__file__).resolve().parents[1]
    scripts_root = sdk_root.parent / "scripts"
    allowed = set(HEADER_BUILDERS)
    offenders = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc"}:
                continue
            if path in allowed or "tests" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                # A header BEING BUILT is a quoted key or a curl -H argument;
                # prose naming the header in a docstring uses backticks.
                built = (
                    '"X-Matrx-User-Id"' in line
                    or "'X-Matrx-User-Id'" in line
                    or '-H "X-Matrx-User-Id' in line
                    or "X-Matrx-User-Id: $" in line
                )
                if built:
                    offenders.append(f"{path}:{lineno}")

    assert not offenders, (
        "identity headers must come from matrx_agent.bridge_headers / "
        "orchestrator.bridge_headers (Python) or scripts/bridge-headers.sh "
        "(shell): " + ", ".join(offenders)
    )


def _function_nodes(tree):
    import ast

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


#: A real outbound request, not ``dict.get`` / ``os.environ.get``. Kept narrow
#: on purpose: a guard that flags every ``.get(`` gets switched off.
_HTTP_CALL_RE = re.compile(
    r"""(
        httpx\. | requests\. | aiohttp\. | urlopen\( | urlretrieve\( |
        \b\w*(?:client|hx|session|http|conn|transport)\w*\s*\.\s*
        (?:get|post|put|patch|delete|request|stream|send)\s*\(
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _makes_http_call(segment: str) -> bool:
    return bool(_HTTP_CALL_RE.search(segment))


def test_every_aidream_call_site_builds_its_identity_in_the_same_function() -> None:
    """The stronger half of the class fix.

    The builder-only check above can only see a call site that hand-writes the
    header. It cannot see the worse case: a request to AI Dream that names NO
    identity at all. So: any Python function that names the aidream base URL
    and makes an HTTP request must reference a header builder in that same
    function, or say why it does not need one.
    """
    import ast

    offenders = []
    for path, text in _scan_files():
        if path.suffix != ".py":
            continue
        if not any(m in text for m in AIDREAM_URL_MARKERS):
            continue
        if path in set(HEADER_BUILDERS):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover — check:parse's job, not ours
            continue
        lines = text.splitlines()
        for fn in _function_nodes(tree):
            start, end = fn.lineno - 1, (fn.end_lineno or fn.lineno)
            segment = "\n".join(lines[start:end])
            if not any(m in segment for m in AIDREAM_URL_MARKERS):
                continue
            if not _makes_http_call(segment):
                continue
            if any(m in segment for m in BUILDER_MARKERS):
                continue
            if EXEMPT_MARKER in segment:
                continue
            offenders.append(f"{path}:{fn.lineno} ({fn.name})")

    assert not offenders, (
        "these functions call AI Dream without building identity headers in the "
        "same function — a bridge call with no actor and no organization is "
        "worse than one with half the context: " + ", ".join(offenders)
    )


def _shell_aidream_url_names(text: str) -> set[str]:
    """Every shell variable in this file that holds the aidream base URL.

    A curl rarely names ``MATRX_AIDREAM_URL`` inline — it names
    ``$PROBE_URL`` or ``$EXPECTED_AIDREAM_URL``, assigned a line or two above.
    Following the assignment is what keeps this guard from being fooled by a
    rename, and what keeps it from flagging the curl to localhost that merely
    sits near one.
    """
    names = {"MATRX_AIDREAM_URL"}
    for _ in range(3):  # resolve chains: A=$MATRX_AIDREAM_URL…; B=$A…
        for line in text.splitlines():
            assign = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if not assign:
                continue
            name, value = assign.group(1), assign.group(2)
            if any(n in value for n in names) or re.search(r"aidream.*url", name, re.I):
                names.add(name)
    return names


def _shell_command_at(lines: list[str], idx: int) -> str:
    """The whole command starting at ``idx`` — continuation lines included."""
    out = [lines[idx]]
    j = idx
    while j < len(lines) - 1 and lines[j].rstrip().endswith("\\"):
        j += 1
        out.append(lines[j])
    return "\n".join(out)


def test_every_shell_aidream_curl_carries_the_builder_or_declares_why_not() -> None:
    """Shell half: AST is not available, so the rule is the COMMAND plus a
    short window above it.

    A ``curl`` that names the aidream base URL — directly or through a variable
    assigned from it — must use the builder's header array, or carry
    ``# bridge-headers: exempt <why>`` nearby, which is how a genuinely public,
    unauthenticated probe says so out loud instead of looking like a forgotten
    identity.
    """
    lookback = 8
    offenders = []
    for path, text in _scan_files():
        if path.suffix not in {".sh", ""}:
            continue
        lines = text.splitlines()
        url_names = _shell_aidream_url_names(text)
        for idx, line in enumerate(lines):
            if "curl" not in line:
                continue
            command = _shell_command_at(lines, idx)
            if not any(n in command for n in url_names):
                continue
            context = "\n".join(lines[max(0, idx - lookback):idx]) + "\n" + command
            if "MATRX_BRIDGE_HEADERS" in context or EXEMPT_MARKER in context:
                continue
            offenders.append(f"{path}:{idx + 1}")

    assert not offenders, (
        "these shell curls reach AI Dream without the one header builder "
        "(source scripts/bridge-headers.sh and use \"${MATRX_BRIDGE_HEADERS[@]}\"), "
        "and do not declare themselves exempt: " + ", ".join(offenders)
    )


# ── The identity writer is never swallowed ──────────────────────────────────
# Every entrypoint used to run `write-bridge-env.sh || true`. The writer runs
# under `set -euo pipefail`, so any failure inside it aborted it halfway and the
# `|| true` hid that: the box came up fully wired while every SHELL on it took
# bridge-headers.sh's quiet "unwired image" branch, and `git push` failed with
# no explanation. The box must still START (an unreachable container cannot be
# debugged) — but it must never be silently unwired.

ENTRYPOINTS = (
    REPO_ROOT / "sandbox-image" / "scripts" / "entrypoint.sh",
    REPO_ROOT / "sandbox-image" / "scripts" / "entrypoint-slim.sh",
    REPO_ROOT / "sandbox-local" / "scripts" / "entrypoint-local.sh",
)


def test_no_entrypoint_swallows_the_identity_writer() -> None:
    offenders = []
    for path in ENTRYPOINTS:
        assert path.exists(), path
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "write-bridge-env.sh" not in line:
                continue
            if "||" in line and "true" in line.split("||", 1)[1]:
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "an entrypoint is discarding the identity writer's failure; the box "
        "would run wired-but-invisible to every shell on it: " + "; ".join(offenders)
    )


def test_every_entrypoint_still_starts_the_box_when_the_writer_fails() -> None:
    """The other half of the ruling: report it, don't abort the boot."""
    for path in ENTRYPOINTS:
        text = path.read_text(encoding="utf-8")
        assert "if ! /opt/sandbox/scripts/write-bridge-env.sh; then" in text, path
        assert "bridge-env.FAILED" in text, path


def test_the_writer_leaves_a_marker_a_reader_can_scream_about(tmp_path) -> None:
    """Forcing function: run the real script with an unwritable target and
    prove BOTH readers (the shell builder and the SDK) refuse loudly."""
    import os
    import subprocess

    writer = REPO_ROOT / "sandbox-image" / "scripts" / "write-bridge-env.sh"
    env_dir = tmp_path / "matrx"
    env_dir.mkdir()
    marker = env_dir / "bridge-env.FAILED"
    env = {
        **os.environ,
        "MATRX_BRIDGE_ENV_DIR": str(env_dir),
        # A DIRECTORY where the identity file should go: the write fails.
        "MATRX_BRIDGE_ENV_FILE": str(env_dir / "occupied"),
        "MATRX_BRIDGE_ENV_FAILED_FILE": str(marker),
        "MATRX_BRIDGE_PROFILE_DROPIN": str(tmp_path / "profile.d" / "00-matrx.sh"),
        "MATRX_BRIDGE_ENV_OWNER": "root:root",
        "USER_ID": "user-123",
        "ORGANIZATION_ID": "org-9",
        "MATRX_AIDREAM_URL": "https://server.example.test",
        "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
    }
    (env_dir / "occupied").mkdir()

    proc = subprocess.run([str(writer)], env=env, capture_output=True, text=True)

    assert proc.returncode != 0, "the writer must not pretend it succeeded"
    assert marker.exists(), "no marker: the failure would be invisible"
    assert "could not publish its identity" in marker.read_text(encoding="utf-8")

    # Reader 1 — the SDK. A shell with no identity at all is normally quiet;
    # with the marker present it names the failure instead.
    from matrx_agent.bridge_headers import published_identity_failure

    assert "could not publish" in (published_identity_failure(marker) or "")

    # Reader 2 — the shell builder. Nothing exported, marker present.
    probe = (
        f'MATRX_BRIDGE_ENV_FILE=/nonexistent/x '
        f'MATRX_BRIDGE_ENV_FAILED_FILE={marker} '
        f'bash -c \'unset MATRX_AIDREAM_URL MATRX_AIDREAM_SERVICE_TOKEN USER_ID '
        f'ORGANIZATION_ID; . {REPO_ROOT}/sandbox-image/scripts/bridge-headers.sh; '
        f'matrx_bridge_ready probe\''
    )
    shell = subprocess.run(["bash", "-c", probe], capture_output=True, text=True)
    assert shell.returncode == 1
    assert "failed to publish its identity" in shell.stderr
