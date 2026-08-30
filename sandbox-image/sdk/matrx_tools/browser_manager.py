"""Fail-closed client for the canonical AI Dream Browser Manager.

The sandbox never launches Chromium and never receives a profile filesystem,
worker address, fencing token, or Vault material.  It submits the existing
``matrx_tools`` browser calls to AI Dream using the same approved-server
credential already injected for Cloud Files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


class BrowserManagerNotConfiguredError(RuntimeError):
    """Raised rather than silently creating a second browser implementation."""


class BrowserManagerRequestError(RuntimeError):
    """Safe Browser Manager refusal; response bodies are intentionally omitted."""


@dataclass(frozen=True)
class BrowserManagerConfig:
    base_url: str
    service_token: str
    user_id: str
    organization_id: str
    profile_id: str
    execution_target: str
    sandbox_id: str

    @classmethod
    def from_env(cls) -> "BrowserManagerConfig":
        values = {
            "base_url": os.environ.get("MATRX_AIDREAM_URL", "").rstrip("/"),
            "service_token": os.environ.get("MATRX_AIDREAM_SERVICE_TOKEN", ""),
            "user_id": os.environ.get("USER_ID", ""),
            # aidream's AuthMiddleware refuses every authenticated request
            # that names no organization (400 organization_required) before
            # it routes. The orchestrator already injects ORGANIZATION_ID
            # into every sandbox container (sandbox_manager.py) — missing it
            # here is a provisioning defect, not something this client papers
            # over with a fallback organization.
            "organization_id": os.environ.get("ORGANIZATION_ID", ""),
            "profile_id": os.environ.get("MATRX_BROWSER_PROFILE_ID", ""),
            "execution_target": os.environ.get("MATRX_BROWSER_EXECUTION_TARGET", ""),
            "sandbox_id": os.environ.get("SANDBOX_ID", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            env_names = {
                "base_url": "MATRX_AIDREAM_URL",
                "service_token": "MATRX_AIDREAM_SERVICE_TOKEN",
                "user_id": "USER_ID",
                "organization_id": "ORGANIZATION_ID",
                "profile_id": "MATRX_BROWSER_PROFILE_ID",
                "execution_target": "MATRX_BROWSER_EXECUTION_TARGET",
                "sandbox_id": "SANDBOX_ID",
            }
            needed = ", ".join(env_names[name] for name in missing)
            raise BrowserManagerNotConfiguredError(
                f"Canonical Browser Manager is not configured; missing {needed}. "
                "The sandbox refuses to launch a local fallback browser."
            )
        if values["execution_target"] not in {"browser_fleet", "sandbox"}:
            raise BrowserManagerNotConfiguredError(
                "MATRX_BROWSER_EXECUTION_TARGET must be browser_fleet or sandbox."
            )
        return cls(**values)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.service_token}",
            "X-Matrx-User-Id": self.user_id,
            "X-Organization-Id": self.organization_id,
            "Accept": "application/json",
        }


class BrowserManagerClient:
    def __init__(
        self,
        config: BrowserManagerConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or BrowserManagerConfig.from_env()
        self._client = client or httpx.AsyncClient(
            headers=self.config.headers(), timeout=httpx.Timeout(65.0)
        )
        self._owns_client = client is None
        self.run_id: str | None = None
        self.active_page_id: str | None = None
        self.current_url: str | None = None

    @property
    def is_running(self) -> bool:
        return self.run_id is not None

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self.config.base_url}{path}",
                headers=self.config.headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise BrowserManagerRequestError("Browser Manager returned an invalid response.")
            return payload
        except BrowserManagerRequestError:
            raise
        except httpx.HTTPStatusError as exc:
            raise BrowserManagerRequestError(
                f"Browser Manager refused the request (HTTP {exc.response.status_code})."
            ) from exc
        except httpx.HTTPError as exc:
            raise BrowserManagerRequestError("Browser Manager is unreachable.") from exc

    async def ensure_run(self) -> str:
        if self.run_id is not None:
            return self.run_id
        payload = await self._post(
            "/browser-manager/internal/sandbox/runs",
            {
                "profile_id": self.config.profile_id,
                "mode": "handoff_capable",
                "execution_target": self.config.execution_target,
                "activation_key": f"sandbox:{self.config.sandbox_id}",
                "runtime_execution_id": None,
            },
        )
        run = payload.get("run")
        run_id = run.get("run_id") if isinstance(run, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise BrowserManagerRequestError("Browser Manager did not return a run identity.")
        self.run_id = run_id
        self.current_url = run.get("current_url") if isinstance(run, dict) else None
        return run_id

    async def command(self, command: dict[str, Any]) -> dict[str, Any]:
        run_id = await self.ensure_run()
        payload = await self._post(
            f"/browser-manager/internal/sandbox/runs/{run_id}/command",
            {"command": command},
        )
        if not payload.get("ok"):
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else "browser_command_refused"
            raise BrowserManagerRequestError(f"Browser command refused ({code}).")
        page_id = payload.get("active_page_id")
        if isinstance(page_id, str):
            self.active_page_id = page_id
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("url"), str):
            self.current_url = result["url"]
        return payload

    async def capture(self) -> dict[str, Any]:
        run_id = await self.ensure_run()
        payload = await self._post(
            f"/browser-manager/internal/sandbox/runs/{run_id}/capture"
        )
        if not payload.get("ok") or not payload.get("captured"):
            raise BrowserManagerRequestError("Browser screenshot is not available.")
        return payload

    async def close(self) -> None:
        if self.run_id is not None:
            await self._post(
                f"/browser-manager/internal/sandbox/runs/{self.run_id}/stop"
            )
            self.run_id = None
            self.active_page_id = None
            self.current_url = None
        if self._owns_client:
            await self._client.aclose()
