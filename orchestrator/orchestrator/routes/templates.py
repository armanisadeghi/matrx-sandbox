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


# Per-template image overrides. Most templates share the bare image; only
# templates whose runtime really differs (e.g. aidream bakes /opt/aidream-template
# + Python 3.13 venv) need their own image. Empty value = use the default
# settings.sandbox_image.
#
# Mapping is hand-maintained for now; promote to a Settings field or DB row
# if we end up with > ~5 distinct images.
_TEMPLATE_IMAGE_OVERRIDES: dict[str, str] = {
    "aidream": "matrx-sandbox:aidream",
}


def resolve_template_image(template: str | None) -> str | None:
    """Return the Docker image to spawn for ``template``, or None to use the
    orchestrator's default ``settings.sandbox_image``.

    Imported by ``sandbox_manager.create_sandbox`` so the per-template image
    decision lives in one place.
    """
    if not template:
        return None
    return _TEMPLATE_IMAGE_OVERRIDES.get(template)


def _builtin_templates() -> list[TemplateInfo]:
    default_image = settings.sandbox_image
    tier = settings.host_tier or None
    def _img(tpl: str) -> str:
        return _TEMPLATE_IMAGE_OVERRIDES.get(tpl, default_image)
    return [
        TemplateInfo(
            id="bare",
            version="1",
            description="Default sandbox — Ubuntu 22.04 + Python 3.11 + Node 20 + git + ripgrep + Chromium.",
            image=_img("bare"),
            tier=tier,
            languages=["python", "node", "bash"],
        ),
        TemplateInfo(
            id="node-22",
            version="1",
            description="Node-focused. Defaults pnpm + corepack; expects projects to use Node 22 LTS.",
            image=_img("node-22"),
            tier=tier,
            languages=["node", "typescript"],
        ),
        TemplateInfo(
            id="python-3.13",
            version="1",
            description="Python-focused. Adds uv on top of the bare image; Python 3.13 toolchain.",
            image=_img("python-3.13"),
            tier=tier,
            languages=["python"],
        ),
        TemplateInfo(
            id="aidream",
            version="1",
            description=(
                "Heavy variant — bakes the full aidream monorepo at "
                "/opt/aidream-template; first spawn copies it to "
                "/home/agent/aidream (per-user persistent volume). FastAPI is "
                "NOT auto-started; run `mtx aidream serve` to launch it. "
                "Use `mtx aidream update` to refresh the working copy "
                "without rebuilding the sandbox."
            ),
            image=_img("aidream"),
            tier=tier,
            languages=["python", "node", "bash"],
        ),
    ]


@router.get("", response_model=TemplateListResponse)
async def list_templates() -> TemplateListResponse:
    """List sandbox templates available on this orchestrator."""
    return TemplateListResponse(templates=_builtin_templates())
