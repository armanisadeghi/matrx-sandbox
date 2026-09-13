"""Narrow root helper for EC2 writable-home promotion.

Installed verbatim as ``/usr/local/libexec/matrx-ec2-home-copy``. Input is a
strict operation receipt, never paths, shell fragments, or Docker arguments.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Any

_DOCKER = "/usr/bin/docker"
_PYTHON = "/usr/bin/python3.11"
_SUDO = "/usr/bin/sudo"
_ENV = "/usr/bin/env"
_LIBEXEC = "/usr/local/libexec/matrx-ec2-home-copy"
_ID = re.compile(r"^[0-9a-f]{64}$")
_OP_KEYS = frozenset(("schema_version", "operation", "source_container_id", "source_image", "source_pid", "source_started_at", "source_overlay_identity", "target_volume", "target_created_at"))
_JOURNAL_DIR = "/var/lib/matrx-sandbox/hosted-migrations"
_COPY_LOCK = re.compile(r"^copy-[A-Za-z0-9][A-Za-z0-9_.-]{0,200}\.lock$")


class Ec2HomeCopyError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise Ec2HomeCopyError(message)


def _normalize_copy_lock(fd: int, directory: os.stat_result, name: str) -> None:
    """Make a root-helper lock readable by the service identity, safely.

    The root-only copy helper and the unprivileged orchestrator share this lock
    namespace. Root may create the inode, but ownership must match the journal
    directory before the service or release holder can reopen it.
    """
    value = os.fstat(fd)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        _fail(f"copy lock is not one safe regular inode: {name}")
    if stat.S_IMODE(value.st_mode) != 0o600:
        _fail(f"copy lock has incompatible mode: {name}")
    expected = (directory.st_uid, directory.st_gid)
    owner = (value.st_uid, value.st_gid)
    if owner == (0, 0) and owner != expected:
        os.fchown(fd, *expected)
    elif owner != expected:
        _fail(f"copy lock has incompatible owner: {name}")


def normalize_copy_locks(root: str = _JOURNAL_DIR) -> None:
    """Repair only canonical root-helper lock ownership before release admission."""
    directory_fd = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    try:
        directory = os.fstat(directory_fd)
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if not _COPY_LOCK.fullmatch(entry.name):
                    continue
                fd = os.open(
                    entry.name,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                try:
                    named = os.stat(
                        entry.name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                        _fail(f"copy lock changed while opening: {entry.name}")
                    _normalize_copy_lock(fd, directory, entry.name)
                finally:
                    os.close(fd)
    finally:
        os.close(directory_fd)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _operation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _OP_KEYS or value.get("schema_version") != 1:
        _fail("operation has missing or unexpected fields")
    if not isinstance(value["operation"], str) or not re.fullmatch(r"[0-9a-f-]{16,64}", value["operation"]):
        _fail("operation identity is invalid")
    if not isinstance(value["source_container_id"], str) or not _ID.fullmatch(value["source_container_id"]):
        _fail("source container ID must be a full Docker ID")
    image = value["source_image"]
    if not isinstance(image, str) or not _ID.fullmatch(image.removeprefix("sha256:")):
        _fail("source image must be an immutable image ID")
    if not isinstance(value["source_pid"], int) or isinstance(value["source_pid"], bool) or value["source_pid"] <= 1:
        _fail("source PID is invalid")
    if not isinstance(value["source_started_at"], str) or not value["source_started_at"]:
        _fail("source start identity is invalid")
    if not isinstance(value["source_overlay_identity"], dict) or not value["source_overlay_identity"]:
        _fail("source overlay identity is invalid")
    if not isinstance(value["target_volume"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["target_volume"]):
        _fail("target volume is invalid")
    if not isinstance(value["target_created_at"], str) or not value["target_created_at"]:
        _fail("target volume creation identity is invalid")
    return value


def _open_child(parent: int, name: str) -> int:
    if name in {"", ".", ".."} or "/" in name:
        _fail("unsafe fixed path component")
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def source_home_fd(pid: int) -> int:
    """Open trusted proc-root, then fixed children using O_NOFOLLOW/openat.

    /proc/PID/root is intentionally not O_NOFOLLOW: it is Linux's trusted
    magic link. Applying O_NOFOLLOW there would reject every valid source.
    """
    if not isinstance(pid, int) or pid <= 1:
        _fail("invalid source pid")
    try:
        root = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise Ec2HomeCopyError("cannot open verified source proc root") from exc
    try:
        home = _open_child(root, "home")
        try:
            return _open_child(home, "agent")
        finally:
            os.close(home)
    finally:
        os.close(root)


def _home_mount(destination: Any) -> bool:
    if not isinstance(destination, str) or not destination.startswith("/"):
        return True
    path, home = PurePosixPath(destination), PurePosixPath("/home/agent")
    return path == home or home.is_relative_to(path) or path.is_relative_to(home)


def validate_source(inspect: dict[str, Any], op: dict[str, Any]) -> None:
    state = inspect.get("State") or {}
    if inspect.get("Id") != op["source_container_id"] or inspect.get("Image") != op["source_image"]:
        _fail("source Docker identity changed")
    if state.get("Pid") != op["source_pid"] or state.get("StartedAt") != op["source_started_at"] or state.get("Paused") is not True:
        _fail("source PID/start/pause identity changed")
    if _canon(inspect.get("GraphDriver") or {}) != _canon(op["source_overlay_identity"]):
        _fail("source overlay identity changed")
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or any(_home_mount(m.get("Destination") if isinstance(m, dict) else None) for m in mounts):
        _fail("source has a home ancestor or nested mount")


def validate_target_volume(volume: dict[str, Any], op: dict[str, Any]) -> str:
    if volume.get("Name") != op["target_volume"] or volume.get("CreatedAt") != op["target_created_at"]:
        _fail("target volume identity changed")
    if volume.get("Driver") != "local" or volume.get("Scope") not in {None, "local"} or volume.get("Options"):
        _fail("target volume must use the default local driver")
    labels = volume.get("Labels") or {}
    if labels.get("matrx.owner") != "orchestrator" or labels.get("matrx.ec2_home_copy") != op["operation"]:
        _fail("target volume ownership labels mismatch")
    mountpoint = volume.get("Mountpoint")
    if not isinstance(mountpoint, str) or not mountpoint.startswith("/"):
        _fail("target volume mountpoint is invalid")
    return mountpoint


def _open_absolute_directory(path: str, *, copied_home: bool = False) -> int:
    if not path.startswith("/") or "//" in path:
        _fail("unsafe Docker mountpoint")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.split("/")[1:]:
            child = _open_child(fd, part)
            os.close(fd)
            fd = child
        meta = os.fstat(fd)
        if not copied_home and (meta.st_uid != 0 or meta.st_gid != 0 or meta.st_mode & 0o022):
            _fail("target volume mountpoint is not securely root owned")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: return
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()


def _tar_copy(source_fd: int, target_fd: int) -> None:
    """Direct GNU-tar stream; both exits are checked and stderr never blocks."""
    producer = consumer = None
    try:
        producer = subprocess.Popen(["tar", "--create", "--format=posix", "--file=-", "--numeric-owner", "--xattrs", "--acls", "--sparse", "-C", f"/proc/self/fd/{source_fd}", "."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(source_fd,), start_new_session=True)
        assert producer.stdout is not None
        consumer = subprocess.Popen(["tar", "--extract", "--file=-", "--numeric-owner", "--xattrs", "--xattrs-include=*", "--acls", "--sparse", "-C", f"/proc/self/fd/{target_fd}"], stdin=producer.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(target_fd,), start_new_session=True)
        producer.stdout.close()
        consumer_rc, producer_rc = consumer.wait(), producer.wait()
        if producer_rc or consumer_rc:
            _fail(f"tar stream failed (producer={producer_rc}, consumer={consumer_rc})")
    except BaseException:
        if consumer is not None: _stop(consumer)
        if producer is not None: _stop(producer)
        raise


# Deliberately embedded from hosted_backup's controlled manifest program. This
# release artifact never imports mutable user/service modules while root.
_MANIFEST = r'''import base64,errno,hashlib,json,os,stat,subprocess,sys
r=sys.argv[1]
if not os.path.isdir(r):raise SystemExit("manifest root absent")
if not(os.path.exists("/usr/bin/getfacl")or os.path.exists("/bin/getfacl")):raise SystemExit("getfacl required")
def sp(p,n):
 o=[];q=0;f=os.open(p,os.O_RDONLY)
 try:
  while q<n:
   a=os.lseek(f,q,os.SEEK_DATA);b=os.lseek(f,a,os.SEEK_HOLE);o.append([a,min(b,n)]);q=b
 except OSError as e:
  if e.errno==errno.ENXIO:return o
  raise SystemExit("sparse extents unavailable")
 finally:os.close(f)
 return o
def dg(p):
 h=hashlib.sha256()
 with open(p,"rb",buffering=0)as f:
  for c in iter(lambda:f.read(1048576),b""):h.update(c)
 return h.hexdigest()
def ac(p):
 x=subprocess.run(["getfacl","-P","-cpn","--",p],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
 if x.returncode:raise SystemExit("getfacl failed")
 return x.stdout.decode("utf-8","surrogateescape")
e=[];i={};ps=[r]
for b,ds,fs in os.walk(r,topdown=True,followlinks=False):ds.sort();fs.sort();ps.extend(os.path.join(b,n)for n in ds+fs)
for p in ps:
 z=os.lstat(p);v={"path":os.path.relpath(p,r),"uid":z.st_uid,"gid":z.st_gid,"mode":stat.S_IMODE(z.st_mode),"mtime_ns":z.st_mtime_ns,"acl":ac(p),"xattrs":{n:base64.b64encode(os.getxattr(p,n,follow_symlinks=False)).decode()for n in sorted(os.listxattr(p,follow_symlinks=False))}}
 if stat.S_ISLNK(z.st_mode):v.update(type="symlink",target=os.readlink(p))
 elif stat.S_ISREG(z.st_mode):
  k=(z.st_dev,z.st_ino);i.setdefault(k,[]).append(v["path"]);v.update(type="file",size=z.st_size,sha256=dg(p),sparse=sp(p,z.st_size))
 elif stat.S_ISDIR(z.st_mode):v.update(type="directory")
 else:raise SystemExit("unsupported file type")
 e.append(v)
print(json.dumps({"entries":e,"hardlink_groups":sorted(sorted(v)for v in i.values()if len(v)>1)},sort_keys=True,separators=(",",":")))'''


def _manifest(fd: int) -> dict[str, Any]:
    # A container mount namespace can be unreachable to the host's getcwd().
    # Keep Python's cwd on the host; '/.' makes lstat inspect the directory,
    # not the proc-fd symlink, while pinning every walk to the opened home.
    completed = subprocess.run([_PYTHON, "-I", "-c", _MANIFEST, f"/proc/{os.getpid()}/fd/{fd}/."], pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        _fail("metadata manifest failed: " + completed.stderr.decode(errors="replace")[-600:])
    try: result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc: raise Ec2HomeCopyError("metadata manifest was corrupt") from exc
    if not isinstance(result, dict) or not isinstance(result.get("entries"), list): _fail("metadata manifest was incomplete")
    return result


def _inspect(kind: str, identity: str) -> dict[str, Any]:
    completed = subprocess.run([_DOCKER, kind, "inspect", identity], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode: _fail(f"Docker could not inspect {kind}")
    try: result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc: raise Ec2HomeCopyError("Docker emitted invalid inspection JSON") from exc
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict): _fail("Docker inspection was not exact")
    return result[0]


def perform_copy(value: Any) -> dict[str, Any]:
    op = _operation(value)
    # Survives an orchestrator process crash. Recovery takes the same lock
    # before it may unpause the source or clean the isolated target volume.
    lock_name = "copy-" + op["operation"] + ".lock"
    fd = os.open(_JOURNAL_DIR + "/" + lock_name,
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        directory = os.stat(_JOURNAL_DIR, follow_symlinks=False)
        _normalize_copy_lock(fd, directory, lock_name)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _perform_copy_locked(op)
    finally:
        os.close(fd)


def _perform_copy_locked(value: Any) -> dict[str, Any]:
    op = _operation(value)
    validate_source(_inspect("container", op["source_container_id"]), op)
    mountpoint = validate_target_volume(_inspect("volume", op["target_volume"]), op)
    source = source_home_fd(op["source_pid"])
    try:
        target = _open_absolute_directory(mountpoint)
    except BaseException:
        os.close(source)
        raise
    try:
        if os.listdir(target): _fail("target volume is not empty")
        before = _manifest(source)
        # Sparse payload plus filesystem bookkeeping, before the first write.
        required = sum(sum(end - start for start, end in entry.get("sparse", []))
                       + 8192 for entry in before["entries"])
        space = os.fstatvfs(target)
        if space.f_bavail * space.f_frsize < required:
            _fail("insufficient free space for the preserved home")
        _tar_copy(source, target)
        after = _manifest(target)
        if before != after: _fail("target metadata manifest differs from source")
        validate_source(_inspect("container", op["source_container_id"]), op)
        validate_target_volume(_inspect("volume", op["target_volume"]), op)
        if _manifest(source) != before:
            _fail("source changed during paused copy")
        os.sync()
        return {"ok": True, "schema_version": 1, "operation": op["operation"],
                "source_identity": op, "manifest": before,
                "manifest_sha256": hashlib.sha256(_canon(before).encode()).hexdigest()}
    finally:
        os.close(source); os.close(target)


def verify_home(value: Any) -> dict[str, Any]:
    """Read-only attestation of the exact paused replacement before routing."""
    keys = {"action", "operation", "receipt", "target_id", "target_image"}
    if not isinstance(value, dict) or set(value) != keys or value["action"] != "verify":
        _fail("invalid verification request")
    op = _operation(value["operation"])
    receipt = value["receipt"]
    if (not isinstance(receipt, dict) or receipt.get("source_identity") != op
            or receipt.get("operation") != op["operation"]
            or receipt.get("manifest_sha256") != hashlib.sha256(_canon(receipt.get("manifest")).encode()).hexdigest()):
        _fail("invalid copy receipt")
    if (not isinstance(value["target_id"], str) or not _ID.fullmatch(value["target_id"])
            or not isinstance(value["target_image"], str)
            or not _ID.fullmatch(value["target_image"].removeprefix("sha256:"))):
        _fail("invalid replacement identity")

    def checked_target():
        target = _inspect("container", value["target_id"])
        if (target.get("Id") != value["target_id"] or target.get("Image") != value["target_image"]
                or (target.get("State") or {}).get("Paused") is not True
                or ((target.get("Config") or {}).get("Labels") or {}).get("matrx.hosted_migration") != op["operation"]
                or not any(m.get("Type") == "volume" and m.get("Name") == op["target_volume"]
                           and m.get("Destination") == "/home/agent" and m.get("RW") is True
                           for m in target.get("Mounts", []))):
            _fail("replacement is not the exact paused home owner")
        return target

    initial = checked_target()
    mountpoint = validate_target_volume(_inspect("volume", op["target_volume"]), op)
    fd = _open_absolute_directory(mountpoint, copied_home=True)
    try:
        manifest = _manifest(fd)
        if manifest != receipt["manifest"]:
            _fail("replacement boot changed the preserved home")
        final = checked_target()
        if final.get("State", {}).get("StartedAt") != initial.get("State", {}).get("StartedAt"):
            _fail("replacement restarted during verification")
        validate_target_volume(_inspect("volume", op["target_volume"]), op)
        return {"ok": True, "operation": op["operation"], "target_id": value["target_id"],
                "target_image": value["target_image"], "manifest_sha256": receipt["manifest_sha256"]}
    finally:
        os.close(fd)


def verify_root_artifact(path: str, expected_sha256: str) -> None:
    if path != artifact_path(expected_sha256) or not _ID.fullmatch(expected_sha256): _fail("root helper artifact identity is invalid")
    meta = os.lstat(path)
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_gid != 0 or meta.st_mode & 0o022: _fail("root helper artifact is not securely root owned")
    with open(path, "rb", buffering=0) as artifact: actual = hashlib.file_digest(artifact, "sha256").hexdigest()
    if actual != expected_sha256: _fail("root helper artifact hash differs from approved release")


def artifact_path(digest: str) -> str:
    if not _ID.fullmatch(digest):
        _fail("root helper artifact digest is invalid")
    return _LIBEXEC + "-" + digest + ".py"


async def run_root_copy(operation: dict[str, Any], *, artifact_sha256: str) -> dict[str, Any]:
    path = artifact_path(artifact_sha256)
    _operation(operation["operation"] if operation.get("action") == "verify" else operation)
    verify_root_artifact(path, artifact_sha256)
    process = await asyncio.create_subprocess_exec(_SUDO, "-n", _ENV, "-i", "PATH=/usr/bin:/bin", _PYTHON, "-I", path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    communication = asyncio.create_task(process.communicate(_canon(operation).encode()))
    try:
        output, _ = await asyncio.shield(communication)
    except asyncio.CancelledError:
        # Do not release the owning home lease while native tar still runs.
        # A process crash is independently fenced by the helper's copy lock.
        await communication
        raise
    try: receipt = json.loads(output)
    except json.JSONDecodeError as exc: raise Ec2HomeCopyError("root helper did not emit one JSON receipt") from exc
    if process.returncode != 0 or not isinstance(receipt, dict) or receipt.get("ok") is not True: _fail("root helper refused the copy")
    return receipt


def main() -> int:
    if sys.argv[1:] == ["--preflight"]:
        if os.geteuid() != 0:
            _fail("home-copy helper must run as root")
        normalize_copy_locks()
        for command in (["/usr/bin/tar", "--version"], ["/usr/bin/getfacl", "--version"], [_DOCKER, "version", "--format", "{{.Server.Version}}"]):
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print("EC2_HOME_COPY_READY")
        return 0
    try:
        value = json.load(sys.stdin)
        print(_canon(verify_home(value) if isinstance(value, dict) and value.get("action") == "verify" else perform_copy(value)))
        return 0
    except (Ec2HomeCopyError, json.JSONDecodeError) as exc: print(_canon({"ok": False, "error": str(exc)})); return 2


if __name__ == "__main__": raise SystemExit(main())
