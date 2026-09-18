"""Binding-time runtime refresh — every box inherits /opt/sandbox updates, alive.

THE GAP (review row ca931876, 2026-09-14). The ``matrx_agent`` SDK — the ``mtx``
CLI and the in-container daemon — is baked into the image, and an existing box is
never force-migrated (SBX-006). A box created before commit 8a1680a therefore had
no way to ever run ``mtx toolchain ensure``, and the prompt carried a
three-command shell fallback to paper over it.

THE PRIMITIVE. The orchestrator already knows the current image for a template
(versioning.current_image) and already holds the Docker socket. So at binding
time — the one moment an agent is about to use the box — it compares the SDK the
box is carrying with the current image's, and when the box is behind it stages
the current ``/opt/sandbox/sdk`` tree INTO the running container and runs the
staged tree's own installer (matrx_agent/selfupdate.py). The box gains the new
commands without being recreated and without one byte written to ``/home/agent``.

WHAT IT IS NOT. This is not migration. The container keeps running its old image:
``/etc/sandbox-image-version`` is untouched and ``/drift`` still reports the box
as stale, because it IS. This closes the *tooling* half of drift, which is the
half users hit; the image half stays the explicit lifecycle action it was.

SAFETY.
* **Never fatal.** Every failure becomes a status on the binding diagnostics and
  a loud log line. The token is minted either way.
* **Rate limited** to once per box per image version, by a stamp written inside
  the container (``/opt/sandbox/sdk/.matrx-sdk-refresh``) plus a process cache —
  so a box that is up to date costs zero execs, and a box that just refreshed
  costs one ``cat``.
* **A knob**, ``infrastructure.sandbox.sdk_refresh_on_binding`` (default on).
* **PTYs.** The daemon holds every PTY session in its own process, so it is
  restarted only when the daemon's code actually changed AND no PTY/watch
  attachment is open on this box (activity.open_session_count). Otherwise the
  restart is deferred and said out loud; the CLI half is live immediately
  because ``mtx`` is a fresh process on every invocation.
* **Never during a migration.** A fenced box is left alone.

WHY IT DELIVERS ``scripts`` TOO (2026-09-18). Until now this refreshed ONLY
``/opt/sandbox/sdk``. ``/opt/sandbox/scripts`` — the git credential helper, the
bridge-header builder, ``write-bridge-env.sh``, ``configure-git-credentials.sh``
— stayed frozen at the box's birthday. Admin's box sbx-cd6d53863995, checked
live on 2026-09-18, had an SDK tree from Sep 18 sitting next to a scripts tree
from Aug 15: a 925-byte credential helper that read ``$GITHUB_PAT`` out of the
environment and had never heard of the AI Dream bridge, and no ``/etc/matrx`` at
all. Every fix shipped to that helper in the last month reached new boxes only.
The scripts are plain files with no import graph and no daemon to restart, so
they are staged and swapped the same way — and when the credential helper or the
bridge-env writer actually CHANGED, the two idempotent scripts that install
their effects are re-run, so a month-old box ends the refresh with the live
helper wired into ``~/.gitconfig`` and its identity published to ``/etc/matrx``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import tarfile
import time

from orchestrator import activity, sandbox_manager, versioning
from orchestrator.knobs import (
    KnobNotRegisteredError,
    KnobSourceUnavailableError,
    knob_bool,
)
from orchestrator.models import SandboxResponse

logger = logging.getLogger(__name__)

KNOB = "sdk_refresh_on_binding"
SDK_PATH = "/opt/sandbox/sdk"
STAGE_DIR = "sdk.incoming"
STAGE_PATH = f"/opt/sandbox/{STAGE_DIR}"
OPT_SANDBOX = "/opt/sandbox"
SCRIPTS_PATH = "/opt/sandbox/scripts"
SCRIPTS_STAGE_DIR = "scripts.incoming"
SCRIPTS_STAGE_PATH = f"/opt/sandbox/{SCRIPTS_STAGE_DIR}"
#: Scripts whose CONTENT changing means an install step has to run again. Each
#: is idempotent and safe to re-run on a live box.
SCRIPT_REINSTALL: tuple[tuple[str, str, str], ...] = (
    (
        "matrx-git-credential-env",
        f"sudo -H -u agent {SCRIPTS_PATH}/configure-git-credentials.sh",
        "the git credential helper changed",
    ),
    (
        "configure-git-credentials.sh",
        f"sudo -H -u agent {SCRIPTS_PATH}/configure-git-credentials.sh",
        "the credential configuration changed",
    ),
    (
        "write-bridge-env.sh",
        f"{SCRIPTS_PATH}/write-bridge-env.sh",
        "the identity publisher changed",
    ),
)
#: The install itself is seconds; the budget is for the worst case — a daemon
#: restart that waits out a slow start (60s) AND a rollback restart after it.
INSTALL_TIMEOUT = 240
#: Guardrail on the staged payload — the SDK tree is a few MB; anything near
#: this means we are about to copy the wrong path into a user's box.
MAX_STAGE_BYTES = 256 * 1024 * 1024

_locks: dict[str, asyncio.Lock] = {}
#: (sandbox_id -> (image_id, monotonic)) — a box already refreshed to this image
#: in this process is not re-checked for the TTL, so repeated bindings are free.
_recent: dict[str, tuple[str, float]] = {}
_RECENT_TTL = 900.0


@contextlib.contextmanager
def _preserved_cwd(sandbox_id: str):
    """The exec helper caches the working directory each call lands in, and that
    cache is the user's shell location. A background tooling update must not
    move the agent's next command to /opt/sandbox."""
    prior = sandbox_manager._sandbox_cwd.get(sandbox_id)
    try:
        yield
    finally:
        if prior is None:
            sandbox_manager._sandbox_cwd.pop(sandbox_id, None)
        else:
            sandbox_manager._sandbox_cwd[sandbox_id] = prior


def _result(status: str, **extra) -> dict:
    out = {"hook": "session_start.sdk_refresh", "status": status, "from": None, "to": None}
    out.update(extra)
    return out


async def refresh_sdk_if_stale(sandbox: SandboxResponse) -> dict:
    """The connection hook. Always returns a dict; never raises."""
    sandbox_id = sandbox.sandbox_id
    try:
        enabled = await knob_bool(KNOB)
    except (KnobNotRegisteredError, KnobSourceUnavailableError) as exc:
        logger.warning(
            "SDK REFRESH UNAVAILABLE for %s: %s. Seed the platform.feature_knob row "
            "'infrastructure.sandbox.%s' — until then a box created from an older "
            "image keeps the tools it was born with.",
            sandbox_id, exc, KNOB,
        )
        return _result("unavailable", reason=f"knob {KNOB} unreadable: {exc}")
    if not enabled:
        return _result("disabled", reason=f"infrastructure.sandbox.{KNOB} is off")

    if activity.is_migrating(sandbox_id):
        return _result("skipped", reason="the box is migrating; the new image carries the SDK anyway")

    lock = _locks.setdefault(sandbox_id, asyncio.Lock())
    async with lock:
        try:
            return await _refresh(sandbox)
        except Exception as exc:  # noqa: BLE001 — never fatal to a binding
            logger.exception("SDK REFRESH FAILED for %s", sandbox_id)
            return _result("failed", reason=f"{type(exc).__name__}: {exc}")


async def _refresh(sandbox: SandboxResponse) -> dict:
    sandbox_id = sandbox.sandbox_id
    client = sandbox_manager._get_docker_client()
    container = await asyncio.to_thread(client.containers.get, sandbox.container_id or sandbox_id)
    await asyncio.to_thread(container.reload)
    if container.status != "running":
        return _result("skipped", reason=f"container is {container.status}")

    current = await asyncio.to_thread(versioning.current_image, client, sandbox.template)
    if not current.available or not current.image_id:
        return _result(
            "skipped",
            reason=f"current image {current.tag!r} is not present on this host — nothing to compare",
        )
    running_image_id = (getattr(container, "attrs", None) or {}).get("Image")
    box_version = versioning._version_from_container(container) or "unknown"
    to_version = current.version or versioning.UNVERSIONED

    if running_image_id == current.image_id:
        return _result("current", **{"from": box_version, "to": to_version})

    cached = _recent.get(sandbox_id)
    if cached and cached[0] == current.image_id and time.monotonic() - cached[1] < _RECENT_TTL:
        return _result("already_refreshed", **{"from": box_version, "to": to_version, "cached": True})

    with _preserved_cwd(sandbox_id):
        async with activity.track(sandbox_id):
            # The scripts half runs FIRST and on its own stamp. A box whose SDK
            # was already refreshed by the pre-2026-09-18 code would otherwise
            # short-circuit below and keep its birthday scripts forever — which
            # is exactly the state admin's box was found in.
            scripts = await _refresh_scripts(
                client=client,
                container=container,
                sandbox_id=sandbox_id,
                image_tag=current.tag,
                image_id=current.image_id,
            )

            stamp = await _read_stamp(sandbox_id)
            if stamp.get("to_image_id") == current.image_id:
                _recent[sandbox_id] = (current.image_id, time.monotonic())
                return _result(
                    "already_refreshed",
                    scripts=scripts,
                    **{"from": stamp.get("to_version") or box_version, "to": to_version},
                )
            from_version = stamp.get("to_version") or box_version

            payload = await asyncio.to_thread(
                _stage_payload, client, current.tag, SDK_PATH, STAGE_DIR
            )
            await asyncio.to_thread(_put_payload, container, payload)

            allow_restart = activity.open_session_count(sandbox_id) == 0
            command = (
                f"python3 {STAGE_PATH}/matrx_agent/selfupdate.py "
                f"--source {STAGE_PATH} --target {SDK_PATH} "
                f"--image-id {current.image_id} --image-version {to_version}"
            )
            if allow_restart:
                command += " --allow-daemon-restart"
            command += f"; rc=$?; rm -rf {STAGE_PATH}; exit $rc"
            exit_code, stdout, stderr, _ = await sandbox_manager.exec_in_sandbox(
                sandbox_id=sandbox_id,
                command=command,
                timeout=INSTALL_TIMEOUT,
                user="root",
                cwd=OPT_SANDBOX,
            )

    installed = _parse_json(stdout)
    if exit_code != 0 or not installed:
        logger.error(
            "SDK REFRESH FAILED for %s (exit=%s): %s %s",
            sandbox_id, exit_code, (stdout or "")[-2000:], (stderr or "")[-2000:],
        )
        return _result(
            "failed",
            scripts=scripts,
            **{
                "from": from_version,
                "to": to_version,
                "exit_code": exit_code,
                "reason": ((stderr or stdout or "installer produced no report").strip())[-600:],
            },
        )

    _recent[sandbox_id] = (current.image_id, time.monotonic())
    daemon = installed.get("daemon_restart") or {}
    result = _result(
        installed.get("status") or "failed",
        scripts=scripts,
        **{
            "from": from_version,
            "to": to_version,
            "daemon_restart": str(daemon.get("status", "unknown")),
            "daemon_restart_reason": (str(daemon.get("reason")) if daemon.get("reason") else None),
            "deps_added": list((installed.get("deps_checked") or {}).get("added") or []),
            "mtx_shim": str(installed.get("mtx_shim", "")) or None,
            "elapsed_seconds": installed.get("elapsed_seconds"),
            "rollback": (
                str((installed.get("rollback") or {}).get("status"))
                if installed.get("rollback")
                else None
            ),
        },
    )
    if result["status"] == "rolled_back":
        logger.error(
            "SDK REFRESH ROLLED BACK on %s: the current SDK's daemon would not start, so the "
            "box was restored to %s (rollback=%s). This box needs a real image migration.",
            sandbox_id, from_version, result["rollback"],
        )
    if result["deps_added"]:
        logger.warning(
            "SDK REFRESH on %s installed a tree that declares NEW dependencies %s — the file "
            "copy cannot install them; this box needs a real image migration for those.",
            sandbox_id, result["deps_added"],
        )
    logger.info(
        "SDK refresh on %s: %s (%s -> %s, daemon=%s)",
        sandbox_id, result["status"], from_version, to_version, result["daemon_restart"],
    )
    return result


SCRIPTS_STAMP = f"{SCRIPTS_PATH}/.matrx-scripts-refresh"
#: Bump when the delivery contract changes, so boxes stamped by an older
#: version of THIS code are refreshed once more rather than trusted forever.
SCRIPTS_CONTRACT = 1


async def _refresh_scripts(
    *, client, container, sandbox_id: str, image_tag: str, image_id: str
) -> dict:
    """Deliver ``/opt/sandbox/scripts`` from the current image into a live box.

    Never raises: a scripts failure is a status, exactly like the SDK half.
    """
    try:
        stamp = _parse_json(
            (
                await sandbox_manager.exec_in_sandbox(
                    sandbox_id=sandbox_id,
                    command=f"cat {SCRIPTS_STAMP} 2>/dev/null || true",
                    timeout=20,
                    user="root",
                    cwd=OPT_SANDBOX,
                )
            )[1]
        )
        if (
            stamp.get("to_image_id") == image_id
            and stamp.get("contract") == SCRIPTS_CONTRACT
        ):
            return {"status": "already_refreshed", "changed": []}

        before = await _script_digests(sandbox_id)
        payload = await asyncio.to_thread(
            _stage_payload, client, image_tag, SCRIPTS_PATH, SCRIPTS_STAGE_DIR
        )
        await asyncio.to_thread(_put_payload, container, payload)

        # Swap in place: the staged tree becomes the live tree file by file.
        # `cp -a` rather than a directory rename, so a script a box added
        # locally is not deleted and nothing holding the directory breaks.
        swap = (
            f"cp -a {SCRIPTS_STAGE_PATH}/. {SCRIPTS_PATH}/ && "
            f"chmod 0755 {SCRIPTS_PATH}/* 2>/dev/null; "
            f"rc=$?; rm -rf {SCRIPTS_STAGE_PATH}; exit 0"
        )
        exit_code, _out, err, _ = await sandbox_manager.exec_in_sandbox(
            sandbox_id=sandbox_id, command=swap, timeout=60, user="root", cwd=OPT_SANDBOX
        )
        if exit_code != 0:
            return {
                "status": "failed",
                "changed": [],
                "reason": (err or "the scripts swap failed").strip()[-400:],
            }
        after = await _script_digests(sandbox_id)
        changed = sorted(
            name for name in set(before) | set(after) if before.get(name) != after.get(name)
        )

        reinstalled: list[str] = []
        for script, command, why in SCRIPT_REINSTALL:
            if script not in changed:
                continue
            rc, _o, rerr, _ = await sandbox_manager.exec_in_sandbox(
                sandbox_id=sandbox_id, command=command, timeout=60, user="root", cwd=OPT_SANDBOX
            )
            reinstalled.append(script if rc == 0 else f"{script} (FAILED: {why})")
            if rc != 0:
                logger.warning(
                    "SCRIPTS REFRESH on %s: %s, but re-running %r failed (exit=%s): %s",
                    sandbox_id, why, command, rc, (rerr or "")[-300:],
                )

        await sandbox_manager.exec_in_sandbox(
            sandbox_id=sandbox_id,
            command=(
                f"cat > {SCRIPTS_STAMP} <<'MATRXSTAMP'\n"
                + json.dumps(
                    {
                        "to_image_id": image_id,
                        "contract": SCRIPTS_CONTRACT,
                        "changed": changed,
                        "at": time.time(),
                    }
                )
                + "\nMATRXSTAMP"
            ),
            timeout=20,
            user="root",
            cwd=OPT_SANDBOX,
        )
        if changed:
            logger.info(
                "SCRIPTS REFRESH on %s: %d file(s) updated (%s); re-ran %s",
                sandbox_id, len(changed), ", ".join(changed[:10]), reinstalled or "nothing",
            )
        return {
            "status": "refreshed",
            "changed": changed,
            "reinstalled": reinstalled,
        }
    except Exception as exc:  # noqa: BLE001 — never fatal to a binding
        logger.exception("SCRIPTS REFRESH FAILED for %s", sandbox_id)
        return {"status": "failed", "changed": [], "reason": f"{type(exc).__name__}: {exc}"}


async def _script_digests(sandbox_id: str) -> dict[str, str]:
    """name -> sha256, for every file in the box's scripts directory."""
    _rc, out, _err, _ = await sandbox_manager.exec_in_sandbox(
        sandbox_id=sandbox_id,
        command=(
            f"find {SCRIPTS_PATH} -maxdepth 1 -type f -printf '%f\\n' 2>/dev/null "
            f"| while read -r f; do printf '%s %s\\n' \"$f\" "
            f"\"$(sha256sum {SCRIPTS_PATH}/\"$f\" | cut -d' ' -f1)\"; done"
        ),
        timeout=30,
        user="root",
        cwd=OPT_SANDBOX,
    )
    digests: dict[str, str] = {}
    for line in (out or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            digests[parts[0]] = parts[1]
    return digests


async def _read_stamp(sandbox_id: str) -> dict:
    exit_code, stdout, _stderr, _ = await sandbox_manager.exec_in_sandbox(
        sandbox_id=sandbox_id,
        command=f"cat {SDK_PATH}/.matrx-sdk-refresh 2>/dev/null || true",
        timeout=20,
        user="root",
        cwd=OPT_SANDBOX,
    )
    if exit_code != 0:
        return {}
    return _parse_json(stdout)


def _parse_json(text: str | None) -> dict:
    """The exec wrapper can prepend shell noise; take the last JSON object."""
    if not text:
        return {}
    start = text.find("{")
    while start != -1:
        try:
            value = json.loads(text[start:].strip())
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else {}
    return {}


def _stage_payload(
    client,
    image_tag: str,
    source_path: str = SDK_PATH,
    stage_dir: str = STAGE_DIR,
) -> bytes:
    """Pull ``source_path`` out of the CURRENT image as a tar whose members are
    rewritten to ``<stage_dir>/`` — so unpacking it in a live box can never land
    on top of the tree the box is currently using."""
    holder = client.containers.create(image_tag, command="/bin/true")
    try:
        stream, _stat = holder.get_archive(source_path)
        raw = io.BytesIO()
        size = 0
        for chunk in stream:
            size += len(chunk)
            if size > MAX_STAGE_BYTES:
                raise RuntimeError(
                    f"refusing to stage {size} bytes from {image_tag}:{source_path} — "
                    f"over the {MAX_STAGE_BYTES} byte guardrail"
                )
            raw.write(chunk)
        raw.seek(0)
    finally:
        try:
            holder.remove(force=True)
        except Exception:  # noqa: BLE001 — a leaked /bin/true container is not worth failing a binding
            logger.warning("could not remove SDK staging container for %s", image_tag)

    out = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="r|*") as src, tarfile.open(fileobj=out, mode="w") as dst:
        for member in src:
            # Docker names the archive after its root path ("sdk", "sdk/..."),
            # and tarfile strips a directory's trailing slash. Rewrite the first
            # segment whatever it is called, so the payload can only ever unpack
            # into the staging directory.
            name = member.name.lstrip("./")
            if not name:
                continue
            head, _, rest = name.partition("/")
            member.name = f"{stage_dir}/{rest}" if rest else stage_dir
            if member.islnk() and member.linkname.startswith(f"{head}/"):
                member.linkname = f"{stage_dir}/{member.linkname[len(head) + 1:]}"
            if member.isfile():
                dst.addfile(member, src.extractfile(member))
            else:
                dst.addfile(member)
    return out.getvalue()


def _put_payload(container, payload: bytes) -> None:
    if not container.put_archive(OPT_SANDBOX, payload):
        raise RuntimeError(f"docker refused to unpack the staged SDK into {OPT_SANDBOX}")


def clear_caches() -> None:
    """Tests, and anything that just changed the knob."""
    _recent.clear()
    _locks.clear()
