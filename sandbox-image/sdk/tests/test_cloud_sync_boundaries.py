from __future__ import annotations

import asyncio
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


def test_no_second_header_builder_exists_in_the_image() -> None:
    """Fix the class: identity headers are built in ONE place. A new call site
    that hand-writes X-Matrx-User-Id would drop the organization again the next
    time somebody adds an endpoint."""
    sdk_root = Path(__file__).resolve().parents[1]
    scripts_root = sdk_root.parent / "scripts"
    allowed = {
        sdk_root / "matrx_agent" / "bridge_headers.py",
        scripts_root / "bridge-headers.sh",
    }
    offenders = []
    for root in (sdk_root, scripts_root):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc"}:
                continue
            if path in allowed or "tests" in path.parts:
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
        "identity headers must come from matrx_agent.bridge_headers (Python) or "
        "scripts/bridge-headers.sh (shell): " + ", ".join(offenders)
    )
