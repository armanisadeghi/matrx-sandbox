"""Sandbox lifecycle manager — creates, monitors, and destroys Docker containers.

Manages the full lifecycle: create, wait-for-ready, exec, heartbeat, destroy.
Uses a singleton Docker client to avoid connection leaks.
Uses a pluggable SandboxStore for persistence (in-memory or Postgres).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os
import re
from pathlib import Path
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from types import SimpleNamespace

import docker
from docker.errors import DockerException, NotFound, APIError

from orchestrator.bridge_headers import identity_headers
from orchestrator.browser_profile import resolve_browser_profile
from orchestrator.config import settings
from orchestrator.boot_readiness import BOOT_FOLLOW_INTERVAL_SECONDS
from orchestrator.knobs import knob_float, knob_int, knob_str
from orchestrator.runtime_isolation import container_runtime_isolation
from orchestrator.models import SandboxBoot, SandboxResponse, SandboxStatus
from orchestrator.storage_layout import (
    StorageLocation,
    ec2_home_volume_name,
    ensure_ec2_home_volume,
    ensure_user_volume,
    inherited_from,
    resolve_user_storage,
    user_volume_name,
    validate_ec2_home_volume,
)
from orchestrator.store import SandboxStore, create_store

logger = logging.getLogger(__name__)

# ── CWD tracking ──────────────────────────────────────────────────────────────
# Each sandbox has a server-side CWD so stateless Docker exec calls can chain
# directory changes across requests (e.g. "cd home" then "ls" shows /home).
_CWD_SENTINEL = "___MATRX_CWD_SENTINEL_9f8a7b___"
_DEFAULT_CWD = "/home/agent"
_sandbox_cwd: dict[str, str] = {}  # sandbox_id -> current working directory

# Pluggable sandbox store (replaces old in-memory _sandboxes dict)
_store: SandboxStore | None = None

# Singleton Docker client — avoids creating new connections on every call (C7)
_docker_client: docker.DockerClient | None = None


class SandboxLivenessUnavailable(RuntimeError):
    """Docker could not answer a single-box liveness check safely."""


_LIVE_CONTAINER_STATUSES = {"ready", "running", "starting"}
_TRANSITIONAL_DOCKER_STATUSES = {"created", "restarting"}


async def get_live_sandbox_for_issuance(sandbox_id: str) -> SandboxResponse | None:
    """Read a durable row and prove its container is running before minting.

    Boot reconciliation deliberately runs in the background for Postgres so a
    large host can become routable quickly.  This narrow gate closes the
    resulting stale-row window without turning every deployment into a
    whole-fleet serialized startup.  A missing or stopped container is marked
    terminal with a compare-and-set, so a concurrent resume cannot be clobbered.
    """
    sandbox = await get_sandbox(sandbox_id)
    if sandbox is None:
        return None
    status = getattr(sandbox.status, "value", sandbox.status)
    if status not in _LIVE_CONTAINER_STATUSES:
        return sandbox

    try:
        client = _get_docker_client()
        container = await asyncio.to_thread(
            client.containers.get, sandbox.container_id or sandbox_id
        )
        await asyncio.to_thread(container.reload)
        if container.status == "running":
            return sandbox
        if container.status in _TRANSITIONAL_DOCKER_STATUSES:
            # Docker has the box but has not finished its own startup.  The
            # fleet reconciler counts these as alive; treating them as gone
            # here would race startup and destroy a valid durable row.
            raise SandboxLivenessUnavailable(
                f"sandbox {sandbox_id} is still {container.status}"
            )
    except NotFound:
        pass
    except (APIError, DockerException) as exc:
        raise SandboxLivenessUnavailable(
            f"could not verify container liveness for sandbox {sandbox_id}"
        ) from exc

    store = _get_store()
    changed = await store.mark_stopped_if_active(
        sandbox_id, "container_missing_at_token_issuance"
    )
    if changed:
        logger.warning(
            "Sandbox %s container vanished during token issuance; marked STOPPED",
            sandbox_id,
        )
    return _backfill_proxy_url(await store.get(sandbox_id)) or sandbox


def _resolve_passthrough_keys() -> list[str]:
    """Union of names from settings.aidream_passthrough_env_file (parsed once,
    cached) and settings.aidream_passthrough_env (comma-separated). Values
    are read from os.environ at call time — this only resolves NAMES.

    Reading the file fresh each call would be wasted I/O; cache by mtime so
    if the operator updates aidream's .env and restarts, we pick it up.
    """
    keys: set[str] = set()
    explicit = (settings.aidream_passthrough_env or "").split(",")
    keys.update(k.strip() for k in explicit if k.strip())

    path = (settings.aidream_passthrough_env_file or "").strip()
    if path and os.path.isfile(path):
        keys.update(_parse_env_file_keys(path))
    return sorted(keys)


_env_file_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _parse_env_file_keys(path: str) -> frozenset[str]:
    """Return the set of env-var NAMES defined in a .env-style file. Cached
    by (path, mtime) so repeated calls don't re-read the file."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return frozenset()
    cached = _env_file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    keys: set[str] = set()
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Accept "FOO=bar", "export FOO=bar", "FOO =". Reject lines
                # without "=" (they aren't env-vars).
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, _ = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                # Defensive: skip lines that aren't valid env-var names so
                # garbage in the file can't poison the passthrough set.
                if key and all(c.isalnum() or c == "_" for c in key) and not key[0].isdigit():
                    keys.add(key)
    except OSError as exc:
        logger.warning("could not read aidream_passthrough_env_file %s: %s", path, exc)
        return frozenset()

    frozen = frozenset(keys)
    _env_file_cache[path] = (mtime, frozen)
    logger.info("loaded %d passthrough keys from %s", len(frozen), path)
    return frozen


# ── Platform-credential isolation ────────────────────────────────────────────
# Incident 2026-09-13 (docs/incidents/2026-09-13-platform-env-leak.md): the
# passthrough loop below used to run for EVERY template, so every user's
# ``slim`` box carried the platform's database URL, admin tokens, the bridge
# service token and every provider API key. Two layers now stand in the way:
#
#   1. The passthrough registry is consulted ONLY for templates in
#      :data:`PLATFORM_PASSTHROUGH_TEMPLATES` — the internal "run aidream
#      itself inside a box" dev case. Every other template's env is the
#      orchestrator-managed identity/storage vars set explicitly in
#      ``create_sandbox``, the caller's ``config.env`` and the user's vault
#      secrets. Nothing from the orchestrator's own process environment.
#   2. Even for those templates, the forward set is an ALLOWLIST
#      (:data:`PLATFORM_ENV_ALLOWLIST`) — see the ruling below. The operator
#      knob ``aidream_template_forwards_master_credentials`` (feature
#      ``infrastructure.sandbox``, default OFF, missing row = OFF) is the one
#      explicit, supervised way to widen that to the whole registry.
#
# 🚨 XT-10 (2026-09-18, feedback 34dcf28a, register ruling R14): layer 2 used to
# be a PATTERN DENYLIST, and a denylist is fail-open BY CONSTRUCTION. It missed
# 14 secret-shaped names because nobody had written a pattern for their exact
# spelling — ``.*_API_KEY.*`` does not match ``ANTHROPIC_KEY``,
# ``.*_SERVICE_TOKEN.*`` does not match ``MATRX_AGENT_TOKEN``, and a Supabase
# service-role key is spelled ``..._KEY``, not ``..._SECRET``. Measured on a
# real hosted box (sbx-7bc1060b325f): a live ``sk-ant-api03-…`` sat in the env
# of a container the person has a shell in, while ``denied_count=72`` — which
# counts only PATTERN HITS — made the box look protected. The forward set is
# now a named allowlist: anything not on it is dropped, COUNTED and NAMED, and
# the patterns survive only as a SECONDARY guard that REFUSES to boot a box if
# an allowlisted name ever looks like a secret. Never turn this back into "add
# another pattern": the next unnamed secret would leak the same way.
#
# The deny-list is about the orchestrator's OWN environment: a user's vault
# secret or ``config.env`` entry is theirs and is never filtered.

PLATFORM_PASSTHROUGH_TEMPLATES: frozenset[str] = frozenset({"aidream"})

#: Where the binding-time vault refresh publishes the person's CURRENT vault
#: values (orchestrator/vault_env_refresh.py owns the writing). Named here so
#: the exec wrapper does not import that module for one string.
#: Every env name the ORCHESTRATOR itself puts in a container (see the `env`
#: dict in ``create_sandbox``). The binding-time refresh must never clear one of
#: these while trying to clear a leaked platform credential: several of them
#: MATCH the master-credential patterns on purpose (MATRX_AIDREAM_SERVICE_TOKEN,
#: AWS_SECRET_ACCESS_KEY), and a hosted box loses its S3 sync without them.
#: ``create_sandbox`` asserts its own keys are a subset of this set, so the two
#: cannot drift (tests/test_github_credential_and_vault_env.py).
ORCHESTRATOR_MANAGED_ENV: frozenset[str] = frozenset({
    "SANDBOX_ID", "USER_ID", "ORGANIZATION_ID",
    "S3_BUCKET", "S3_REGION", "HOT_PATH", "COLD_PATH",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "MATRX_TIER", "MATRX_HOT_PREFIX", "MATRX_COLD_PREFIX",
    "SANDBOX_TEMPLATE", "SANDBOX_TEMPLATE_VERSION", "SANDBOX_MIGRATION",
    "MATRX_AGENT_TOKEN",
    "MATRX_AIDREAM_URL", "MATRX_AIDREAM_SERVICE_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION", "AWS_REGION",
    # The sandbox↔browser join (2026-09-20). Orchestrator-managed on purpose:
    # the pair is REFRESHED at every binding like the identity, because a
    # person can create, rename or delete their cloud browser long after this
    # box was born. Missing from this set, the binding-time leak sweep would
    # clear them out of every live box the first time it ran.
    "MATRX_BROWSER_PROFILE_ID", "MATRX_BROWSER_EXECUTION_TARGET",
})

#: Names the platform has RETIRED as git credentials (2026-09-18). They are not
#: master-credential shaped, so the pattern filter alone would leave them in a
#: box forever; the credential helper no longer reads them, and a name nothing
#: reads should not sit in a user's environment looking like a credential.
RETIRED_GIT_CREDENTIAL_NAMES: frozenset[str] = frozenset({
    "GH_TOKEN", "GITHUB_PAT", "MATRX_GITHUB_TOKEN",
})

VAULT_ENV_FILE = "/etc/matrx/vault-env.sh"
#: Where the box's identity lives (write-bridge-env.sh at boot on a current
#: image; republished at binding by vault_env_refresh for boxes older than that
#: script). Sourced BEFORE the vault file so a vault value the person happened
#: to name USER_ID still wins in their own shell.
BRIDGE_ENV_FILE = "/etc/matrx/bridge-env.sh"

MASTER_CREDENTIALS_KNOB = "aidream_template_forwards_master_credentials"

#: Name patterns (matched against the whole upper-cased name) that identify
#: a platform master credential. Broad on purpose — a false positive costs
#: one operator knob turn; a false negative is a leaked secret.
MASTER_CREDENTIAL_PATTERNS: tuple[str, ...] = (
    r".*PASSWORD.*",
    r".*_SECRET.*",
    r".*SECRET_.*",
    r".*DATABASE_URL.*",
    r".*CONNECTION_STRING.*",
    r".*_SERVICE_TOKEN.*",
    r"ADMIN_.*TOKEN.*",
    r".*_API_KEY.*",
    r".*_API_TOKEN.*",
    r".*_ACCESS_TOKEN.*",
    r".*_AUTH_TOKEN.*",
    r".*_ACCESS_KEY.*",
    r".*_PRIVATE_KEY.*",
    r".*CREDENTIALS.*",
    r".*_PAT$",
)
_MASTER_CREDENTIAL_RE = re.compile("|".join(f"(?:{p})" for p in MASTER_CREDENTIAL_PATTERNS))


def is_master_credential_name(name: str) -> bool:
    """True when an env-var NAME looks like a platform master credential."""
    return bool(_MASTER_CREDENTIAL_RE.fullmatch((name or "").upper()))


#: 🚨 THE FORWARD SET, FAIL-CLOSED. The ONLY names from the orchestrator's own
#: process environment that a passthrough-template container may receive with
#: the operator knob OFF (the default, and a missing knob row). Everything else
#: in the passthrough registry — all 128 names of aidream's `.env` — is dropped,
#: counted and named in the boot report.
#:
#: The credential-free hosted mode (XT-08b) needs exactly ONE platform name:
#: ``MATRX_PLATFORM_AUTH_JWKS_URL``, the PUBLIC JWKS document every browser
#: bundle fetches, which is how a box with no secret at all can still know who
#: is calling it (aidream/api/hosted_box_app.py JWKS_URL_ENV). Everything else
#: the in-box aidream needs is either baked into the image, set explicitly by
#: ``create_sandbox`` (identity, storage, the path-shape overrides), minted per
#: box, or deliberately absent — `configure_packages()` in a box is an allowlist
#: of 4 steps plus 19 NAMED skips precisely so the credentials can be missing.
#:
#: Adding a name here is a security decision, not a convenience: it must be
#: PUBLIC by design (a URL, a region, a log level), and the secondary pattern
#: guard below refuses to import this module if it is secret-shaped.
PLATFORM_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # The public JWKS document URL — the one thing a credential-free box needs.
    "MATRX_PLATFORM_AUTH_JWKS_URL",
    # Non-secret behaviour flags the in-box aidream reads.
    "MATRX_ENV", "LOG_LEVEL", "DEBUG",
    # Region strings (NOT credentials; the keys themselves are orchestrator-
    # managed and set explicitly for the hosted tier only).
    "AWS_REGION", "AWS_DEFAULT_REGION",
    # PATH-style basics. `create_sandbox` overrides TOOL_WORKSPACE_BASE for the
    # aidream template anyway; listed so the host's value is not reported as a
    # withheld secret every boot.
    "PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ", "TOOL_WORKSPACE_BASE",
})


def _assert_allowlist_holds_no_secret_shapes(
    names: "frozenset[str] | set[str]" = PLATFORM_ENV_ALLOWLIST,
) -> None:
    """The SECONDARY guard: the patterns no longer gate the registry, they
    police the allowlist. A secret-shaped name on the allowlist is a mistake
    that would hand a real credential to every box of a passthrough template,
    so it refuses LOUDLY — at import, and again on every forward decision."""
    offenders = sorted(n for n in names if is_master_credential_name(n))
    if offenders:
        raise RuntimeError(
            "PLATFORM_ENV_ALLOWLIST holds secret-shaped name(s): "
            f"{offenders}. The allowlist may only carry names that are PUBLIC "
            "by design. REMEDY: remove them, or (if the name is genuinely "
            "public despite its spelling) rename the platform variable — never "
            "loosen MASTER_CREDENTIAL_PATTERNS to make room for it."
        )


_assert_allowlist_holds_no_secret_shapes()


def aidream_template_path_overrides(sandbox_home: str = "/home/agent") -> dict[str, str]:
    """Path-shaped env the ORCHESTRATOR sets for the ``aidream`` template.

    /srv/projects/aidream/.env was authored on the maintainer's Mac and ships
    paths like ``BASE_DIR=/Users/armanisadeghi/code/aidream``, which do not
    exist inside a box. These are the corrected values, in ONE place because two
    readers need the same list: ``create_sandbox`` (which sets them) and the
    binding-time leak sweep (which must never clear them — they are registry
    names, so without this the sweep would unset ``BASE_DIR`` in every aidream
    box's shell and the in-box aidream would resolve the maintainer's Mac paths).
    """
    root = f"{sandbox_home}/aidream"
    return {
        "BASE_DIR": root,
        "TEMP_DIR": f"{root}/temp",
        "LOG_DIR": f"{root}/temp/logs",
        "MEDIA_BASE_DIR": f"{root}/temp/media",
        "SAMPLE_DATA_DIR": f"{root}/common/sample_data",
        # matrx-ai filesystem tools (fs_list, fs_read, fs_search, ...) gate every
        # path against TOOL_WORKSPACE_BASE. Aidream's .env points it at the
        # maintainer's local /Users/.../temp, which strands every tool call with
        # "Directory not found" / "Path escapes workspace". Inside the sandbox
        # the natural workspace IS the agent's home — the container itself is
        # the user/project boundary.
        "TOOL_WORKSPACE_BASE": sandbox_home,
        # The sniff-marker (/opt/aidream-template) is baked into the image; set
        # the explicit env signal too so tools never fall back to the
        # multi-tenant /tmp/workspaces layout even if the marker is moved.
        "MATRX_TOOLS_SANDBOX_MODE": "1",
        # Other Mac-shaped paths that aidream code reads at runtime.
        "ADMIN_PYTHON_ROOT": root,
        "ADMIN_TS_ROOT": root,
        "ADMIN_SAVE_DIRECT_ROOT": root,
        "MATRX_PYTHON_ROOT": root,
        "PYTHONPATH": root,
    }


def template_receives_platform_env(template: str | None) -> bool:
    return (template or "") in PLATFORM_PASSTHROUGH_TEMPLATES


@dataclass(frozen=True)
class PlatformEnvDecision:
    """What a passthrough-template box is given from the orchestrator's own
    environment, and — name by name — what it was NOT given and why.

    Nothing fails silently: every registry name that is present on the host and
    withheld appears in exactly one of the ``withheld_*`` lists, so the counts
    always add up against the registry. NAMES only, never values.
    """

    template: str
    forwarded: dict[str, str]
    withheld_not_allowlisted: tuple[str, ...]
    withheld_master_credential: tuple[str, ...]
    allowlist_widened_by_knob: bool

    @property
    def denied(self) -> list[str]:
        """Every name present on the host and withheld from the box."""
        return sorted(
            set(self.withheld_not_allowlisted) | set(self.withheld_master_credential)
        )

    def report(self) -> dict[str, Any]:
        """The boot report stamped on the sandbox row (names only)."""
        return {
            "forwarded": bool(self.template),
            "allowlist_widened_by_knob": self.allowlist_widened_by_knob,
            "forwarded_count": len(self.forwarded),
            "forwarded_names": sorted(self.forwarded),
            "withheld_not_allowlisted_count": len(self.withheld_not_allowlisted),
            "withheld_not_allowlisted_names": list(self.withheld_not_allowlisted),
            "withheld_master_credential_count": len(self.withheld_master_credential),
            "withheld_master_credential_names": list(self.withheld_master_credential),
            # Kept for the admin UI and /diagnostics, which read these two.
            "denied_count": len(self.denied),
            "denied_names": self.denied,
        }


def platform_env_decision(
    template: str | None,
    *,
    allow_master_credentials: bool,
    environ: "os._Environ[str] | dict[str, str] | None" = None,
) -> PlatformEnvDecision:
    """Decide, FAIL-CLOSED, what platform env a container of ``template`` gets.

    With the operator knob OFF (the default, and a missing knob row) the forward
    set is ``registry ∩ PLATFORM_ENV_ALLOWLIST``. Every other registry name
    present on this host is withheld and NAMED — that is the fix for feedback
    34dcf28a, where a pattern denylist forwarded 14 secret-shaped names nobody
    had written a pattern for.

    With the knob ON, an operator has explicitly asked for the whole registry
    for a short supervised session (the knob's own label says it hands over the
    platform's master credentials); the withheld lists then say so and the
    caller logs it at WARNING.

    Non-passthrough templates always get an empty decision: the registry is not
    even consulted, so a future edit to the registry cannot widen the blast
    radius by itself.
    """
    if not template_receives_platform_env(template):
        return PlatformEnvDecision("", {}, (), (), False)

    # The patterns' ONLY remaining job: police the allowlist. Re-checked here
    # (not just at import) so a test or a runtime patch cannot slip past it.
    _assert_allowlist_holds_no_secret_shapes()

    source = os.environ if environ is None else environ
    forwarded: dict[str, str] = {}
    not_allowlisted: list[str] = []
    master_credential: list[str] = []
    for key in _resolve_passthrough_keys():
        val = source.get(key)
        if not val:
            continue  # not set on this host — nothing to withhold
        if allow_master_credentials or key in PLATFORM_ENV_ALLOWLIST:
            forwarded[key] = val
            continue
        # Withheld. Which bucket is diagnostic only — both are dropped.
        if is_master_credential_name(key):
            master_credential.append(key)
        else:
            not_allowlisted.append(key)
    return PlatformEnvDecision(
        template=str(template or ""),
        forwarded=forwarded,
        withheld_not_allowlisted=tuple(sorted(not_allowlisted)),
        withheld_master_credential=tuple(sorted(master_credential)),
        allowlist_widened_by_knob=bool(allow_master_credentials),
    )


def platform_passthrough_env(
    template: str | None,
    *,
    allow_master_credentials: bool,
    environ: "os._Environ[str] | dict[str, str] | None" = None,
) -> tuple[dict[str, str], list[str]]:
    """``(forwarded, every withheld name)`` — the two-value view of
    :func:`platform_env_decision` that migrate/diagnostics call sites use."""
    decision = platform_env_decision(
        template,
        allow_master_credentials=allow_master_credentials,
        environ=environ,
    )
    return decision.forwarded, decision.denied


async def master_credentials_allowed() -> bool:
    """The operator knob, read through the fleet-settings store. Fails CLOSED
    (knobs.security_knob_bool): a missing row denies and screams the remedy —
    a guard that a missing setting could open is not a guard."""
    from orchestrator.knobs import security_knob_bool

    return await security_knob_bool(MASTER_CREDENTIALS_KNOB)


def agent_token_for(sandbox_id: str) -> str:
    """Per-sandbox shared secret the in-container daemon checks.

    Derived deterministically as HMAC(access_token_secret, "agent:<id>") so the
    orchestrator can recompute it for any sandbox without storing it, and the
    same value it injects into the container is the value it forwards on every
    proxied request. Returns "" when MATRX_ACCESS_TOKEN_SECRET is unset, which
    keeps the daemon fail-open (no enforcement) for local dev / pre-rollout.

    See sandbox-image/sdk/matrx_agent/api/_auth.py for the daemon side.
    """
    secret = settings.access_token_secret
    if not secret:
        return ""
    import hashlib
    import hmac as _hmac
    return _hmac.new(secret.encode(), f"agent:{sandbox_id}".encode(), hashlib.sha256).hexdigest()


def agent_forward_headers(sandbox_id: str) -> dict[str, str]:
    """Header dict to merge into a proxied request so the daemon accepts it.
    Empty when enforcement is off (no secret configured)."""
    tok = agent_token_for(sandbox_id)
    return {"X-Matrx-Agent-Token": tok} if tok else {}


def _proxy_url_for(sandbox_id: str) -> str | None:
    """Build the public ``proxy_url`` field surfaced on SandboxResponse.

    The browser hits ``{proxy_url}/<path>`` (e.g. ``/ai/agents/.../execute``)
    and the orchestrator forwards 1:1 to the in-container daemon. Returns
    None when MATRX_PUBLIC_URL is unset (the orchestrator doesn't know how
    it is reachable from the outside, so it can't build a URL).
    """
    base = (settings.public_url or "").rstrip("/")
    if not base:
        return None
    return f"{base}/sandboxes/{sandbox_id}/proxy"


_internal_base_cache: str | None = None  # resolved once; "" means "fall back to public"


def _imds_private_ipv4() -> str:
    """Best-effort EC2 private IPv4 via the Instance Metadata Service.

    Tries IMDSv2 (token) then IMDSv1. Returns "" on anything that isn't a
    reachable EC2 IMDS (e.g. the Hostinger hosted tier) within a tight timeout,
    so this never hangs a request on non-EC2 hosts.
    """
    import urllib.request
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(token_req, timeout=0.5).read().decode()
        headers = {"X-aws-ec2-metadata-token": token}
    except Exception:
        headers = {}
    try:
        ip_req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/local-ipv4", headers=headers
        )
        return urllib.request.urlopen(ip_req, timeout=0.5).read().decode().strip()
    except Exception:
        return ""


def resolve_internal_base() -> str:
    """Base URL the co-located AI Dream uses to reach this orchestrator for
    agent tool calls — kept on the private LAN so traffic is fast + free.

    Precedence: explicit ``MATRX_INTERNAL_URL`` > auto-detected EC2 private IP
    (``http://<private-ip>:<port>``) > ``MATRX_PUBLIC_URL``. Cached after the
    first resolve. Returning the EC2 private IP automatically means the
    operator does NOT have to set MATRX_INTERNAL_URL by hand.
    """
    global _internal_base_cache
    if settings.internal_url:
        return settings.internal_url.rstrip("/")
    if _internal_base_cache is None:
        ip = _imds_private_ipv4()
        _internal_base_cache = f"http://{ip}:{settings.port}" if ip else ""
        if _internal_base_cache:
            logger.info("Resolved internal base via EC2 metadata: %s", _internal_base_cache)
    return _internal_base_cache or (settings.public_url or "").rstrip("/")


def _resolve_ssh_host() -> str:
    """Public SSH host returned to clients in /access responses.

    Priority:
      1. Explicit ``MATRX_SSH_HOST`` env var (overrides; supports the case
         where SSH and HTTP exit through different hostnames).
      2. Hostname parsed out of ``MATRX_PUBLIC_URL``.
      3. Fallback to ``"localhost"`` — only useful for in-host testing;
         logs a loud warning when this fallback is hit.
    """
    if settings.ssh_host:
        return settings.ssh_host
    if settings.public_url:
        from urllib.parse import urlparse
        parsed = urlparse(settings.public_url)
        if parsed.hostname:
            return parsed.hostname
    logger.warning(
        "SSH host falling back to 'localhost' — set MATRX_SSH_HOST or MATRX_PUBLIC_URL "
        "so /sandboxes/{id}/access returns a hostname clients can actually reach."
    )
    return "localhost"


def _get_store() -> SandboxStore:
    """Get or create the sandbox store."""
    global _store
    if _store is None:
        _store = create_store()
    return _store


def _get_docker_client() -> docker.DockerClient:
    """Get or create the singleton Docker client.

    Returns a cached Docker client instance. The client is reused across all
    operations to avoid leaking TCP connections and file descriptors.

    Raises:
        RuntimeError: If unable to connect to the Docker daemon.
    """
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
        except DockerException as e:
            raise RuntimeError(f"Failed to connect to Docker daemon: {e}") from e
    return _docker_client


def close_docker_client() -> None:
    """Explicitly close the Docker client. Called during app shutdown."""
    global _docker_client
    if _docker_client is not None:
        _docker_client.close()
        _docker_client = None


async def close_store() -> None:
    """Close the sandbox store. Called during app shutdown."""
    global _store
    if _store is not None:
        await _store.close()
        _store = None


async def reserve_sandbox_admission(
    *, user_id: str, organization_id: str, name: str | None, config: dict | None,
    template: str | None, template_version: str | None, tier: str | None,
    labels: dict | None, ttl_seconds: int | None, replacement_for: str | None = None,
) -> SandboxResponse:
    """Persist only the manager-allocated creating identity before mutation."""
    sandbox_id = f"sbx-{uuid.uuid4().hex[:12]}"
    sandbox = SandboxResponse(
        sandbox_id=sandbox_id, user_id=user_id, organization_id=organization_id,
        name=name, status=SandboxStatus.CREATING, created_at=datetime.now(timezone.utc),
        config={**(config or {}), "organization_id": organization_id},
        ttl_seconds=ttl_seconds or 7200, tier=tier, template=template,
        template_version=template_version, labels=labels,
        proxy_url=_proxy_url_for(sandbox_id),
    )
    await _get_store().reserve_active(sandbox, replacement_for=replacement_for)
    return sandbox


@asynccontextmanager
async def reset_successor_admission(
    predecessor: SandboxResponse,
    *, name: str | None, config: dict | None, template: str | None,
    template_version: str | None, tier: str | None, labels: dict | None,
    ttl_seconds: int | None,
):
    """Own one reset successor from reservation through runtime creation.

    The lifecycle-home lock is acquired before the creating row exists and is
    retained while the predecessor is destroyed, its home is optionally
    wiped, and the successor is created. Reconciliation therefore cannot
    prove the reserved SID absent in the destructive gap. All nested lifecycle
    calls must use their explicit already-leased path.
    """
    from orchestrator.home_identity import home_key
    from orchestrator.hosted_operation_lease import hosted_operation_lease

    successor = SandboxResponse(
        sandbox_id=f"sbx-{uuid.uuid4().hex[:12]}",
        user_id=predecessor.user_id,
        organization_id=predecessor.organization_id,
        name=name,
        status=SandboxStatus.CREATING,
        created_at=datetime.now(timezone.utc),
        config={**(config or {}), "organization_id": predecessor.organization_id},
        ttl_seconds=ttl_seconds or 7200,
        tier=tier,
        template=template,
        template_version=template_version,
        labels=labels,
        proxy_url=None,
    )
    successor.proxy_url = _proxy_url_for(successor.sandbox_id)
    reset_home = home_key(predecessor)
    async with hosted_operation_lease(successor.sandbox_id, reset_home, lifecycle=True):
        await _get_store().reserve_active(
            successor, replacement_for=predecessor.sandbox_id,
        )
        yield successor


async def create_sandbox(
    user_id: str,
    organization_id: str,
    name: str | None = None,
    config: dict | None = None,
    template: str | None = None,
    template_version: str | None = None,
    tier: str | None = None,
    resources: dict | None = None,
    labels: dict | None = None,
    ttl_seconds: int | None = None,
    persistence_from: str | None = None,
    replacement_for: str | None = None,
    reserved: SandboxResponse | None = None,
    _lifecycle_lease_held: bool = False,
) -> SandboxResponse:
    """Create under the hosted home lease before the first durable write."""
    config = config or {}
    config_organization_id = config.get("organization_id")
    if config_organization_id is not None and config_organization_id != organization_id:
        raise ValueError(
            "config.organization_id must match the explicit organization_id"
        )
    location = resolve_user_storage(user_id, tier, organization_id)
    sandbox_id = reserved.sandbox_id if reserved is not None else f"sbx-{uuid.uuid4().hex[:12]}"
    if reserved is not None and (reserved.user_id != user_id or reserved.organization_id != organization_id):
        raise RuntimeError("reserved sandbox identity does not match the create request")
    persistence_reference: str | None = None
    if persistence_from is not None:
        if template == "development":
            raise RuntimeError("development workspaces cannot adopt a named EC2 home")
        prior = await _get_store().get(persistence_from)
        if not prior or prior.user_id != user_id or prior.organization_id != organization_id:
            raise RuntimeError("prior EC2 persistence row is missing or belongs to another user or organization")
        prior_tier = getattr(prior.tier, "value", prior.tier)
        if location.tier != "ec2" or prior_tier != "ec2":
            raise RuntimeError("prior persistence may only be reused by the matching EC2 sandbox")
        reference = getattr(prior, "persistence_volume", None)
        if not reference or reference.startswith("host:"):
            raise RuntimeError("prior EC2 sandbox has no reusable named durable home")
        persistence_reference = await asyncio.to_thread(
            validate_ec2_home_volume, _get_docker_client(), reference, prior
        )
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    if persistence_reference:
        home_key = persistence_reference
    elif location.tier == "hosted":
        home_key = user_volume_name(user_id, organization_id)
    elif template == "development":
        # Keep development binds in the same canonical lock namespace, using
        # the hashed bind identity rather than a path-shaped lock key.
        from orchestrator.home_identity import home_key as canonical_home_key
        workspace_key = str(config.get("workspace_key") or "primary")
        home_key = canonical_home_key(SimpleNamespace(
            persistence_volume=f"host:{workspace_key}", sandbox_id=sandbox_id, tier="ec2", user_id=user_id
        ))
    else:
        home_key = ec2_home_volume_name(sandbox_id)
    async def create_under_lease() -> SandboxResponse:
        if persistence_reference:
            prior = await _get_store().get(persistence_from)
            life = await _get_store().get_lifecycle(persistence_from)
            if (not prior or not life or life.get("deleted")
                    or prior.user_id != user_id or prior.organization_id != organization_id
                    or prior.persistence_volume != persistence_reference
                    or getattr(prior.status, "value", prior.status) not in {"stopped", "expired", "failed"}):
                raise RuntimeError("prior home routing is not a retained terminal workspace")
            await asyncio.to_thread(validate_ec2_home_volume, _get_docker_client(), persistence_reference, prior)
            attached = await asyncio.to_thread(_get_docker_client().containers.list, all=True,
                                               filters={"volume": persistence_reference})
            if attached:
                raise RuntimeError("retained EC2 home is already attached; refusing concurrent reuse")
        return await _create_sandbox_unleased(
            sandbox_id, user_id, organization_id, name, config, template,
            template_version, tier, resources, labels, ttl_seconds, persistence_reference,
            replacement_for, reserved,
        )
    if _lifecycle_lease_held:
        return await create_under_lease()
    async with hosted_operation_lease(sandbox_id, home_key, lifecycle=True):
        return await create_under_lease()


async def _create_sandbox_unleased(
    sandbox_id: str,
    user_id: str,
    organization_id: str,
    name: str | None = None,
    config: dict | None = None,
    template: str | None = None,
    template_version: str | None = None,
    tier: str | None = None,
    resources: dict | None = None,
    labels: dict | None = None,
    ttl_seconds: int | None = None,
    persistence_reference: str | None = None,
    replacement_for: str | None = None,
    reserved: SandboxResponse | None = None,
) -> SandboxResponse:
    """Create and start a new sandbox container for a user.

    ``template``/``template_version``/``tier``/``labels`` are recorded on the
    sandbox row for routing + observability. ``resources`` overrides the per-
    container CPU/memory limits when supplied (hosted tier only — EC2 tier ignores
    overrides for now). ``ttl_seconds`` overrides the default TTL for this sandbox.
    """
    store = _get_store()
    from orchestrator.hosted_migration import hosted_volume_fenced
    if (tier or settings.host_tier) == "hosted" and hosted_volume_fenced(
        user_volume_name(user_id, organization_id)
    ):
        raise RuntimeError("hosted user home is fenced by an in-progress migration/recovery")
    config["organization_id"] = organization_id
    resources = resources or {}

    sandbox = reserved or SandboxResponse(
        sandbox_id=sandbox_id, user_id=user_id, organization_id=organization_id,
        name=name, status=SandboxStatus.CREATING, created_at=datetime.now(timezone.utc),
        config=config, ttl_seconds=ttl_seconds or 7200, tier=tier, template=template,
        template_version=template_version, labels=labels, proxy_url=_proxy_url_for(sandbox_id),
    )
    # Capacity is a durable admission, not a best-effort count in a route.
    # This INSERT is deliberately before Docker volumes, storage hydration,
    # vault fetches, or a container create.  A manager-generated sandbox id
    # makes a retry after an ambiguous commit idempotent.
    if reserved is None:
        await store.reserve_active(sandbox, replacement_for=replacement_for)

    logger.info(
        "Creating sandbox %s for user %s (tier=%s, template=%s)",
        sandbox_id, user_id, tier, template,
    )

    try:
        client = _get_docker_client()

        # ── Resolve persistence location for this (user, tier) pair ───────────
        location: StorageLocation = resolve_user_storage(user_id, tier, organization_id)
        volumes: dict[str, dict] = {}
        reusing_home = bool(persistence_reference)
        if location.tier == "hosted":
            # Hosted tier: per-user Docker volume mounted at /home/agent.
            # Volume survives container destruction; subsequent sandboxes for
            # the same user see the same home dir.
            try:
                await asyncio.to_thread(
                    client.volumes.get, user_volume_name(user_id, organization_id)
                )
                reusing_home = True
            except NotFound:
                pass
            # A failed inheritance copy raises here and REFUSES the create —
            # a box never starts on an empty or half-copied home.
            volume_name = await asyncio.to_thread(
                ensure_user_volume, client, user_id, organization_id
            )
            if not reusing_home:
                seeded_from = await asyncio.to_thread(
                    inherited_from, client, volume_name
                )
                if seeded_from:
                    # A home seeded from the user's pre-organization volume is a
                    # RETAINED home: it already holds their projects, session
                    # manifest and sync queue, so nothing may hydrate over it.
                    reusing_home = True
                    logger.warning(
                        "Hosted-tier sandbox %s: home %s was seeded from the "
                        "pre-organization volume %s",
                        sandbox_id, volume_name, seeded_from,
                    )
            volumes[volume_name] = {"bind": "/home/agent", "mode": "rw"}
            sandbox.persistence_volume = volume_name
            logger.info(
                "Hosted-tier sandbox %s: mounting user volume %s -> /home/agent",
                sandbox_id, volume_name,
            )
        elif location.tier == "ec2" and template != "development":
            volume_name = persistence_reference or await asyncio.to_thread(
                ensure_ec2_home_volume, client, sandbox_id, user_id, organization_id
            )
            volumes[volume_name] = {"bind": "/home/agent", "mode": "rw"}
            sandbox.persistence_volume = volume_name
            logger.info(
                "EC2 sandbox %s: mounting durable per-sandbox home %s -> /home/agent",
                sandbox_id, volume_name,
            )
        elif template == "development":
            workspace_root = Path(settings.internal_development_workspace_root).resolve()
            workspace_key = str(config.get("workspace_key") or "primary")
            workspace_path = (workspace_root / workspace_key).resolve()
            if workspace_path.parent != workspace_root:
                raise RuntimeError("internal development workspace escapes configured root")
            reusing_home = workspace_path.exists()
            workspace_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(workspace_path, 1000, 1000)
            volumes[str(workspace_path)] = {"bind": "/home/agent", "mode": "rw"}
            sandbox.persistence_volume = f"host:{workspace_key}"
            logger.info(
                "Internal development sandbox %s: mounting workspace %s -> /home/agent",
                sandbox_id,
                workspace_key,
            )

        env = {
            "SANDBOX_ID": sandbox_id,
            "USER_ID": user_id,
            "S3_BUCKET": location.s3_bucket or "",
            "S3_REGION": config.get("s3_region", settings.s3_region),
            "HOT_PATH": "/home/agent",
            "COLD_PATH": "/data/cold",
            "SHUTDOWN_TIMEOUT_SECONDS": str(await knob_int("shutdown_timeout_seconds")),
            # Tier hint — the in-container persistence module reads this to
            # decide whether to also push to S3 (Phase 1.5). When empty / "ec2"
            # the existing hot-sync.sh + cold-mount.sh handle S3 directly.
            "MATRX_TIER": location.tier,
            "MATRX_HOT_PREFIX": location.s3_hot_prefix or "",
            "MATRX_COLD_PREFIX": location.s3_cold_prefix or "",
        }
        env["ORGANIZATION_ID"] = organization_id
        if template:
            env["SANDBOX_TEMPLATE"] = template
        if template_version:
            env["SANDBOX_TEMPLATE_VERSION"] = template_version

        # Per-sandbox daemon secret. Fail-open: only set when an access-token
        # secret is configured; the daemon only enforces when it receives this.
        _agent_tok = agent_token_for(sandbox_id)
        if _agent_tok:
            env["MATRX_AGENT_TOKEN"] = _agent_tok

        # ── AI Dream integration env ──────────────────────────────────────────
        # When the orchestrator has a service token, hand it to the sandbox
        # along with the user_id. The in-container ``mtx`` CLI uses this to
        # call AI Dream's cld_files endpoints on behalf of the user.
        if settings.aidream_url:
            env["MATRX_AIDREAM_URL"] = settings.aidream_url
        if settings.aidream_service_token:
            env["MATRX_AIDREAM_SERVICE_TOKEN"] = settings.aidream_service_token

        # ── AWS creds for hosted-tier S3 sync ────────────────────────────────
        # On EC2 tier the orchestrator has an instance role and skips this
        # block — sandboxes inherit creds via the role. On hosted tier we
        # have to pass explicit creds (orchestrator reads them from its own
        # env via ``settings.aws_access_key_id`` / ``aws_secret_access_key``).
        if location.tier == "hosted" and settings.aws_access_key_id and settings.aws_secret_access_key:
            env["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
            # Pass BOTH region env-var conventions. boto3 honors either, but
            # downstream code may explicitly check just one — aidream's
            # common/aws/aws_client.py reads AWS_REGION specifically.
            region = config.get("s3_region", settings.s3_region)
            env["AWS_DEFAULT_REGION"] = region
            env["AWS_REGION"] = region

        # ── The sandbox↔browser join ─────────────────────────────────────────
        # The box drives the person's OWN persistent cloud browser — the one
        # that already survives teardown — rather than launching a second,
        # amnesiac Chromium of its own. Two names carry that: which browser,
        # and where it runs. Resolved through AI Dream's door, never by reading
        # browser.profile from here (orchestrator/browser_profile.py says why).
        #
        # NOTHING FAILS SILENTLY, AND NOTHING IS INVENTED: a person with no
        # cloud browser gets NEITHER name, the in-box client refuses with a
        # sentence telling them how to get one, and the reason is stamped on
        # the row so the screen can say why instead of showing a dead control.
        browser_lookup = await resolve_browser_profile(
            user_id=user_id, organization_id=organization_id
        )
        env.update(browser_lookup.env())
        if browser_lookup.present:
            logger.info(
                "Sandbox %s is joined to cloud browser %s (%s)",
                sandbox_id, browser_lookup.profile_id, browser_lookup.label or "unnamed",
            )
        else:
            logger.info(
                "Sandbox %s gets no browser names: %s",
                sandbox_id, browser_lookup.reason,
            )

        # The refresh's protected set and the dict above are ONE contract: a
        # name the orchestrator manages but that is missing from
        # ORCHESTRATOR_MANAGED_ENV would be CLEARED out of every live box by the
        # binding-time leak sweep (vault_env_refresh.leaked_platform_names).
        # Checked HERE — after the orchestrator's own block and before the
        # caller's config.env, the vault and the passthrough merge in, so this
        # sees exactly the set it is about.
        _unregistered = sorted(set(env) - ORCHESTRATOR_MANAGED_ENV)
        if _unregistered:
            raise RuntimeError(
                f"orchestrator-managed env names missing from "
                f"ORCHESTRATOR_MANAGED_ENV: {_unregistered}. Add them there, or the "
                f"binding-time leak sweep will clear them out of live boxes."
            )

        # ── aidream-in-sandbox env passthrough (aidream template ONLY) ────────
        # Forward env vars named in EITHER (a) the file at
        # settings.aidream_passthrough_env_file (e.g. /srv/projects/aidream/.env)
        # or (b) the explicit settings.aidream_passthrough_env list, from the
        # orchestrator's process environment — but only into a box of a
        # template in PLATFORM_PASSTHROUGH_TEMPLATES, and only a name on the
        # fail-closed PLATFORM_ENV_ALLOWLIST unless the operator knob is on.
        # Every other template gets NOTHING from the orchestrator's environ
        # (incident 2026-09-13: this loop used to run for every template).
        decision = platform_env_decision(
            template, allow_master_credentials=await master_credentials_allowed(),
        )
        for key, val in decision.forwarded.items():
            if key not in env:  # don't clobber already-set vars
                env[key] = val
        # THE BOOT REPORT — nothing fails silently. Every host-set registry name
        # this box did NOT get is named here, not just the ones a pattern
        # happened to catch (feedback 34dcf28a: `denied_count=72` counted
        # pattern hits while 14 real secrets sailed through).
        if decision.withheld_not_allowlisted or decision.withheld_master_credential:
            logger.warning(
                "sandbox %s (template=%s): platform env withheld — %d name(s) not on "
                "PLATFORM_ENV_ALLOWLIST: %s | %d master-credential-shaped name(s): %s "
                "| forwarded %d: %s. REMEDY if the box needs one of these: add it to "
                "PLATFORM_ENV_ALLOWLIST only if it is PUBLIC by design, or turn the "
                "knob %s on for a short supervised session.",
                sandbox_id, template,
                len(decision.withheld_not_allowlisted),
                ", ".join(decision.withheld_not_allowlisted) or "-",
                len(decision.withheld_master_credential),
                ", ".join(decision.withheld_master_credential) or "-",
                len(decision.forwarded), ", ".join(sorted(decision.forwarded)) or "-",
                MASTER_CREDENTIALS_KNOB,
            )
        if decision.allowlist_widened_by_knob and decision.forwarded:
            logger.warning(
                "sandbox %s (template=%s): knob %s is ON — the WHOLE platform "
                "passthrough registry was handed to this container, including "
                "%d master-credential-shaped name(s). Turn it off and recreate "
                "the box when the supervised session ends.",
                sandbox_id, template, MASTER_CREDENTIALS_KNOB,
                sum(1 for k in decision.forwarded if is_master_credential_name(k)),
            )
        # Stamp the outcome on the persisted row (names only, never values)
        # so /diagnostics and the admin UI can show what this box carries.
        platform_env_diag = decision.report()
        if isinstance(config, dict):
            config["platform_env"] = platform_env_diag
        if isinstance(sandbox.config, dict):
            sandbox.config["platform_env"] = platform_env_diag

        # ── Path-shape overrides ──────────────────────────────────────────────
        # /srv/projects/aidream/.env was authored on the maintainer's Mac and
        # ships paths like BASE_DIR=/Users/armanisadeghi/code/aidream. Those
        # paths don't exist inside the sandbox (aidream is at /home/agent/aidream).
        # Override the path-shaped vars so matrx-utils settings + file-handling
        # code resolves real on-disk locations. These are ORCHESTRATOR-set for
        # this template (the one named source is
        # ``aidream_template_path_overrides`` below, which the binding-time leak
        # sweep also reads so it can never unset one of them).
        if (template or "") == "aidream":
            env.update(aidream_template_path_overrides())
            # GOOGLE_APPLICATION_CREDENTIALS points at a Mac filesystem path
            # that doesn't exist here — having it set causes google-auth to
            # try to open a non-existent file. Drop it so the Google SDKs
            # gracefully fall back to other auth signals (or stay unconfigured).
            env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

        # ── User-secrets vault injection (orchestrator-side, all paths) ──
        # Fetch the user's vaulted secrets from aidream by user_id, using
        # the existing sandbox-service token (same token cloud-files bridge
        # uses). Putting this in the orchestrator means EVERY create-path
        # gets secrets — matrx-frontend "New sandbox" button, Matrx Ship
        # admin portal, Chrome extension, agent automation. The old
        # Next.js-route-side fetch had a class-of-failures bug (server
        # `getSession()` returns falsy access_token under @supabase/ssr).
        # This path doesn't have that gate.
        #
        # Safe no-op: when aidream_url/aidream_service_token are unset OR
        # the call fails, secrets simply aren't injected — sandbox creation
        # never blocks on the secrets fetch.
        #
        # Diagnostic capture: every outcome is stamped on
        # config["secrets_injection"] (and therefore on the persisted
        # sandbox_instances.config row) so the UI can show exactly what
        # happened — attempted? skipped (why)? fetched (how many)? errored
        # (what message)? Without this stamp, the only signal was "did the
        # env land", which is non-diagnostic when it didn't.
        # Resolve URL + token with passthrough-file fallback (hosted tier
        # reads them from /srv/projects/aidream/.env automatically; EC2 needs
        # the explicit MATRX_ env vars).
        resolved_aidream_url = settings.resolve_aidream_url()
        resolved_aidream_token = settings.resolve_aidream_service_token()
        secrets_env: dict[str, str] = {}
        diag: dict[str, Any] = {
            "attempted": False,
            "skipped_reason": None,
            "fetched_count": 0,
            "fetched_at": None,
            "status_code": None,
            "error": None,
            "aidream_url_set": bool(resolved_aidream_url),
            "aidream_service_token_set": bool(resolved_aidream_token),
            "aidream_url_used": resolved_aidream_url or None,
            "user_id_set": bool(user_id),
            "organization_id_set": bool(organization_id),
        }
        if not user_id:
            diag["skipped_reason"] = "user_id is empty on the create request"
        elif not resolved_aidream_url:
            diag["skipped_reason"] = (
                "aidream URL unavailable — set MATRX_AIDREAM_URL on this "
                "orchestrator (hosted tier reads it from "
                "/srv/projects/aidream/.env automatically; EC2 needs it set "
                "explicitly)"
            )
        elif not resolved_aidream_token:
            diag["skipped_reason"] = (
                "aidream service token unavailable — set "
                "MATRX_AIDREAM_SERVICE_TOKEN (= aidream's "
                "AIDREAM_SANDBOX_SERVICE_TOKEN) on this orchestrator. Hosted "
                "tier reads it from /srv/projects/aidream/.env automatically; "
                "EC2 needs it set explicitly in the systemd env."
            )
        else:
            diag["attempted"] = True
            try:
                import httpx
                # datetime/timezone come from the MODULE-level import (line 15).
                # A local `from datetime import ...` here made `datetime` a
                # function-local, so the earlier `created_at=datetime.now(...)`
                # (~line 263) hit UnboundLocalError → every create + the
                # test_create_sandbox unit test failed → the whole Deploy
                # pipeline was blocked (test gates deploy). Do NOT re-import.

                # NOTE: must NOT be named `client` — that's the docker client
                # (line ~279, used later for client.containers.run). Shadowing
                # it here left a closed httpx.AsyncClient in `client`, so every
                # create died with "'AsyncClient' object has no attribute
                # 'containers'". Use a distinct name.
                async with httpx.AsyncClient(timeout=10.0) as hx:
                    resp = await hx.get(
                        f"{resolved_aidream_url.rstrip('/')}/api/user-secrets/internal/sandbox-env-for-user",
                        params={"organization_id": organization_id},
                        # ONE builder, never a hand-written pair: aidream's
                        # AuthMiddleware refuses every authenticated request
                        # that names no organization (400
                        # organization_required) before it routes — the gate
                        # reads the HEADER, not the query param above (kept
                        # for the route's own use) — and the builder refuses
                        # to omit it. Mirror of the image's one builder; the
                        # two are held together by
                        # tests/test_bridge_header_parity.py.
                        headers=identity_headers(
                            token=resolved_aidream_token,
                            user_id=str(user_id),
                            organization_id=str(organization_id),
                            extra={"User-Agent": "matrx-sandbox-orchestrator"},
                        ),
                    )
                diag["status_code"] = resp.status_code
                diag["fetched_at"] = datetime.now(timezone.utc).isoformat()
                if resp.status_code == 200:
                    body = resp.json() or {}
                    fetched = body.get("env") if isinstance(body, dict) else None
                    if isinstance(fetched, dict):
                        for k, v in fetched.items():
                            if isinstance(k, str) and isinstance(v, str):
                                secrets_env[k] = v
                    diag["fetched_count"] = len(secrets_env)
                    # NAMES only, never values. This is the baseline the
                    # binding-time vault refresh diffs against the first time it
                    # runs on this box: without it, it can say what was ADDED
                    # but not what the person has since DELETED.
                    diag["names"] = sorted(secrets_env)
                    logger.info(
                        "user-secrets: injected %d secret(s) for user=%s org=%s",
                        len(secrets_env), user_id, organization_id,
                    )
                else:
                    body_text = (resp.text or "")[:300]
                    diag["error"] = (
                        f"HTTP {resp.status_code} from aidream: {body_text}"
                    )
                    logger.warning(
                        "user-secrets: aidream returned HTTP %s; sandbox will boot "
                        "without auto-injected secrets. body=%s",
                        resp.status_code, body_text,
                    )
            except Exception as exc:  # network / DNS / TLS / parsing
                diag["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "user-secrets: fetch failed (%s); sandbox will boot without "
                    "auto-injected secrets",
                    exc,
                )

        # Persist the diagnostic on the sandbox row so the UI can show the
        # status without re-querying the orchestrator. Mutate BOTH `config`
        # (local var still used for the dispatch + the later docker env
        # merge) AND `sandbox.config` (the field that gets serialized into
        # the DB row). Pydantic v2 keeps the ref for dict fields, but being
        # explicit means future pydantic changes can't silently drop this.
        browser_diag = browser_lookup.diagnostic()
        if isinstance(config, dict):
            config["secrets_injection"] = diag
            config["browser_profile"] = browser_diag
        if isinstance(sandbox.config, dict):
            sandbox.config["secrets_injection"] = diag
            sandbox.config["browser_profile"] = browser_diag
        else:
            sandbox.config = {
                "secrets_injection": diag,
                "browser_profile": browser_diag,
            }

        # Merge per-sandbox env from THREE sources, last-wins:
        #   1. orchestrator-wide passthrough (already in `env` above)
        #   2. caller-supplied `config.env` (e.g. matrx-frontend sandbox-prefs)
        #   3. user-secrets vault (fetched above)
        # The vault wins because the user spec is "set once, never lose it"
        # — a stale `config.env` value can't shadow a rotated vault value.
        config_env = config.get("env") if isinstance(config, dict) else None
        if config_env is not None and not isinstance(config_env, dict):
            # Don't silently drop bogus shapes — that's the failure mode
            # this whole merge was added to fix. Log loudly so a future
            # client sending the wrong shape gets seen in the deploy log.
            logger.warning(
                "config.env present but not a dict (type=%s); ignoring",
                type(config_env).__name__,
            )
        elif isinstance(config_env, dict):
            skipped: list[str] = []
            for k, v in config_env.items():
                if not isinstance(k, str):
                    skipped.append(repr(k))
                    continue
                # Exclude bools first (bool is a subclass of int — without
                # this check `True` becomes `"True"`, surprising callers).
                if isinstance(v, bool) or v is None:
                    skipped.append(k)
                    continue
                if not isinstance(v, (str, int, float)):
                    skipped.append(k)
                    continue
                env[k] = str(v)
            if skipped:
                logger.warning(
                    "config.env had %d unusable entries skipped: %s",
                    len(skipped), ", ".join(skipped[:10]),
                )

        # Step 3: vault overrides — applied AFTER caller's config.env so a
        # rotated vault value can't be shadowed by a stale caller-supplied
        # entry. Same shape filtering as above (str/int/float, no bools).
        for k, v in secrets_env.items():
            env[k] = v
        if reusing_home:
            # Storage identity is server-owned. Caller env/secrets may not
            # re-enable hydration over an already retained home.
            env["SANDBOX_MIGRATION"] = "1"

        if template == "development":
            # The internal development box clones private repos at boot, so it
            # must be born able to reach GitHub. Until 2026-09-18 this accepted
            # GH_TOKEN / GITHUB_PAT / MATRX_GITHUB_TOKEN as well — names that
            # could be satisfied by the ORCHESTRATOR HOST's own master PAT
            # rather than by the person's credential, which is how a box came
            # to hold a revoked platform token for a month. The gate now names
            # the ONE vault key the credential helper actually reads, and says
            # what to do when it is missing.
            if not env.get("GITHUB_TOKEN"):
                raise RuntimeError(
                    "internal development sandbox requires a GITHUB_TOKEN vault "
                    "item marked 'inject into sandbox' for the user this box "
                    "belongs to. GH_TOKEN, GITHUB_PAT and MATRX_GITHUB_TOKEN are "
                    "no longer accepted: they named the platform's own token, not "
                    "this person's."
                )

        # Resource overrides — fall back to the fleet settings
        # (infrastructure.sandbox knobs; MATRX_CONTAINER_* env until 2026-09-11)
        cpu_limit = resources.get("cpu") or await knob_float("container_cpu_limit")
        memory_limit = resources.get("memory_mb")
        memory_limit = f"{memory_limit}m" if memory_limit else await knob_str("container_memory_limit")

        # Per-template image override. Most templates share the bare image
        # (they differ via env vars / init scripts), but variants like
        # 'aidream' need a heavier purpose-built image.
        from orchestrator.routes.templates import resolve_template_image
        image_for_template = resolve_template_image(template) or settings.sandbox_image

        # Stamp the box with the ACTUAL baked version of the image it's born on
        # (not whatever the caller passed) so drift detection compares like for
        # like. Falls back to the caller's value when the image is unstamped.
        try:
            from orchestrator.versioning import current_image
            _born_version = (await asyncio.to_thread(current_image, client, template)).version
            if _born_version:
                template_version = _born_version
                sandbox.template_version = _born_version
        except Exception as exc:
            logger.debug("could not read baked image version for %s: %s", image_for_template, exc)

        container = await asyncio.to_thread(lambda: client.containers.run(
            image=image_for_template,
            name=sandbox_id,
            detach=True,
            environment=env,
            volumes=volumes or None,
            **container_runtime_isolation(template, location.tier),
            # Resource constraints
            cpu_period=100000,
            cpu_quota=int(cpu_limit * 100000),
            mem_limit=memory_limit,
            # FUSE requires SYS_ADMIN capability and /dev/fuse access
            # Expose SSH (port 22) on a dynamic host port
            ports={"22/tcp": None},
            # Networking
            network=settings.docker_network,
            # Extra hosts so container can reach the orchestrator
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": user_id,
                "matrx.organization_id": organization_id,
                **({"matrx.name": name} if name else {}),
                "matrx.created_at": sandbox.created_at.isoformat(),
                # Labels carry the *minimum* state needed for boot-time
                # reconcile to rebuild a SandboxResponse without help from
                # the store. If you add a new field to SandboxResponse that
                # the agent or FE depends on, add it here too.
                **({"matrx.tier": tier} if tier else {}),
                **({"matrx.template": template} if template else {}),
                **({"matrx.template_version": template_version} if template_version else {}),
                **{f"matrx.label.{k}": v for k, v in (labels or {}).items()},
            },
            restart_policy={
                "Name": "unless-stopped" if template == "development" else "no",
                "MaximumRetryCount": 0,
            },
        ))  # type: ignore[call-overload]

        sandbox.container_id = container.id
        sandbox.status = SandboxStatus.STARTING

        # Read back the dynamically assigned SSH host port
        await asyncio.to_thread(container.reload)
        port_bindings = container.attrs["NetworkSettings"]["Ports"].get("22/tcp")
        if port_bindings:
            sandbox.ssh_port = int(port_bindings[0]["HostPort"])

        await store.save(sandbox)

        sandbox = await _wait_for_ready(sandbox)
        await store.save(sandbox)

        # Hydrate the user's central memory into .matrx/memory/ so a fresh box
        # already knows the user/projects/preferences. Best-effort: a memory
        # failure must never fail the create. Only when the box actually came up.
        if sandbox.status == SandboxStatus.READY and not reusing_home:
            try:
                from orchestrator.memory_sync import hydrate_memory_into_container
                await hydrate_memory_into_container(container, user_id, store)
            except Exception as exc:
                logger.warning("Memory hydrate skipped for %s: %s", sandbox_id, exc)

        logger.info("Sandbox %s is %s for user %s", sandbox_id, sandbox.status, user_id)
        return sandbox

    except Exception as e:
        sandbox.status = SandboxStatus.FAILED
        await store.save(sandbox)
        logger.error("Failed to create sandbox %s: %s", sandbox_id, e)
        raise RuntimeError(f"Failed to create sandbox {sandbox_id}: {e}") from e


async def _wait_for_ready(
    sandbox: SandboxResponse,
    *,
    store: "SandboxStore | None" = None,
    poll_interval: float = 2.0,
) -> SandboxResponse:
    """Wait on the box's own boot PHASE — never on a fixed wall clock.

    See :mod:`orchestrator.boot_readiness` for the incident this replaces: a
    hardcoded 120s killed every EC2 create whose S3 home sync ran longer than
    two minutes, including boxes whose entrypoints went on to log
    ``Sandbox is READY`` minutes later.

    The budgets are operator knobs, and they are *liveness* budgets: the clock
    resets whenever the box changes phase or copies another file, so a big
    home is never a reason to die while a wedged one still is. The box is
    handed over as soon as its SDK answers, even mid home-sync; the sync's
    progress rides along on the row (``sandbox.boot``) so the caller can say
    "home sync in progress: N/M files" instead of the box disappearing.
    """
    from orchestrator import boot_readiness as br
    from orchestrator.knobs import knob_int

    client = _get_docker_client()
    store = store or _get_store()

    ready_budget = float(await knob_int("ready_timeout_seconds"))
    home_sync_budget = float(await knob_int("home_sync_timeout_seconds"))

    started = time.monotonic()
    phase_started = started
    previous: br.BootSnapshot | None = None
    last_published: str | None = None

    async def publish(snapshot: br.BootSnapshot) -> None:
        """Surface the live phase on the row so a person can watch it."""
        nonlocal last_published
        sandbox.boot = SandboxBoot(
            phase=snapshot.budget_phase,
            files_done=snapshot.files_done,
            files_total=snapshot.files_total,
            briefing=snapshot.briefing(),
            updated_at=datetime.now(timezone.utc),
        )
        signature = f"{snapshot.budget_phase}:{snapshot.files_done}"
        if signature != last_published:
            last_published = signature
            try:
                await store.save(sandbox)
            except Exception as exc:  # never let a status write fail a create
                logger.warning(
                    "Could not publish boot phase for %s: %s", sandbox.sandbox_id, exc
                )

    while True:
        try:
            container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
            if container.status == "exited":
                sandbox.status = SandboxStatus.FAILED
                sandbox.stop_reason = br.timeout_reason(
                    previous, 0.0, time.monotonic() - started
                ).replace("stalled in", "exited during")
                logger.error(
                    "Sandbox %s container exited during boot (%s)",
                    sandbox.sandbox_id, sandbox.stop_reason,
                )
                sandbox.boot = None
                return sandbox

            exit_code, output = await asyncio.to_thread(
                container.exec_run, ["/bin/sh", "-c", br.PROBE_SCRIPT]
            )
            text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
            snapshot = br.parse_probe(text if exit_code == 0 else "")

            if br.advanced(previous, snapshot):
                phase_started = time.monotonic()
                await publish(snapshot)
            previous = snapshot

            if snapshot.usable:
                sandbox.status = SandboxStatus.READY
                if snapshot.phase == br.PHASE_HOME_SYNC or (
                    snapshot.files_total and not snapshot.ready_marker
                ):
                    # Up and bindable while its home is still filling in. The
                    # briefing line is the honest half of that: the screen says
                    # what is still happening rather than pretending or dying.
                    logger.info(
                        "Sandbox %s is usable with home sync still running (%s)",
                        sandbox.sandbox_id, snapshot.progress_text() or "no count",
                    )
                    # ...and something must keep telling the truth after we
                    # return, or the row would say "4210/8630" forever. The
                    # follower runs the same probe until the box reports
                    # `ready`, then clears the line.
                    _follow_boot_to_completion(sandbox.sandbox_id, store)
                else:
                    sandbox.boot = None
                return sandbox

        except (NotFound, APIError) as e:
            logger.warning("Error polling sandbox %s: %s", sandbox.sandbox_id, e)
            sandbox.status = SandboxStatus.FAILED
            sandbox.stop_reason = f"boot probe could not reach the container: {e}"
            sandbox.boot = None
            return sandbox

        budget = br.phase_budget(
            previous.budget_phase if previous else None,
            ready_budget=ready_budget,
            home_sync_budget=home_sync_budget,
        )
        stalled_for = time.monotonic() - phase_started
        if stalled_for >= budget:
            reason = br.timeout_reason(previous, budget, time.monotonic() - started)
            logger.warning("Sandbox %s gave up: %s", sandbox.sandbox_id, reason)
            sandbox.status = SandboxStatus.FAILED
            sandbox.stop_reason = reason
            # A dead box has no boot in progress. The reason carries the phase
            # and count; a live-looking progress line on a failed row would be
            # the screen lying again.
            sandbox.boot = None
            return sandbox

        await asyncio.sleep(poll_interval)


#: Detached boot followers, keyed by sandbox id, so one box never gets two.
_boot_followers: dict[str, asyncio.Task] = {}


def _follow_boot_to_completion(sandbox_id: str, store: "SandboxStore") -> None:
    """Keep a handed-over box's boot line true until its boot really ends.

    A box admitted mid home-sync carries a progress line. Without this, that
    line would be frozen at whatever count it had when we returned — a screen
    stating something that stopped being true, which is the quiet half of the
    same defect the phase budgets fixed. The follower re-runs the same probe
    and clears ``boot`` once the box reports ``ready``.

    Detached and entirely best-effort: it never blocks a create, never fails
    one, and an orchestrator restart that loses it cannot leave a lying row
    either — ``SandboxBoot`` expires on age when it is read back.
    """
    existing = _boot_followers.get(sandbox_id)
    if existing is not None and not existing.done():
        return

    async def follow() -> None:
        from orchestrator import boot_readiness as br

        client = _get_docker_client()
        try:
            budget = float(await knob_int("home_sync_timeout_seconds"))
        except Exception:
            return
        deadline = time.monotonic() + budget
        previous: br.BootSnapshot | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(BOOT_FOLLOW_INTERVAL_SECONDS)
            try:
                container = await asyncio.to_thread(client.containers.get, sandbox_id)
                code, output = await asyncio.to_thread(
                    container.exec_run, ["/bin/sh", "-c", br.PROBE_SCRIPT]
                )
            except (NotFound, APIError):
                return
            text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
            snapshot = br.parse_probe(text if code == 0 else "")
            if not br.advanced(previous, snapshot):
                continue
            previous = snapshot

            row = await store.get(sandbox_id)
            if row is None or getattr(row.status, "value", row.status) not in {"ready", "running", "starting"}:
                return
            finished = snapshot.ready_marker or snapshot.phase == br.PHASE_READY
            row.boot = None if finished else SandboxBoot(
                phase=snapshot.budget_phase,
                files_done=snapshot.files_done,
                files_total=snapshot.files_total,
                briefing=snapshot.briefing(),
                updated_at=datetime.now(timezone.utc),
            )
            try:
                await store.save(row)
            except Exception as exc:
                logger.warning("Boot follower could not update %s: %s", sandbox_id, exc)
                return
            if finished:
                logger.info("Sandbox %s finished its boot; progress line cleared", sandbox_id)
                return

    try:
        task = asyncio.get_running_loop().create_task(follow())
    except RuntimeError:
        return
    _boot_followers[sandbox_id] = task
    task.add_done_callback(lambda _t: _boot_followers.pop(sandbox_id, None))


def _backfill_proxy_url(sb: SandboxResponse | None) -> SandboxResponse | None:
    """Stamp proxy_url on a SandboxResponse loaded from storage.

    The store may hold rows persisted before MATRX_PUBLIC_URL was configured,
    or before the proxy_url field existed; recompute on read so clients
    always see the current orchestrator's idea of the URL.
    """
    if sb is not None and not sb.proxy_url:
        sb.proxy_url = _proxy_url_for(sb.sandbox_id)
    return sb


async def get_sandbox(sandbox_id: str) -> SandboxResponse | None:
    """Get sandbox info by ID."""
    store = _get_store()
    return _backfill_proxy_url(await store.get(sandbox_id))


async def migration_fenced(sandbox_id: str) -> bool:
    """Resolve exact shared-home scope from the durable row, never labels."""
    from orchestrator.hosted_migration import hosted_fenced, hosted_volume_fenced
    sandbox = await get_sandbox(sandbox_id)
    return hosted_fenced(sandbox_id) or bool(sandbox and hosted_volume_fenced(sandbox.persistence_volume))


async def list_sandboxes(
    user_id: str | None = None, include_deleted: bool = False
) -> list[SandboxResponse]:
    """List sandboxes, optionally filtered by user. Soft-deleted rows are
    excluded unless ``include_deleted`` (admin/debug only)."""
    store = _get_store()
    rows = await store.list(user_id=user_id, include_deleted=include_deleted)
    for sb in rows:
        _backfill_proxy_url(sb)
    return rows


async def get_sandbox_internal_ip(sandbox_id: str) -> str | None:
    """Get the internal Docker IP address of a running sandbox.

    Async + threaded: this runs on every proxied tool call (fs/exec/git), so a
    synchronous docker round-trip here would stall the whole event loop.
    """
    client = _get_docker_client()
    try:
        container = await asyncio.to_thread(client.containers.get, sandbox_id)
        return container.attrs.get("NetworkSettings", {}).get("Networks", {}).get(settings.docker_network, {}).get("IPAddress")
    except (NotFound, APIError):
        return None



async def exec_in_sandbox(
    sandbox_id: str,
    command: str,
    timeout: int = 30,
    user: str = "agent",
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str, str]:
    """Execute a command inside a running sandbox with CWD tracking.

    Returns (exit_code, stdout, stderr, cwd_after).

    The command is wrapped so it runs in the tracked working directory.
    After execution, the new CWD is captured via a sentinel/pwd trailer
    and cached for the next call.  Clients may also pass an explicit
    ``cwd`` to override the server-tracked value.

    ``env`` is merged into the container default env for this single exec.
    ``stdin`` is fed to the command on stdin; large stdin payloads bypass the
    command-length cap (use this instead of inline heredocs).
    """
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox or not sandbox.container_id:
        raise ValueError(f"Sandbox {sandbox_id} not found or has no container")

    # C2: Validate command length (infrastructure.sandbox.max_command_length)
    max_command_length = await knob_int("max_command_length")
    if len(command) > max_command_length:
        raise ValueError(
            f"Command exceeds max length ({max_command_length} chars)"
        )

    # Resolve CWD: explicit param > server cache > default
    effective_cwd = cwd or _sandbox_cwd.get(sandbox_id, _DEFAULT_CWD)

    client = _get_docker_client()
    try:
        container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)

        # C1/H5: Validate container is actually running before exec
        await asyncio.to_thread(container.reload)
        if container.status != "running":
            raise RuntimeError(
                f"Container for sandbox {sandbox_id} is not running "
                f"(status: {container.status})"
            )

        logger.info(
            "Executing command in sandbox %s (user=%s, timeout=%d, len=%d, cwd=%s, env_keys=%d, stdin=%s)",
            sandbox_id, user, timeout, len(command), effective_cwd,
            len(env or {}), "yes" if stdin else "no",
        )

        # Wrap the command so it:
        #   1. cd's into the tracked CWD
        #   2. runs the user command
        #   3. prints a sentinel + pwd so we can capture the new CWD
        # Pass the user command to ``eval`` as one shell-quoted argument instead
        # of interpolating it into a brace group.  Appending ``; }`` directly
        # after a heredoc terminator changes valid shell into a syntax error.
        # ``eval`` parses the command as its own complete shell program while
        # still running in this shell, so intentional ``cd`` changes remain
        # visible to the CWD trailer.
        # THE VAULT ENV LINE (2026-09-18). ``docker exec`` always injects the
        # environment the CONTAINER WAS CREATED WITH — a snapshot that is wrong
        # the moment the person adds, rotates or deletes a vault value, and
        # that cannot be changed without destroying the container. The
        # binding-time refresh writes the current truth to
        # /etc/matrx/vault-env.sh; sourcing it here is what makes a month-old
        # box run the agent's command with today's credentials. `bash -c` is
        # neither a login nor an interactive shell, so it reads no profile and
        # no bashrc: without this line the file would reach ssh sessions and
        # miss the tool path entirely. MATRX_VAULT_ENV_SKIP protects names this
        # caller set on purpose for this one exec.
        # Identity first, vault second. `docker exec` injects only the env the
        # CONTAINER was created with, and a box older than write-bridge-env.sh
        # was created without ORGANIZATION_ID at all — proven on admin's
        # sbx-cd6d53863995, 2026-09-18, where the login shell had the
        # organization and the tool path did not.
        vault_line = (
            f"[ -r {shlex.quote(BRIDGE_ENV_FILE)} ] && . {shlex.quote(BRIDGE_ENV_FILE)}; "
            f"[ -r {shlex.quote(VAULT_ENV_FILE)} ] && . {shlex.quote(VAULT_ENV_FILE)}; "
        )
        wrapped = (
            f"MATRX_VAULT_ENV_SKIP={shlex.quote(' '.join(sorted(env or {})))}; "
            f"export MATRX_VAULT_ENV_SKIP; "
            f"{vault_line}"
            f"cd {shlex.quote(effective_cwd)} && "
            f"eval -- {shlex.quote(command)}; "
            f"__matrx_ec=$?; "
            f"echo '{_CWD_SENTINEL}'; "
            f"pwd; "
            f"exit $__matrx_ec"
        )

        exec_kwargs: dict = dict(
            cmd=["bash", "-c", wrapped],
            user=user,
            demux=True,
        )
        if env:
            exec_kwargs["environment"] = env
        if stdin is not None:
            # docker-py exec_run with stdin requires socket attach; we use create+start.
            return await _exec_with_stdin(
                container, wrapped, user, env or {}, stdin, sandbox_id, effective_cwd,
            )

        # The actual command run — can be arbitrarily long (e.g. a build or a
        # `sleep`). Offloaded to a thread so it never blocks the event loop.
        exit_code, output = await asyncio.to_thread(container.exec_run, **exec_kwargs)

        stdout_raw = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")

        # Parse sentinel to extract real stdout and new CWD
        stdout, new_cwd = _parse_cwd_sentinel(stdout_raw)
        if new_cwd:
            _sandbox_cwd[sandbox_id] = new_cwd
        else:
            new_cwd = effective_cwd

        return exit_code, stdout, stderr, new_cwd

    except (NotFound, APIError) as e:
        raise RuntimeError(f"Failed to exec in sandbox {sandbox_id}: {e}") from e


async def _exec_with_stdin(
    container,
    wrapped: str,
    user: str,
    env: dict[str, str],
    stdin_data: str,
    sandbox_id: str,
    effective_cwd: str,
) -> tuple[int, str, str, str]:
    """Run an exec where stdin is fed to the command.

    docker-py's ``exec_run`` doesn't accept a stdin payload directly. We use
    the lower-level ``exec_create`` + ``exec_start`` pair with ``socket=True``
    to write the input, then collect output.
    """
    # The whole exec_create + socket send/recv + exec_inspect dance is blocking
    # docker/socket IO. Run it in a thread so a slow stdin-fed command can't
    # freeze the event loop.
    def _blocking() -> tuple[int, str, str]:
        api = container.client.api
        exec_id = api.exec_create(
            container.id,
            cmd=["bash", "-c", wrapped],
            user=user,
            environment=env,
            stdin=True,
            tty=False,
            stdout=True,
            stderr=True,
        )["Id"]

        # ``socket=True`` returns Docker's raw multiplexed wire stream.  The
        # SDK's ``demux`` option only applies when it consumes that socket, so
        # demux it below after we have written stdin and closed its write side.
        sock = api.exec_start(exec_id, socket=True)
        raw = sock._sock if hasattr(sock, "_sock") else sock
        try:
            raw.sendall(stdin_data.encode("utf-8"))
            try:
                raw.shutdown(1)  # SHUT_WR — signal EOF on stdin
            except OSError:
                pass

            chunks_out, chunks_err = _demux_docker_exec_socket(raw)
        finally:
            try:
                raw.close()
            except OSError:
                pass

        inspect = api.exec_inspect(exec_id)
        return (
            inspect.get("ExitCode") or 0,
            chunks_out.decode("utf-8", errors="replace"),
            chunks_err.decode("utf-8", errors="replace"),
        )

    exit_code, stdout_raw, stderr = await asyncio.to_thread(_blocking)

    stdout, new_cwd = _parse_cwd_sentinel(stdout_raw)
    if new_cwd:
        _sandbox_cwd[sandbox_id] = new_cwd
    else:
        new_cwd = effective_cwd
    return exit_code, stdout, stderr, new_cwd


def _demux_docker_exec_socket(raw: Any) -> tuple[bytearray, bytearray]:
    """Read Docker's non-TTY exec stream, separating stdout and stderr frames.

    Docker sends every frame as an 8-byte header (stream id plus big-endian
    payload length) followed by that many bytes.  ``recv`` is allowed to split
    either a header or a UTF-8 payload across arbitrary boundaries, so bytes
    remain buffered until a complete frame is available and decoding happens
    only after all frames have been collected.
    """
    stdout = bytearray()
    stderr = bytearray()
    pending = bytearray()

    while True:
        data = raw.recv(65536)
        if not data:
            break
        pending.extend(data)

        while len(pending) >= 8:
            stream_id = pending[0]
            payload_length = int.from_bytes(pending[4:8], "big")
            frame_length = 8 + payload_length
            if len(pending) < frame_length:
                break

            payload = pending[8:frame_length]
            del pending[:frame_length]
            if stream_id == 1:
                stdout.extend(payload)
            elif stream_id == 2:
                stderr.extend(payload)
            else:
                raise RuntimeError(
                    f"Docker exec stream used unsupported stream id {stream_id}"
                )

    if pending:
        raise RuntimeError("Docker exec stream ended with an incomplete multiplex frame")
    return stdout, stderr


def _parse_cwd_sentinel(raw: str) -> tuple[str, str | None]:
    """Split stdout at the CWD sentinel, returning (visible_output, new_cwd)."""
    idx = raw.rfind(_CWD_SENTINEL)
    if idx == -1:
        return raw, None
    output = raw[:idx].rstrip("\n")
    after = raw[idx + len(_CWD_SENTINEL):].strip()
    lines = after.split("\n")
    new_cwd = lines[0].strip() if lines else None
    return output, new_cwd


def forget_sandbox_cwd(sandbox_id: str) -> None:
    """Remove CWD state for a destroyed sandbox."""
    _sandbox_cwd.pop(sandbox_id, None)


async def _finalize_terminal_status(
    store: SandboxStore,
    sandbox_id: str,
    reason: str,
    final_status: SandboxStatus | None,
) -> None:
    """Write the post-teardown terminal status.

    Default (``final_status=None``) → ``mark_stopped`` (status='stopped',
    stamps stop_reason). For the expiry path the caller passes
    ``SandboxStatus.EXPIRED`` so the row lands in the resumable EXPIRED
    state instead of being overwritten to 'stopped' — ``expire_stale``
    already stamped stop_reason='expired'/stopped_at, so we only re-affirm
    the status here (the transient SHUTTING_DOWN write during teardown
    would otherwise leave it wrong if the process died mid-destroy).
    """
    if final_status is None:
        await store.mark_stopped(sandbox_id, reason)
    else:
        await store.update_status(sandbox_id, final_status)


async def destroy_sandbox(
    sandbox_id: str,
    graceful: bool = True,
    reason: str = "user_requested",
    final_status: SandboxStatus | None = None,
    *,
    _lifecycle_lease_held: bool = False,
) -> bool:
    """Destroy under the exact hosted home lease, including reaper callers."""
    sandbox = await _get_store().get(sandbox_id)
    if not sandbox:
        return False
    if _lifecycle_lease_held:
        return await _destroy_sandbox_unleased(sandbox_id, graceful, reason, final_status)
    from orchestrator.home_identity import home_key
    volume = home_key(sandbox)
    tier = getattr(sandbox.tier, "value", sandbox.tier) or settings.host_tier
    if tier == "ec2" and not volume:
        # Legacy EC2 homes live in the writable layer.  Keeping the stopped
        # container is the only non-lossy lifecycle until promotion completes.
        from orchestrator.hosted_operation_lease import hosted_operation_lease
        async with hosted_operation_lease(sandbox_id, f"layer-{sandbox_id}", lifecycle=True):
            return await _destroy_sandbox_unleased(sandbox_id, graceful, reason, final_status)
    if not volume and getattr(sandbox, "user_id", None) and getattr(sandbox, "organization_id", None):
        volume = user_volume_name(sandbox.user_id, sandbox.organization_id)
    if not volume:
        logger.warning("Refusing hosted destroy without an authoritative home for %s", sandbox_id)
        if settings.host_tier == "hosted":
            return False
        return await _destroy_sandbox_unleased(sandbox_id, graceful, reason, final_status)
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    async with hosted_operation_lease(sandbox_id, volume, lifecycle=True):
        return await _destroy_sandbox_unleased(sandbox_id, graceful, reason, final_status)


async def _destroy_sandbox_unleased(
    sandbox_id: str,
    graceful: bool = True,
    reason: str = "user_requested",
    final_status: SandboxStatus | None = None,
) -> bool:
    """Destroy a sandbox, optionally with graceful shutdown.

    Records the stop reason (user_requested, expired, error, graceful_shutdown, admin)
    and marks the sandbox as stopped rather than deleting, preserving audit history.

    ``final_status`` overrides the terminal status written after teardown.
    The reaper passes ``SandboxStatus.EXPIRED`` so expired sandboxes stay
    distinguishable from user-stopped ones (both keep their volume and are
    resumable). Leave it ``None`` for the normal stop path.
    """
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        return False
    from orchestrator.hosted_migration import hosted_fenced, hosted_volume_fenced
    if hosted_fenced(sandbox_id) or hosted_volume_fenced(getattr(sandbox, "persistence_volume", None)):
        logger.warning("Refusing destroy for hosted migration-fenced sandbox/home %s", sandbox_id)
        return False

    forget_sandbox_cwd(sandbox_id)

    await store.update_status(sandbox_id, SandboxStatus.SHUTTING_DOWN)

    logger.info("Destroying sandbox %s (graceful=%s, reason=%s)", sandbox_id, graceful, reason)

    client = _get_docker_client()
    try:
        # Prefer the immutable recorded id.  A legacy EC2 writable layer is
        # retained by that exact container, not by whatever later acquires its
        # human-readable sandbox name.
        from orchestrator.hosted_runtime import _docker
        container = await _docker(
            client.containers.get, sandbox.container_id or sandbox.sandbox_id
        )
        await _docker(container.reload)
        if sandbox.container_id and container.id != sandbox.container_id:
            raise RuntimeError("refusing teardown of a substituted sandbox runtime")

        # Capture the box's .matrx/memory/ back to central memory BEFORE we stop
        # it (the dir must still be readable). Best-effort — never block teardown.
        # graceful only: a kill means we're not trying to save anything.
        if graceful:
            try:
                from orchestrator.memory_sync import capture_memory_from_container
                # Memory capture is best-effort and must fit inside the same
                # bounded shutdown budget.  Otherwise a dead Docker archive
                # stream retains lifecycle/deployment locks indefinitely and
                # prevents the next orchestrator release from promoting.
                await asyncio.wait_for(
                    capture_memory_from_container(container, sandbox.user_id, store),
                    timeout=await knob_int("shutdown_timeout_seconds"),
                )
            except TimeoutError:
                logger.warning(
                    "Memory capture timed out for %s; continuing graceful teardown",
                    sandbox_id,
                )
            except Exception as exc:
                logger.warning("Memory capture skipped for %s: %s", sandbox_id, exc)

        if graceful:
            await _docker(
                container.stop, timeout=await knob_int("shutdown_timeout_seconds") + 10
            )
        else:
            await _docker(container.kill)

        # ``container.remove`` deletes the container itself — anonymous
        # volumes go with it, but the *named* per-user volume we mounted
        # at /home/agent (hosted tier) is preserved. That's the whole
        # point of the persistence model: user data survives sandbox
        # lifecycle. Explicit volume wipe is a separate, admin-only path
        # (see ``delete_user_volume``).
        tier = getattr(sandbox.tier, "value", sandbox.tier) or settings.host_tier
        from orchestrator.hosted_runtime import _state_volume_mount, remove_migration_state_volume
        state_volume = _state_volume_mount(container, sandbox.sandbox_id)
        if tier == "ec2" and not sandbox.persistence_volume:
            logger.warning(
                "Retained legacy EC2 writable-layer container %s; promotion is required before replacement",
                sandbox_id,
            )
        else:
            await _docker(container.remove, force=True)
            if state_volume:
                await remove_migration_state_volume(
                    client, sandbox.sandbox_id, expected_name=state_volume,
                )

        await _finalize_terminal_status(store, sandbox_id, reason, final_status)

        logger.info(
            "Sandbox %s destroyed (volume %s preserved)",
            sandbox_id, sandbox.persistence_volume or "n/a — EC2 tier",
        )
        return True

    except NotFound:
        await _finalize_terminal_status(store, sandbox_id, reason, final_status)
        logger.warning("Sandbox %s container not found during destroy", sandbox_id)
        return True

    except APIError as e:
        await store.update_status(sandbox_id, SandboxStatus.FAILED)
        logger.error("Failed to destroy sandbox %s: %s", sandbox_id, e)
        return False


async def resume_retained_ec2_layer(sandbox: SandboxResponse) -> SandboxResponse:
    """Restart exactly the stopped legacy EC2 container; never replace its layer."""
    if getattr(sandbox.tier, "value", sandbox.tier) != "ec2" or sandbox.persistence_volume:
        raise RuntimeError("sandbox is not a retained EC2 writable-layer sandbox")
    if not sandbox.container_id:
        raise RuntimeError("retained EC2 writable-layer sandbox has no recorded container identity")
    client = _get_docker_client()
    try:
        container = await asyncio.to_thread(client.containers.get, sandbox.container_id)
        await asyncio.to_thread(container.reload)
    except NotFound as exc:
        raise RuntimeError("retained EC2 writable-layer container is missing; it cannot be resumed safely") from exc
    if container.id != sandbox.container_id or container.status not in {"exited", "created"}:
        raise RuntimeError("retained EC2 writable-layer container identity or state is unsafe to resume")
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    async with hosted_operation_lease(sandbox.sandbox_id, f"layer-{sandbox.sandbox_id}", lifecycle=True):
        fresh = await _get_store().get(sandbox.sandbox_id)
        life = await _get_store().get_lifecycle(sandbox.sandbox_id)
        if not fresh or not life or life.get("deleted") or fresh.container_id != sandbox.container_id or fresh.persistence_volume:
            raise RuntimeError("retained layer routing changed before resume")
        await asyncio.to_thread(container.reload)
        if container.status not in {"exited", "created"}:
            raise RuntimeError("retained layer was already resumed")
        await _get_store().resume_active(sandbox)
        sandbox.status = SandboxStatus.STARTING
        await asyncio.to_thread(container.start)
        sandbox = await _wait_for_ready(sandbox)
        await _get_store().save(sandbox)
        return sandbox


async def wipe_retained_ec2_layer(
    sandbox: SandboxResponse, *, _lifecycle_lease_held: bool = False,
) -> None:
    """Explicitly discard one stopped legacy EC2 writable layer by exact ID.

    The lease covers the row re-read and Docker removal: a concurrent resume
    must either win before this operation is admitted or be denied until the
    removal finishes.  A stale reset request must never delete a successor.
    """
    if getattr(sandbox.tier, "value", sandbox.tier) != "ec2" or sandbox.persistence_volume or not sandbox.container_id:
        raise RuntimeError("sandbox has no retained EC2 writable layer to wipe")
    expected = (sandbox.sandbox_id, sandbox.user_id, sandbox.organization_id,
                sandbox.container_id, sandbox.created_at)

    async def remove_under_lease() -> None:
        store = _get_store()
        fresh = await store.get(sandbox.sandbox_id)
        lifecycle = await store.get_lifecycle(sandbox.sandbox_id)
        terminal = {"stopped", "expired", "failed"}
        if (
            not fresh or not lifecycle or lifecycle.get("deleted")
            or getattr(fresh.tier, "value", fresh.tier) != "ec2"
            or fresh.persistence_volume
            or (fresh.sandbox_id, fresh.user_id, fresh.organization_id,
                fresh.container_id, fresh.created_at) != expected
            or lifecycle.get("status") not in terminal
            or getattr(fresh.status, "value", fresh.status) not in terminal
        ):
            raise RuntimeError("retained EC2 layer routing is no longer terminal and exact")

        client = _get_docker_client()
        container = await asyncio.to_thread(client.containers.get, fresh.container_id)
        await asyncio.to_thread(container.reload)
        labels = ((getattr(container, "attrs", None) or {}).get("Config") or {}).get("Labels") or {}
        expected_labels = {
            "matrx.sandbox_id": fresh.sandbox_id,
            "matrx.user_id": fresh.user_id,
            "matrx.organization_id": fresh.organization_id,
            "matrx.tier": "ec2",
        }
        if (
            container.id != fresh.container_id
            or container.status not in {"exited", "created"}
            or any(labels.get(key) != value for key, value in expected_labels.items())
        ):
            raise RuntimeError("refusing to wipe a non-retained EC2 writable-layer container")
        await asyncio.to_thread(container.remove, force=False)
        logger.warning("Explicitly wiped retained EC2 writable layer for %s", fresh.sandbox_id)

    if _lifecycle_lease_held:
        await remove_under_lease()
        return
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    async with hosted_operation_lease(sandbox.sandbox_id, f"layer-{sandbox.sandbox_id}", lifecycle=True):
        task = asyncio.create_task(remove_under_lease())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise


async def delete_ec2_home_volume(
    sandbox: SandboxResponse, *, _lifecycle_lease_held: bool = False,
) -> bool:
    """Delete exactly one validated, terminal EC2 home under its home lease."""
    reference = sandbox.persistence_volume
    if getattr(sandbox.tier, "value", sandbox.tier) != "ec2" or not reference:
        raise RuntimeError("sandbox has no EC2 durable home to wipe")
    expected = (sandbox.sandbox_id, sandbox.user_id, sandbox.organization_id,
                reference, sandbox.created_at)

    async def remove_under_lease() -> bool:
        store = _get_store()
        fresh = await store.get(sandbox.sandbox_id)
        lifecycle = await store.get_lifecycle(sandbox.sandbox_id)
        terminal = {"stopped", "expired", "failed"}
        if (
            not fresh or not lifecycle or lifecycle.get("deleted")
            or getattr(fresh.tier, "value", fresh.tier) != "ec2"
            or (fresh.sandbox_id, fresh.user_id, fresh.organization_id,
                fresh.persistence_volume, fresh.created_at) != expected
            or lifecycle.get("status") not in terminal
            or getattr(fresh.status, "value", fresh.status) not in terminal
        ):
            raise RuntimeError("EC2 durable home routing is no longer terminal and exact")

        client = _get_docker_client()
        await asyncio.to_thread(validate_ec2_home_volume, client, reference, fresh)
        attached = await asyncio.to_thread(
            client.containers.list, all=True, filters={"volume": reference}
        )
        if attached:
            raise RuntimeError("EC2 durable home is attached to a successor or writer")
        volume = await asyncio.to_thread(client.volumes.get, reference)
        await asyncio.to_thread(volume.remove, force=False)
        logger.warning("Explicitly wiped EC2 durable home %s for %s", reference, fresh.sandbox_id)
        return True

    if _lifecycle_lease_held:
        return await remove_under_lease()
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    async with hosted_operation_lease(sandbox.sandbox_id, reference, lifecycle=True):
        task = asyncio.create_task(remove_under_lease())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise


async def delete_user_volume(
    user_id: str, organization_id: str, *, _lifecycle_lease_held: bool = False,
) -> bool:
    """Hard-delete a user's per-user Docker volume (hosted tier only).

    Use case: a user explicitly clicks "Delete my persistent storage" in the
    admin panel, OR an admin needs to forcibly wipe a user's home dir. This is
    a destructive operation — there's no undo. Refuses to run if the volume
    has active containers attached.
    """
    from orchestrator.storage_layout import user_volume_name

    name = user_volume_name(user_id, organization_id)

    async def remove_under_lease() -> bool:
        client = _get_docker_client()
        in_use = await asyncio.to_thread(
            lambda: client.containers.list(all=True, filters={"volume": name})
        )
        if in_use:
            raise RuntimeError(
                f"Volume {name} is still in use by {len(in_use)} container(s). "
                "Stop those sandboxes first."
            )
        try:
            volume = await asyncio.to_thread(client.volumes.get, name)
        except NotFound:
            logger.info(
                "delete_user_volume(%s, %s): volume not found, no-op",
                user_id, organization_id,
            )
            return True
        try:
            await asyncio.to_thread(volume.remove, force=False)
            logger.warning("Deleted user volume %s for user %s", name, user_id)
            return True
        except APIError as e:
            logger.error("Failed to delete volume %s: %s", name, e)
            return False

    if _lifecycle_lease_held:
        return await remove_under_lease()
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    operation_id = f"volume-delete-{user_id}-{organization_id}"
    async with hosted_operation_lease(operation_id, name, lifecycle=True):
        task = asyncio.create_task(remove_under_lease())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise


async def get_user_volume_size(user_id: str, organization_id: str) -> int | None:
    """Return the current size in bytes of a user's hosted-tier volume.

    None if the volume doesn't exist (user has no hosted-tier data yet) or
    Docker doesn't expose UsageData (older daemons / flag-disabled setups).
    Cheap call — uses the docker daemon's own metadata, doesn't `du`.
    """
    from orchestrator.storage_layout import user_volume_name

    name = user_volume_name(user_id, organization_id)
    client = _get_docker_client()
    try:
        # API endpoint /volumes returns a list with UsageData when invoked
        # with the `?status=true` flag-equivalent in the SDK.
        volumes_data = await asyncio.to_thread(client.api.volumes)
        for v in volumes_data.get("Volumes") or []:
            if v.get("Name") == name:
                usage = v.get("UsageData") or {}
                size = usage.get("Size")
                # Docker returns -1 when usage data isn't enabled (default).
                # We need a separate `du -sb` path for accurate sizes; punt
                # to Phase 5 (quotas) where that lives.
                return size if isinstance(size, int) and size >= 0 else None
    except APIError as e:
        logger.warning("Failed to query volume size for %s: %s", name, e)
        return None
    return None


async def heartbeat(sandbox_id: str) -> bool:
    """Record a heartbeat from a sandbox. Returns True if sandbox exists."""
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        return False
    await store.update_heartbeat(sandbox_id)
    return True


async def generate_user_access(sandbox_id: str) -> dict[str, str | int]:
    """Generate a temporary SSH keypair and inject the public key into the sandbox.

    Returns a dict with private_key (PEM), username, host, and port.
    The private key is generated per-request and never stored by the orchestrator.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        raise ValueError(f"Sandbox {sandbox_id} not found")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()

    inject_cmd = (
        "mkdir -p /home/agent/.ssh && "
        f"echo '{public_key} user-access' >> /home/agent/.ssh/authorized_keys && "
        "chown agent:agent /home/agent/.ssh/authorized_keys && "
        "chmod 600 /home/agent/.ssh/authorized_keys"
    )
    await exec_in_sandbox(sandbox_id, inject_cmd, user="root")

    logger.info("Generated temporary SSH access for sandbox %s", sandbox_id)

    return {
        "private_key": private_pem,
        "username": "agent",
        "host": _resolve_ssh_host(),
        "port": sandbox.ssh_port or 22,
    }
