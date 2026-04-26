"""Template registry — which sandbox variants the orchestrator can spawn.

For now this is a hand-maintained list. All templates share the same Docker
image (set via ``MATRX_SANDBOX_IMAGE``); the difference between them is the
``SANDBOX_TEMPLATE`` env var the daemon reads at startup, plus per-template
defaults the frontend uses (e.g. node-22 templates pre-configure ``corepack``).

Evolves to a registry table (or per-template Dockerfiles) when we have a
concrete reason to need fully separate base images.
"""

from __future__ import annotations

from fastapi import APIRouter

from orchestrator.config import settings
from orchestrator.models import TemplateInfo, TemplateListResponse

router = APIRouter(prefix="/templates", tags=["templates"])


def _builtin_templates() -> list[TemplateInfo]:
    image = settings.sandbox_image
    tier = settings.host_tier or None
    return [
        TemplateInfo(
            id="bare",
            version="1",
            description="Default sandbox — Ubuntu 22.04 + Python 3.11 + Node 20 + git + ripgrep + Chromium.",
            image=image,
            tier=tier,
            languages=["python", "node", "bash"],
        ),
        TemplateInfo(
            id="node-22",
            version="1",
            description="Node-focused. Defaults pnpm + corepack; expects projects to use Node 22 LTS.",
            image=image,
            tier=tier,
            languages=["node", "typescript"],
        ),
        TemplateInfo(
            id="python-3.13",
            version="1",
            description="Python-focused. Adds uv on top of the bare image; Python 3.13 toolchain.",
            image=image,
            tier=tier,
            languages=["python"],
        ),
    ]


@router.get("", response_model=TemplateListResponse)
async def list_templates() -> TemplateListResponse:
    """List sandbox templates available on this orchestrator."""
    return TemplateListResponse(templates=_builtin_templates())
