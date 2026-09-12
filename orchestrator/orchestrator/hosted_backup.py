"""Verified, metadata-preserving snapshots for hosted Docker volumes.

This module deliberately has no lifecycle policy: callers reserve the backup
volume and serialize access.  It only makes an immutable helper container copy
one volume and proves the resulting backup before returning a receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from docker.errors import ContainerError, DockerException, NotFound


_SCHEMA_VERSION = 2
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESULT_PREFIX = "HOSTED_BACKUP_RESULT="
_HELPER_LABELS = {
    "matrx.kind": "hosted-volume-backup-helper",
    "matrx.owner": "orchestrator",
}


class HostedBackupError(RuntimeError):
    """A snapshot or restore was refused or could not be verified."""


def _require_image_id(image: str) -> None:
    if not isinstance(image, str) or not _IMAGE_ID.fullmatch(image):
        raise HostedBackupError("helper image must be an immutable sha256 image ID")


def _require_volume_name(name: str, field: str) -> None:
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise HostedBackupError(f"{field} must be one exact Docker volume name")


def _volume_identity(volume: Any) -> dict[str, Any]:
    """Return stable, non-content metadata used to reject volume substitution."""
    attrs = getattr(volume, "attrs", None) or {}
    name = attrs.get("Name") or getattr(volume, "name", None)
    if not isinstance(name, str) or not name:
        raise HostedBackupError("Docker did not return a volume identity")
    # Mountpoint is intentionally excluded: it is host-private operational data.
    return {
        "name": name,
        "driver": attrs.get("Driver"),
        "scope": attrs.get("Scope"),
        "created_at": attrs.get("CreatedAt"),
        "labels": attrs.get("Labels") or {},
    }


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


_MANIFEST_PROGRAM = r'''import base64, errno, hashlib, json, os, stat, subprocess, sys
root = sys.argv[1]
if not os.path.isdir(root): raise SystemExit("manifest root is absent")
if not os.path.exists("/usr/bin/getfacl") and not os.path.exists("/bin/getfacl"):
    raise SystemExit("getfacl is required to verify ACL preservation")
def sparse(path, size):
    if not size: return []
    out=[]; pos=0
    fd=os.open(path, os.O_RDONLY)
    try:
        while pos < size:
            data=os.lseek(fd, pos, os.SEEK_DATA)
            hole=os.lseek(fd, data, os.SEEK_HOLE)
            out.append([data, min(hole,size)]); pos=hole
    except OSError as e:
        if e.errno == errno.ENXIO: return out  # trailing hole, including all-sparse files
        # Filesystems without SEEK_HOLE cannot prove sparse preservation.
        raise SystemExit("filesystem cannot report sparse extents: %s" % e)
    finally:
        os.close(fd)
    return out
def digest(path):
    h=hashlib.sha256()
    with open(path,"rb", buffering=0) as f:
        for chunk in iter(lambda:f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()
def acl(path):
    p=subprocess.run(["getfacl","-P","-cpn","--",path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode: raise SystemExit("getfacl failed for %s" % path)
    return p.stdout.decode("utf-8", "surrogateescape")
entries=[]; inodes={}
paths=[root]
for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
    dirs.sort(); files.sort()
    paths.extend(os.path.join(base, name) for name in dirs + files)
for path in paths:
        rel=os.path.relpath(path,root)
        st=os.lstat(path); item={"path":rel,"uid":st.st_uid,"gid":st.st_gid,"mode":stat.S_IMODE(st.st_mode),"acl":acl(path),"xattrs":{n:base64.b64encode(os.getxattr(path,n,follow_symlinks=False)).decode() for n in sorted(os.listxattr(path,follow_symlinks=False))}}
        if stat.S_ISLNK(st.st_mode): item.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISREG(st.st_mode):
            key=(st.st_dev,st.st_ino); inodes.setdefault(key,[]).append(rel)
            item.update(type="file", size=st.st_size, sha256=digest(path), sparse=sparse(path,st.st_size))
        elif stat.S_ISDIR(st.st_mode): item.update(type="directory")
        else: raise SystemExit("unsupported file type: %s" % rel)
        entries.append(item)
groups=sorted(sorted(v) for v in inodes.values() if len(v)>1)
print(json.dumps({"entries":entries,"hardlink_groups":groups},sort_keys=True,separators=(",",":")))
'''


def _shell_program(body: str) -> str:
    """Run a checked shell body; no pipeline masks a producer's exit status."""
    return "set -eu\n" + body


def _manifest_command(path: str) -> str:
    encoded = base64_encode(_MANIFEST_PROGRAM)
    return f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\" {path}"


def base64_encode(value: str) -> str:
    import base64
    return base64.b64encode(value.encode()).decode()


async def _get_volume(client: Any, name: str) -> tuple[Any, dict[str, Any]]:
    try:
        volume = await asyncio.to_thread(client.volumes.get, name)
    except NotFound as exc:
        raise HostedBackupError(f"required volume {name!r} is missing") from exc
    except DockerException as exc:
        raise HostedBackupError(f"could not inspect volume {name!r}") from exc
    identity = _volume_identity(volume)
    if identity["name"] != name:
        raise HostedBackupError("Docker returned a different volume than requested")
    return volume, identity


async def _run_helper(client: Any, *, image: str, volumes: dict[str, dict[str, str]], command: str) -> str:
    try:
        output = await asyncio.to_thread(
            client.containers.run,
            image,
            # Docker splits a string command into argv tokens. ``sh -c`` needs
            # the entire checked program as exactly one argument.
            command=[command],
            entrypoint=["/bin/sh", "-ec"],
            volumes=volumes,
            network_disabled=True,
            environment={},
            labels=_HELPER_LABELS,
            remove=True,
            detach=False,
        )
    except ContainerError as exc:
        raise HostedBackupError(f"backup helper exited {exc.exit_status}") from exc
    except DockerException as exc:
        raise HostedBackupError("backup helper could not be run") from exc
    if isinstance(output, bytes):
        return output.decode("utf-8", "surrogateescape")
    return str(output)


def _read_manifest(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.startswith(_RESULT_PREFIX)]
    if len(lines) != 1:
        raise HostedBackupError("helper did not emit one complete manifest")
    try:
        manifest = json.loads(lines[0][len(_RESULT_PREFIX):])
    except json.JSONDecodeError as exc:
        raise HostedBackupError("helper emitted a corrupt manifest") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        raise HostedBackupError("helper emitted an incomplete manifest")
    return manifest


def _receipt_manifest(receipt: dict[str, Any], key: str) -> dict[str, Any]:
    value = receipt.get(key)
    digest = receipt.get(f"{key}_sha256")
    if not isinstance(value, dict) or not isinstance(digest, str) or _canonical_digest(value) != digest:
        raise HostedBackupError(f"receipt {key} is absent or corrupt")
    return value


def _archive_digest(output: str) -> str:
    lines = [line.removeprefix("HOSTED_BACKUP_ARCHIVE=") for line in output.splitlines()
             if line.startswith("HOSTED_BACKUP_ARCHIVE=")]
    if len(lines) != 1 or not re.fullmatch(r"[0-9a-f]{64}(?:\s+.*)?", lines[0]):
        raise HostedBackupError("backup archive digest is absent or corrupt")
    return lines[0].split()[0]


def _validate_receipt(receipt: dict[str, Any], image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != _SCHEMA_VERSION:
        raise HostedBackupError("receipt has an unsupported schema")
    if receipt.get("helper_image") != image:
        raise HostedBackupError("receipt was made with a different helper image")
    if not isinstance(receipt.get("archive_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["archive_sha256"]):
        raise HostedBackupError("receipt archive digest is absent or corrupt")
    source = receipt.get("source_volume")
    backup = receipt.get("backup_volume")
    if not isinstance(source, dict) or not isinstance(backup, dict):
        raise HostedBackupError("receipt has no immutable volume identities")
    _require_volume_name(source.get("name"), "receipt source volume")
    _require_volume_name(backup.get("name"), "receipt backup volume")
    if source["name"] == backup["name"]:
        raise HostedBackupError("receipt source and backup volumes must differ")
    return _receipt_manifest(receipt, "source_manifest"), _receipt_manifest(receipt, "backup_manifest")


async def snapshot_volume(client: Any, *, volume: str, backup_volume: str, image: str) -> dict[str, Any]:
    """Copy ``volume`` into a fresh reserved volume and return a verified receipt."""
    _require_image_id(image); _require_volume_name(volume, "source volume"); _require_volume_name(backup_volume, "backup volume")
    if volume == backup_volume:
        raise HostedBackupError("source and backup volumes must differ")
    _, source_identity = await _get_volume(client, volume)
    _, backup_identity = await _get_volume(client, backup_volume)
    copy = _shell_program("""
python3 -c 'import os,sys; sys.exit("reserved backup volume is not empty" if os.listdir("/backup") else 0)'
mkdir /backup/payload
tar --create --format=posix --file=/backup/archive.tar --numeric-owner --xattrs --acls --sparse -C /source .
tar --extract --file=/backup/archive.tar --numeric-owner --xattrs --xattrs-include='*' --acls --sparse -C /backup/payload
printf 'HOSTED_BACKUP_SOURCE='
""" + _manifest_command("/source") + "\nprintf '\\nHOSTED_BACKUP_BACKUP='\n" + _manifest_command("/backup/payload")
        + "\nprintf '\\nHOSTED_BACKUP_ARCHIVE='\nsha256sum -- /backup/archive.tar\nsync -f /backup\n")
    output = await _run_helper(client, image=image, volumes={volume: {"bind": "/source", "mode": "ro"}, backup_volume: {"bind": "/backup", "mode": "rw"}}, command=copy)
    # The copy command emits two JSON values; parse them independently rather than
    # accepting a last-stage-only pipeline result.
    source_lines = [line[len("HOSTED_BACKUP_SOURCE="):] for line in output.splitlines() if line.startswith("HOSTED_BACKUP_SOURCE=")]
    backup_lines = [line[len("HOSTED_BACKUP_BACKUP="):] for line in output.splitlines() if line.startswith("HOSTED_BACKUP_BACKUP=")]
    try:
        if len(source_lines) != 1 or len(backup_lines) != 1:
            raise ValueError("missing manifest")
        source_manifest = json.loads(source_lines[0])
        backup_manifest = json.loads(backup_lines[0])
    except (ValueError, json.JSONDecodeError) as exc:
        raise HostedBackupError("helper did not emit complete source and backup manifests") from exc
    if source_manifest != backup_manifest:
        raise HostedBackupError("backup manifest does not agree with source manifest")
    return {
        "schema_version": _SCHEMA_VERSION,
        "helper_image": image,
        "archive_sha256": _archive_digest(output),
        "source_volume": source_identity,
        "backup_volume": backup_identity,
        "source_manifest": source_manifest,
        "source_manifest_sha256": _canonical_digest(source_manifest),
        "backup_manifest": backup_manifest,
        "backup_manifest_sha256": _canonical_digest(backup_manifest),
        # ``remove=True`` is Docker's synchronous --rm equivalent for this
        # attached helper invocation: the call returns only after exit/removal.
        "helper_cleanup": {"container_auto_removed": True, "labels": _HELPER_LABELS},
    }


async def verify_volume_unchanged(client: Any, *, receipt: dict, image: str) -> dict[str, Any]:
    """Prove a retained named home still equals its immutable backup manifest.

    This is intentionally a read-only helper invocation.  A migration target is
    held/paused before this is called, so this receipt is the pre-CAS and
    rollback proof that neither image has changed the shared user home.
    """
    _require_image_id(image)
    source_manifest, backup_manifest = _validate_receipt(receipt, image)
    if source_manifest != backup_manifest:
        raise HostedBackupError("backup source and payload manifests disagree")
    source_name = receipt["source_volume"]["name"]
    _, source_identity = await _get_volume(client, source_name)
    if source_identity != receipt["source_volume"]:
        raise HostedBackupError("source volume identity no longer matches Docker")
    output = await _run_helper(
        client,
        image=image,
        volumes={source_name: {"bind": "/source", "mode": "ro"}},
        command=_shell_program("printf '" + _RESULT_PREFIX + "'\n" + _manifest_command("/source") + "\nprintf '\\n'"),
    )
    current = _read_manifest(output)
    if current != source_manifest:
        raise HostedBackupError("shared home manifest changed while migration target was held")
    return {
        "source_volume": source_identity,
        "manifest_sha256": receipt["source_manifest_sha256"],
    }


async def restore_volume(client: Any, *, receipt: dict, image: str) -> None:
    """Restore only a previously verified backup; validate before touching target."""
    _require_image_id(image)
    source_manifest, backup_manifest = _validate_receipt(receipt, image)
    source_name = receipt["source_volume"]["name"]; backup_name = receipt["backup_volume"]["name"]
    _, current_source = await _get_volume(client, source_name)
    _, current_backup = await _get_volume(client, backup_name)
    if current_source != receipt["source_volume"] or current_backup != receipt["backup_volume"]:
        raise HostedBackupError("receipt volume identity no longer matches Docker")
    # This entire helper invocation is read-only. A missing/corrupt backup cannot
    # reach the destructive invocation below.
    checked = await _run_helper(client, image=image, volumes={backup_name: {"bind": "/backup", "mode": "ro"}}, command=_shell_program("printf '" + _RESULT_PREFIX + "'\n" + _manifest_command("/backup/payload") + "\nprintf '\\nHOSTED_BACKUP_ARCHIVE='\nsha256sum -- /backup/archive.tar\n"))
    actual_backup = _read_manifest(checked)
    if actual_backup != backup_manifest or source_manifest != backup_manifest:
        raise HostedBackupError("backup is corrupt or does not match its receipt")
    if _archive_digest(checked) != receipt["archive_sha256"]:
        raise HostedBackupError("backup archive does not match its verified receipt")
    # Check the exact extraction input again inside the write helper before
    # deleting anything. The read-only payload is not the archive we extract.
    restore = _shell_program("printf '%s  /backup/archive.tar\\n' '" + receipt["archive_sha256"] + "' | sha256sum --check --status\n" + """
find /target -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
tar --extract --file=/backup/archive.tar --numeric-owner --xattrs --xattrs-include='*' --acls --sparse -C /target
sync -f /target
printf '""" + _RESULT_PREFIX + "'\n" + _manifest_command("/target") + "\n")
    verified = _read_manifest(await _run_helper(client, image=image, volumes={source_name: {"bind": "/target", "mode": "rw"}, backup_name: {"bind": "/backup", "mode": "ro"}}, command=restore))
    if verified != source_manifest:
        raise HostedBackupError("restored volume manifest does not match receipt")
