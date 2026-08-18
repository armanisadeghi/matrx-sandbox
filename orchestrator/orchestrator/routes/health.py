"""Health + system-info routes.

`/health` is the lightweight liveness probe (no auth, fast). `/system` reports
host disk/memory/cpu pressure plus container counts so the matrx-frontend admin
panel can show operators when an orchestrator is approaching its limits — the
"deploy silently fails because root disk is at 97%" failure mode that bit us
on 2026-04-26 should be visible *before* the deploy fails.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

from fastapi import APIRouter

from orchestrator import sandbox_manager
from orchestrator.config import settings
from orchestrator.models import HealthResponse, SystemInfoResponse
from orchestrator.store import PostgresSandboxStore

router = APIRouter(tags=["health"])

_start_time = time.time()


def _sandboxes_for_this_host(sandboxes):
    """Limit shared-ledger rows to the tier served by this orchestrator."""
    host_tier = settings.host_tier
    if not host_tier:
        return sandboxes
    return [
        sandbox
        for sandbox in sandboxes
        if getattr(getattr(sandbox, "tier", None), "value", getattr(sandbox, "tier", None))
        == host_tier
    ]


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check for the orchestrator service."""
    sandboxes = _sandboxes_for_this_host(await sandbox_manager.list_sandboxes())
    active = [s for s in sandboxes if s.status in ("ready", "running", "starting")]
    backend = "postgres" if isinstance(sandbox_manager._get_store(), PostgresSandboxStore) else "memory"
    return HealthResponse(
        status="healthy",
        active_sandboxes=len(active),
        uptime_seconds=round(time.time() - _start_time, 1),
        store_backend=backend,
        durable_storage=backend == "postgres",
    )


def _read_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into a dict of kB values."""
    result: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                # Lines look like:  "MemTotal:       16312828 kB"
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                rhs = parts[1].strip().split()
                if rhs and rhs[0].isdigit():
                    result[key] = int(rhs[0])
    except OSError:
        pass
    return result


def _read_loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError:
        return None


def _docker_container_counts() -> dict[str, int]:
    """Best-effort counts of containers managed by this orchestrator.

    Uses the same Docker client the orchestrator uses for spawning sandboxes;
    falls back to zeros if Docker is unreachable.
    """
    try:
        client = sandbox_manager._get_docker_client()
        all_containers = client.containers.list(all=True, filters={"label": "matrx.sandbox_id"})
        running = [c for c in all_containers if c.status == "running"]
        return {"sandbox_total": len(all_containers), "sandbox_running": len(running)}
    except Exception:
        return {"sandbox_total": 0, "sandbox_running": 0}


@router.get("/system", response_model=SystemInfoResponse)
async def system_info():
    """Live system pressure for the host running this orchestrator.

    Authentication required. Used by the matrx-frontend admin panel to surface
    capacity warnings and to catch silent failures (stale deploy, disk-full,
    runaway memory) that aren't reflected in `/health`.
    """
    # Disk for the FS that holds /home/ec2-user/orchestrator (or, on the hosted
    # tier, the sandbox volumes path). Single root device covers all sandboxes
    # and orchestrator state today.
    du = shutil.disk_usage("/")
    disk_used_pct = round(du.used / du.total * 100, 1) if du.total else 0.0

    mem = _read_meminfo()
    mem_total_kb = mem.get("MemTotal", 0)
    mem_avail_kb = mem.get("MemAvailable", 0)
    mem_used_kb = max(mem_total_kb - mem_avail_kb, 0)
    mem_used_pct = round(mem_used_kb / mem_total_kb * 100, 1) if mem_total_kb else 0.0

    load = _read_loadavg()
    cpu_count = os.cpu_count() or 0

    counts = await asyncio.to_thread(_docker_container_counts)

    # Both orchestrators share one ledger. Reporting the global row count here
    # made each host look badly drifted even when its own containers and rows
    # matched, so scope operational counts to this host's configured tier.
    sandboxes = _sandboxes_for_this_host(await sandbox_manager.list_sandboxes())
    active = sum(1 for s in sandboxes if s.status in ("ready", "running", "starting"))

    return SystemInfoResponse(
        tier=settings.host_tier or None,
        uptime_seconds=round(time.time() - _start_time, 1),
        disk_total_bytes=du.total,
        disk_used_bytes=du.used,
        disk_free_bytes=du.free,
        disk_used_pct=disk_used_pct,
        memory_total_kb=mem_total_kb,
        memory_used_kb=mem_used_kb,
        memory_available_kb=mem_avail_kb,
        memory_used_pct=mem_used_pct,
        cpu_count=cpu_count,
        load_1m=load[0] if load else None,
        load_5m=load[1] if load else None,
        load_15m=load[2] if load else None,
        sandboxes_in_db=len(sandboxes),
        sandboxes_active=active,
        sandbox_containers_total=counts["sandbox_total"],
        sandbox_containers_running=counts["sandbox_running"],
    )
