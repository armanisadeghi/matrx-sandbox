"""Binding-time vault env refresh — "declared in the briefing" equals "in the box".

THE GAP (2026-09-18, Arman's box sbx-7ddc2eb0c364).
A container's environment is a SNAPSHOT. ``sandbox_manager.create_sandbox`` calls
aidream once, at create time, for the user's injectable vault values and hands
them to ``containers.run``. Nothing ever reads the vault again. So:

* a value the person ADDS after the box was born never arrives — the sandbox
  briefing keeps naming ``GITHUB_TOKEN`` and ``BRIGHT_DATA_API_KEY`` as
  "available in this sandbox" while the shell has neither;
* a value the person DELETES never leaves — Arman deleted his ``GITHUB_PAT``
  vault row on 2026-07-24 and the name was still in boxes on 2026-09-18,
  answering ``git push`` with a revoked token;
* a ROTATED value stays at whatever it was on the box's birthday.

A box lives for weeks. A snapshot taken on day one is a lie by day two, and the
briefing turns that lie into a promise.

THE PRIMITIVE. Binding is the one moment an agent is about to use the box, and
it is where the SDK refresh already meets it (``sdk_refresh.py``). This hook
re-fetches the SAME internal aidream route the create path uses, writes the
values to ``/etc/matrx/vault-env.sh`` (root-owned, agent-readable, in the
container layer — NEVER the user's home volume, which is backed up and shared),
and reports which NAMES were added and removed. Names only. Never a value, in a
log, a diagnostic, or the briefing.

WHY A FILE AND NOT A NEW CONTAINER. Recreating the container to change one env
var would destroy the agent's running processes and shell state for a value
change. The file is read by:

* login shells, via ``/etc/profile.d/01-matrx-vault-env.sh`` (ssh);
* the agent's home env, via a source line in ``~/.sandbox_env``;
* EVERY orchestrator ``exec`` — the tool path an agent actually runs commands
  through — because ``sandbox_manager.exec_in_sandbox`` sources it in its
  wrapper. That is what lets a month-old box see a value added this morning.

The container's own ``environ`` (what ``docker inspect`` shows, what PID 1 was
started with) still holds the birthday snapshot. It cannot be changed in place,
which is exactly why the file has to win: a stale container value that is no
longer in the vault is UNSET by the file rather than shadowed.

SAFETY. Same posture as the SDK refresh:
* **Never fatal.** Every failure is a status on the binding diagnostics and a
  loud log line. The token is minted either way.
* **Rate limited** per box per vault version: the version is a digest of the
  fetched name/value set, stamped in the box, so an unchanged vault costs one
  ``cat`` and a box already at that version costs nothing at all.
* **A knob**, ``infrastructure.sandbox.vault_env_refresh_on_binding`` (on).
* **Never during a migration.** A fenced box is left alone.

IT PUBLISHES THE BOX'S IDENTITY TOO (2026-09-18). ``write-bridge-env.sh`` — the
script that copies ``USER_ID``, ``ORGANIZATION_ID``, ``MATRX_AIDREAM_URL`` and
the service token out of the container environment into
``/etc/matrx/bridge-env.sh`` so SHELLS can see them — did not exist when older
images were built. Admin's box sbx-cd6d53863995, read live on 2026-09-18, had no
``/etc/matrx`` at all and no ``ORGANIZATION_ID`` anywhere: ``docker exec`` saw
the identity (Docker injects the container's own env), every ssh session saw
none of it, and ``bridge-headers.sh`` — which every AI Dream call from a shell
goes through — would refuse for a missing organization even though the box is
perfectly wired. Delivering the new credential helper to such a box without this
would have made it WORSE: the helper would have started refusing loudly instead
of failing quietly.

So the identity is published here, from the ORCHESTRATOR's own trusted values —
``sandbox.user_id``, ``sandbox.organization_id`` and the configured AI Dream URL
and service token. Never from the client, never from the container's own env
(which is the very thing that may be missing a piece).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import shlex
import tarfile
import time

from orchestrator import activity, sandbox_manager
from orchestrator.bridge_headers import BridgeIdentityMissing, identity_headers
from orchestrator.browser_profile import BROWSER_ENV_NAMES, resolve_browser_profile
from orchestrator.config import settings
from orchestrator.knobs import (
    KnobNotRegisteredError,
    KnobSourceUnavailableError,
    knob_bool,
)
from orchestrator.models import SandboxResponse

logger = logging.getLogger(__name__)

KNOB = "vault_env_refresh_on_binding"
LEAK_KNOB = "clear_leaked_platform_env_on_binding"

#: Root-owned, agent-readable, container-layer only. Mirrors bridge-env.sh.
ENV_DIR = "/etc/matrx"
ENV_FILE = f"{ENV_DIR}/vault-env.sh"
STAMP_FILE = f"{ENV_DIR}/vault-env.stamp"
PROFILE_DROPIN = "/etc/profile.d/01-matrx-vault-env.sh"
#: The identity file and drop-in write-bridge-env.sh owns on a current image.
#: Exactly the same paths: a box that HAS the script keeps being served by it at
#: boot, and this republishes the same content rather than a second opinion.
BRIDGE_ENV_FILE = f"{ENV_DIR}/bridge-env.sh"
BRIDGE_PROFILE_DROPIN = "/etc/profile.d/00-matrx-bridge.sh"
BRIDGE_FAILED_FILE = f"{ENV_DIR}/bridge-env.FAILED"
IDENTITY_NAMES = (
    "SANDBOX_ID",
    "USER_ID",
    "ORGANIZATION_ID",
    "MATRX_AIDREAM_URL",
    "MATRX_AIDREAM_SERVICE_TOKEN",
)
#: The sandbox↔browser join, republished here for the same reason the identity
#: is: a person can create, rename or DELETE their cloud browser long after this
#: box was born, and the container's own environ cannot be rewritten in place.
#: Present → exported beside the identity. Absent → actively UNSET, so a box
#: whose browser was deleted stops naming a profile that no longer exists.
BROWSER_NAMES = BROWSER_ENV_NAMES

SANDBOX_ENV_FILE = "/home/agent/.sandbox_env"

FETCH_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 30

_locks: dict[str, asyncio.Lock] = {}
#: sandbox_id -> (vault_version, monotonic). A box already at this version in
#: this process is not re-checked for the TTL.
_recent: dict[str, tuple[str, float]] = {}
_RECENT_TTL = 300.0


def _result(status: str, **extra) -> dict:
    out = {
        "hook": "session_start.vault_env_refresh",
        "status": status,
        "added": [],
        "removed": [],
        "present": [],
    }
    out.update(extra)
    return out


def vault_version(env: dict[str, str]) -> str:
    """A stable digest of the whole name/value set.

    Values are hashed, never stored: the stamp left in the box must be able to
    say "the vault has not changed" without being a copy of the secrets. A
    rotation changes the digest even though the names did not, which is the
    point — a rotated value must land.
    """
    digest = hashlib.sha256()
    for name in sorted(env):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(env[name].encode()).digest())
    return digest.hexdigest()[:32]


def render_env_file(
    env: dict[str, str], *, version: str, removed: list[str] | None = None
) -> str:
    """The sourced file. Exports the vault's truth, and UNSETS a name the vault
    no longer has — the half a plain export list cannot do.

    ``MATRX_VAULT_ENV_SKIP`` lets one caller protect names it set explicitly for
    a single exec (``exec_in_sandbox(env=...)``): the vault outranks the stale
    container snapshot, but never the value a caller just passed on purpose.
    """
    lines = [
        "# Written by the orchestrator's binding-time vault env refresh.",
        "# Do not edit: it is rewritten whenever your AI Matrx vault changes.",
        f"# vault-version: {version}",
        "",
        "__matrx_vault_skip=\" ${MATRX_VAULT_ENV_SKIP:-} \"",
        "__matrx_vault_set() {",
        '    case "$__matrx_vault_skip" in *" $1 "*) return 0 ;; esac',
        '    export "$1=$2"',
        "}",
        "__matrx_vault_clear() {",
        '    case "$__matrx_vault_skip" in *" $1 "*) return 0 ;; esac',
        '    unset "$1"',
        "}",
        "",
    ]
    for name in sorted(env):
        lines.append(f"__matrx_vault_set {shlex.quote(name)} {shlex.quote(env[name])}")
    if removed:
        lines.extend(
            [
                "",
                "# Names this box was BORN with that the vault no longer has. Without",
                "# these lines they sit in the container's own environ forever — which",
                "# is how a revoked GITHUB_PAT outlived its vault row by two months.",
            ]
        )
        for name in sorted(removed):
            lines.append(f"__matrx_vault_clear {shlex.quote(name)}")
    lines.append("")
    lines.append("unset -f __matrx_vault_set __matrx_vault_clear")
    lines.append("unset __matrx_vault_skip")
    lines.append("")
    return "\n".join(lines)


def recorded_vault_names(sandbox) -> set[str]:
    """The person's own vault NAMES as this box last recorded them.

    ``create_sandbox`` stamps them on ``config.secrets_injection.names`` and the
    binding refresh restamps them. A caller that judges a box's env without this
    would call the person's OWN ``BRAVE_SEARCH_API_KEY`` a platform leak — which
    is precisely what ``/diagnostics`` did before XT-10 round 2.

    Returns an empty set when the box predates the stamp; the census then errs
    toward naming a name, which is the safe direction for a REPORT (it is never
    the direction used to decide a forward).
    """
    config = getattr(sandbox, "config", None)
    if not isinstance(config, dict):
        return set()
    names: set[str] = set()
    for block in ("secrets_injection", "vault_env_refresh"):
        blob = config.get(block)
        if isinstance(blob, dict):
            for field in ("names", "present", "added"):
                value = blob.get(field)
                if isinstance(value, list):
                    names.update(n for n in value if isinstance(n, str))
    return names


def unentitled_platform_env_names(
    container_env_names: list[str],
    *,
    template: str | None,
    vault_names: set[str],
) -> list[str]:
    """THE ONE CENSUS: platform env names a box holds that it is not entitled to.

    Pure. Two doors read it and must never disagree — the binding-time sweep
    below, and ``/sandboxes/{id}/diagnostics``'s ``platform_env_unentitled_*``.
    They drifted once (the diagnostic subtracted neither the person's vault nor
    ``ORCHESTRATOR_MANAGED_ENV`` and reported seven "leaks" on clean boxes), and
    a census that cries wolf is how a real leak gets ignored.

    THE RULE, and it is the same for EVERY template (XT-10 round 2). A box is
    entitled to:

    * the person's OWN vault items — theirs, never filtered;
    * ``ORCHESTRATOR_MANAGED_ENV`` — identity, storage, the per-box daemon
      token, the browser join. **Read the honest note in that constant: the
      shared AI Dream service token and the hosted-tier AWS pair are in it, so
      they DO enter every box by design. That is an open item, not a closed
      one;**
    * :data:`~orchestrator.sandbox_manager.PLATFORM_ENV_ALLOWLIST` — public by
      design for any template (it holds ``PATH`` and the locale, so it must be
      entitled even on a box that receives no passthrough at all);
    * for the ``aidream`` template, the orchestrator's own path overrides.

    Anything else it carries FROM the platform's own environment — the whole
    passthrough registry, the retired git-credential names, or any name of a
    master-credential shape — is unentitled and gets cleared.

    🚨 Why the registry and the patterns are UNIONED here, when the forward
    decision uses the allowlist alone: this function looks at a box that
    already exists, and a pre-fix box may carry a platform name that has since
    been removed from the registry. The registry is the comprehensive list of
    what the platform's env HAS; the patterns catch what it has since dropped.
    Both only ever ADD names to clear, so neither can reopen the door.

    It is NOT a general env cleaner: a name that is neither in the registry nor
    credential-shaped (the image's ``UV_*``, ``NPM_CONFIG_PREFIX``, the
    person's own exported variables) is none of its business.

    The non-passthrough branch used to be the pattern denylist — the very
    construct that leaked — and it left ``ANTHROPIC_KEY``,
    ``MATRX_SCRAPER_TOKEN`` and six ``SUPABASE_*_KEY`` names in admin's real
    ``bare`` boxes, which are exactly the boxes the 2026-09-13 incident
    contaminated. V-XT-10 measured that. Both branches are now one rule.
    """
    from orchestrator.sandbox_manager import (
        ORCHESTRATOR_MANAGED_ENV,
        PLATFORM_ENV_ALLOWLIST,
        RETIRED_GIT_CREDENTIAL_NAMES,
        _resolve_passthrough_keys,
        aidream_template_path_overrides,
        is_master_credential_name,
    )

    present = {name for name in container_env_names if name}

    entitled = (
        set(vault_names)
        | set(ORCHESTRATOR_MANAGED_ENV)
        | set(PLATFORM_ENV_ALLOWLIST)
    )
    if (template or "") == "aidream":
        entitled |= set(aidream_template_path_overrides())

    platform_shaped = (
        (present & set(_resolve_passthrough_keys()))
        | (present & set(RETIRED_GIT_CREDENTIAL_NAMES))
        | {name for name in present if is_master_credential_name(name)}
    )
    return sorted(platform_shaped - entitled)


def box_identity(sandbox: SandboxResponse) -> dict[str, str]:
    """The identity every AI Dream call from this box must carry.

    Sourced from the ORCHESTRATOR, which knows who the box belongs to, rather
    than from the container environment, which on an old box is missing pieces.
    """
    values = {
        "SANDBOX_ID": sandbox.sandbox_id,
        "USER_ID": str(sandbox.user_id or ""),
        "ORGANIZATION_ID": str(sandbox.organization_id or ""),
        "MATRX_AIDREAM_URL": settings.resolve_aidream_url() or "",
        "MATRX_AIDREAM_SERVICE_TOKEN": settings.resolve_aidream_service_token() or "",
    }
    return {name: value for name, value in values.items() if value}


def render_identity_file(identity: dict[str, str]) -> str:
    """Byte-compatible with what write-bridge-env.sh produces, so a box that
    also runs that script at boot cannot end up with two disagreeing files."""
    lines = [
        "# Written by the orchestrator's binding-time identity publish.",
        "# The identity this sandbox carries into every AI Dream call.",
    ]
    for name in (*IDENTITY_NAMES, *BROWSER_NAMES):
        if identity.get(name):
            lines.append(f"export {name}={shlex.quote(identity[name])}")
    lines.append("")
    return "\n".join(lines)


def render_bridge_dropin() -> str:
    return (
        "# Matrx sandbox identity — republished at binding by the orchestrator.\n"
        f"[ -r {BRIDGE_ENV_FILE} ] && . {BRIDGE_ENV_FILE}\n"
    )


def render_profile_dropin() -> str:
    return (
        "# Matrx sandbox vault environment — see the orchestrator's\n"
        "# vault_env_refresh.py. World-readable; the file it sources is not.\n"
        f"[ -r {ENV_FILE} ] && . {ENV_FILE}\n"
    )


async def refresh_vault_env(sandbox: SandboxResponse) -> dict:
    """The connection hook. Always returns a dict; never raises."""
    sandbox_id = sandbox.sandbox_id
    try:
        enabled = await knob_bool(KNOB)
    except (KnobNotRegisteredError, KnobSourceUnavailableError) as exc:
        logger.warning(
            "VAULT ENV REFRESH UNAVAILABLE for %s: %s. Seed the platform.feature_knob "
            "row 'infrastructure.sandbox.%s' — until then a box keeps whatever vault "
            "values it was CREATED with, and the briefing's vault section is a promise "
            "nobody is keeping.",
            sandbox_id, exc, KNOB,
        )
        return _result("unavailable", reason=f"knob {KNOB} unreadable: {exc}")
    if not enabled:
        return _result("disabled", reason=f"infrastructure.sandbox.{KNOB} is off")

    if activity.is_migrating(sandbox_id):
        return _result("skipped", reason="the box is migrating; it will be recreated with current values")

    lock = _locks.setdefault(sandbox_id, asyncio.Lock())
    async with lock:
        try:
            return await _refresh(sandbox)
        except Exception as exc:  # noqa: BLE001 — never fatal to a binding
            logger.exception("VAULT ENV REFRESH FAILED for %s", sandbox_id)
            return _result("failed", reason=f"{type(exc).__name__}: {exc}")


async def _refresh(sandbox: SandboxResponse) -> dict:
    sandbox_id = sandbox.sandbox_id
    client = sandbox_manager._get_docker_client()
    container = await asyncio.to_thread(
        client.containers.get, sandbox.container_id or sandbox_id
    )
    await asyncio.to_thread(container.reload)
    if container.status != "running":
        return _result("skipped", reason=f"container is {container.status}")

    env, fetch_error = await _fetch_vault_env(sandbox)
    if fetch_error is not None:
        # A fetch failure must NEVER be read as "the vault is empty" — that
        # would unset every value the box legitimately holds. Leave the box
        # exactly as it is and say why.
        logger.warning(
            "VAULT ENV REFRESH could not read the vault for %s: %s. The box keeps "
            "the values it already has.",
            sandbox_id, fetch_error,
        )
        return _result("unavailable", reason=fetch_error)

    identity = box_identity(sandbox)
    missing_identity = [n for n in IDENTITY_NAMES if n != "SANDBOX_ID" and not identity.get(n)]
    if missing_identity:
        # Loud, not fatal: the box still gets whatever identity we DO have, and
        # the helper in it will name the missing variable at first use.
        logger.warning(
            "BOX IDENTITY INCOMPLETE for %s: the orchestrator cannot supply %s. "
            "Shells in this box will refuse AI Dream calls and say so.",
            sandbox_id, ", ".join(missing_identity),
        )
    # The browser join, re-resolved every binding. Fail-open and never fatal:
    # ``resolve_browser_profile`` answers with a reason rather than raising, and
    # a box whose owner has no cloud browser simply carries neither name.
    browser = await resolve_browser_profile(
        user_id=str(sandbox.user_id or ""),
        organization_id=str(sandbox.organization_id or ""),
    )
    identity.update(browser.env())
    browser_cleared = [] if browser.present else list(BROWSER_NAMES)

    leaked: list[str] = []
    try:
        if await knob_bool(LEAK_KNOB):
            container_names = [
                entry.split("=", 1)[0]
                for entry in ((getattr(container, "attrs", None) or {}).get("Config") or {}).get(
                    "Env"
                )
                or []
            ]
            leaked = unentitled_platform_env_names(
                container_names, template=sandbox.template, vault_names=set(env)
            )
            if leaked:
                # 🚨 THE HALF THIS SWEEP CANNOT DO (V-XT-10). Unsetting a name in
                # /etc/matrx/vault-env.sh fixes SHELLS. It does NOT touch
                # Config.Env or /proc/1/environ — a container's own environment
                # cannot be rewritten in place — so the daemon, every process
                # already running, and `docker inspect` keep the credential
                # until the container is REPLACED. Say so with the remedy
                # instead of letting "swept" read as "clean".
                logger.warning(
                    "PLATFORM ENV LEAK found in %s (template=%s): %s. These are the "
                    "platform's own names in a box that is not entitled to them "
                    "(incident 2026-09-13 + the fail-open denylist closed by XT-10). "
                    "Unsetting them for every SHELL now — but the container's own "
                    "environ and /proc/1/environ KEEP them, so this box is not clean "
                    "until it is recreated: POST /sandboxes/%s/migrate rebuilds the "
                    "container from the same home, keeps the sandbox_id and the "
                    "volume, and rebuilds the env through the one chokepoint. Fleet "
                    "census: GET /platform-env-census.",
                    sandbox_id, sandbox.template, ", ".join(leaked), sandbox_id,
                )
    except (KnobNotRegisteredError, KnobSourceUnavailableError) as exc:
        logger.warning(
            "LEAK SWEEP UNAVAILABLE for %s: %s. Seed "
            "'infrastructure.sandbox.%s'; until then a box created before "
            "2026-09-13 keeps the platform credentials it leaked.",
            sandbox_id, exc, LEAK_KNOB,
        )

    version = vault_version(
        {
            **env,
            **{f"__id__{k}": v for k, v in identity.items()},
            **{f"__leak__{n}": "1" for n in leaked},
            **{f"__browser__{n}": "1" for n in browser_cleared},
        }
    )
    cached = _recent.get(sandbox_id)
    if cached and cached[0] == version and time.monotonic() - cached[1] < _RECENT_TTL:
        return _result(
            "current",
            present=sorted(env),
            vault_version=version,
            cached=True,
        )

    with _preserved_cwd(sandbox_id):
        async with activity.track(sandbox_id):
            stamp = await _read_stamp(sandbox_id)
            if stamp.get("vault_version") == version:
                _recent[sandbox_id] = (version, time.monotonic())
                return _result("current", present=sorted(env), vault_version=version)

            previous = [str(n) for n in (stamp.get("names") or [])]
            if not previous:
                # First refresh on this box: the baseline is the set the
                # CONTAINER was created with, which the create path stamped on
                # the row. Without it we cannot name what is going away.
                previous = _created_with_names(sandbox)

            added = sorted(set(env) - set(previous))
            removed = sorted(
                (set(previous) - set(env)) | set(leaked) | set(browser_cleared)
            )

            written = await _write_env_files(
                container=container,
                sandbox_id=sandbox_id,
                env=env,
                identity=identity,
                version=version,
                removed=removed,
            )

    _recent[sandbox_id] = (version, time.monotonic())
    result = _result(
        "refreshed",
        added=added,
        removed=removed,
        present=sorted(env),
        identity=sorted(identity),
        identity_missing=missing_identity,
        leaked_platform_env_cleared=leaked,
        # The sweep cleared them for SHELLS only; the container's own environ
        # still holds them. A caller reading only `leaked_platform_env_cleared`
        # would believe the box is clean, so the remedy travels WITH the claim.
        leaked_platform_env_still_in_container_environ=leaked,
        leaked_platform_env_remedy=(
            None if not leaked else
            f"This box is NOT clean: Config.Env and /proc/1/environ still hold "
            f"{len(leaked)} platform name(s). Recreate it from the same home with "
            f"POST /sandboxes/{sandbox_id}/migrate."
        ),
        browser_profile=browser.diagnostic(),
        vault_version=version,
        **written,
    )
    logger.info(
        "vault env refresh on %s: +%s -%s (%d present, version=%s)",
        sandbox_id, added or "none", removed or "none", len(env), version,
    )
    return result


def _created_with_names(sandbox: SandboxResponse) -> list[str]:
    """Names the create path recorded for this box, if it recorded any."""
    config = sandbox.config if isinstance(sandbox.config, dict) else {}
    injection = config.get("secrets_injection")
    if isinstance(injection, dict):
        names = injection.get("names")
        if isinstance(names, list):
            return [str(n) for n in names]
    return []


async def _fetch_vault_env(sandbox: SandboxResponse) -> tuple[dict[str, str], str | None]:
    """Re-read the user's injectable vault env through the SAME internal aidream
    route the create path uses. Returns ``(env, error)``; exactly one is real."""
    url = settings.resolve_aidream_url()
    token = settings.resolve_aidream_service_token()
    if not url:
        return {}, "aidream URL unavailable on this orchestrator (MATRX_AIDREAM_URL)"
    if not token:
        return {}, "aidream service token unavailable on this orchestrator (MATRX_AIDREAM_SERVICE_TOKEN)"
    try:
        headers = identity_headers(
            token=token,
            user_id=str(sandbox.user_id),
            organization_id=str(sandbox.organization_id),
            extra={"User-Agent": "matrx-sandbox-orchestrator"},
        )
    except BridgeIdentityMissing as exc:
        return {}, str(exc)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as hx:
            resp = await hx.get(
                f"{url.rstrip('/')}/api/user-secrets/internal/sandbox-env-for-user",
                params={"organization_id": str(sandbox.organization_id)},
                headers=headers,
            )
    except Exception as exc:  # noqa: BLE001 — network boundary
        return {}, f"{type(exc).__name__} calling aidream: {exc}"
    if resp.status_code != 200:
        return {}, f"HTTP {resp.status_code} from aidream: {(resp.text or '')[:300]}"
    body = resp.json() or {}
    fetched = body.get("env") if isinstance(body, dict) else None
    if not isinstance(fetched, dict):
        return {}, "aidream returned no 'env' object"
    return (
        {k: v for k, v in fetched.items() if isinstance(k, str) and isinstance(v, str)},
        None,
    )


async def _write_env_files(
    *,
    container,
    sandbox_id: str,
    env: dict[str, str],
    identity: dict[str, str],
    version: str,
    removed: list[str],
) -> dict:
    """Put the three files in the box, then wire the two shells that need a line.

    ``put_archive`` rather than an exec heredoc: the values never appear on a
    command line, in ``ps``, or in the exec log.
    """
    payload = _tar(
        {
            ENV_FILE.lstrip("/"): (render_env_file(env, version=version, removed=removed), 0o640),
            PROFILE_DROPIN.lstrip("/"): (render_profile_dropin(), 0o644),
            BRIDGE_ENV_FILE.lstrip("/"): (render_identity_file(identity), 0o640),
            BRIDGE_PROFILE_DROPIN.lstrip("/"): (render_bridge_dropin(), 0o644),
            STAMP_FILE.lstrip("/"): (
                json.dumps(
                    {
                        "vault_version": version,
                        "names": sorted(env),
                        "identity": sorted(identity),
                        "removed": removed,
                        "written_at": time.time(),
                    },
                    indent=2,
                )
                + "\n",
                0o640,
            ),
        }
    )
    # /etc/matrx may not exist on a box older than write-bridge-env.sh.
    await sandbox_manager.exec_in_sandbox(
        sandbox_id=sandbox_id,
        command=f"mkdir -p {ENV_DIR} /etc/profile.d",
        timeout=WRITE_TIMEOUT_SECONDS,
        user="root",
        cwd="/",
    )
    if not await asyncio.to_thread(container.put_archive, "/", payload):
        raise RuntimeError("docker refused to unpack the vault env files into /")

    # Ownership + the one source line in the agent's home env file. Names are
    # safe to put on a command line; values are not, and none are here.
    wire = (
        f"chown root:agent {ENV_FILE} {STAMP_FILE} {BRIDGE_ENV_FILE} 2>/dev/null || true; "
        f"chmod 0640 {ENV_FILE} {STAMP_FILE} {BRIDGE_ENV_FILE}; "
        # A stale FAILED marker makes bridge-headers.sh scream on a box that is
        # now perfectly wired. We just wired it; the marker is no longer true.
        f"rm -f {BRIDGE_FAILED_FILE}; "
        f"touch {SANDBOX_ENV_FILE}; "
        f"chown agent:agent {SANDBOX_ENV_FILE} 2>/dev/null || true; "
        # Identity BEFORE vault: a vault value the person named USER_ID would
        # otherwise be silently overwritten by the identity line after it.
        f"grep -qF {shlex.quote(BRIDGE_ENV_FILE)} {SANDBOX_ENV_FILE} || "
        f"printf '%s\\n' {shlex.quote(f'[ -r {BRIDGE_ENV_FILE} ] && . {BRIDGE_ENV_FILE}')} >> {SANDBOX_ENV_FILE}; "
        f"grep -qF {shlex.quote(ENV_FILE)} {SANDBOX_ENV_FILE} || "
        f"printf '%s\\n' {shlex.quote(f'[ -r {ENV_FILE} ] && . {ENV_FILE}')} >> {SANDBOX_ENV_FILE}; "
        f"grep -q '.sandbox_env' /home/agent/.bashrc 2>/dev/null || "
        f"echo '[ -f ~/.sandbox_env ] && source ~/.sandbox_env' >> /home/agent/.bashrc"
    )
    exit_code, _stdout, stderr, _ = await sandbox_manager.exec_in_sandbox(
        sandbox_id=sandbox_id,
        command=wire,
        timeout=WRITE_TIMEOUT_SECONDS,
        user="root",
        cwd="/",
    )
    if exit_code != 0:
        raise RuntimeError(
            f"vault env files landed but could not be wired into the shells "
            f"(exit={exit_code}): {(stderr or '')[-400:]}"
        )
    return {
        "env_file": ENV_FILE,
        "profile_dropin": PROFILE_DROPIN,
        "identity_file": BRIDGE_ENV_FILE,
    }


def _tar(files: dict[str, tuple[str, int]]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, (text, mode) in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


async def _read_stamp(sandbox_id: str) -> dict:
    exit_code, stdout, _stderr, _ = await sandbox_manager.exec_in_sandbox(
        sandbox_id=sandbox_id,
        command=f"cat {STAMP_FILE} 2>/dev/null || true",
        timeout=20,
        user="root",
        cwd="/",
    )
    if exit_code != 0 or not stdout:
        return {}
    start = stdout.find("{")
    while start != -1:
        try:
            value = json.loads(stdout[start:].strip())
        except ValueError:
            start = stdout.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else {}
    return {}


@contextlib.contextmanager
def _preserved_cwd(sandbox_id: str):
    """The exec helper caches the directory each call lands in, and that cache
    is the user's shell location. A background refresh must not move it."""
    prior = sandbox_manager._sandbox_cwd.get(sandbox_id)
    try:
        yield
    finally:
        if prior is None:
            sandbox_manager._sandbox_cwd.pop(sandbox_id, None)
        else:
            sandbox_manager._sandbox_cwd[sandbox_id] = prior


def clear_caches() -> None:
    """Tests, and anything that just changed the knob."""
    _recent.clear()
    _locks.clear()
