import os
import subprocess
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from matrx_agent.workspace import WORKSPACE_ROOT

router = APIRouter()

ENV_HELPER = "/opt/sandbox/scripts/matrx-git-credential-env"
DEFAULT_CACHE_TIMEOUT = "31536000"

class CredentialRequest(BaseModel):
    kind: Literal["github", "ssh"]
    token: Optional[str] = None
    private_key: Optional[str] = None
    scope: Optional[str] = None
    known_hosts: Optional[str] = None

@router.post("/credentials")
async def add_credentials(req: CredentialRequest):
    home_dir = WORKSPACE_ROOT
    
    if req.kind == "github":
        if not req.token:
            raise HTTPException(status_code=400, detail="Token required for github credentials")

        username = os.environ.get("GITHUB_USERNAME") or os.environ.get("GITHUB_USER") or "x-access-token"
        try:
            _configure_github_helpers(home_dir, username=username)
            _approve_github_token(home_dir, token=req.token, username=username)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        
    elif req.kind == "ssh":
        if not req.private_key:
            raise HTTPException(status_code=400, detail="Private key required for ssh credentials")
            
        ssh_dir = home_dir / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir.chmod(0o700)
        
        key_file = ssh_dir / "id_rsa"
        key_file.write_text(req.private_key)
        key_file.chmod(0o600)
        
        if req.known_hosts:
            known_hosts_file = ssh_dir / "known_hosts"
            with known_hosts_file.open("a") as f:
                f.write(req.known_hosts + "\n")
                
    return {"status": "success"}

@router.post("/credentials/revoke")
async def revoke_credentials():
    home_dir = WORKSPACE_ROOT
    env = _git_env(home_dir)

    # Remove current-session HTTPS credentials from the in-memory cache, then
    # disable helpers for this sandbox session. Env-injected tokens still exist
    # in the process environment, but without the helper config git will not
    # consult them until the next boot re-runs configure-git-credentials.sh.
    reject = "protocol=https\nhost=github.com\nusername=x-access-token\n\n"
    subprocess.run(["git", "credential", "reject"], input=reject, text=True, env=env, check=False)
    subprocess.run(
        ["git", "credential-cache", f"--socket={_cache_socket(home_dir)}", "exit"],
        env=env,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["git", "config", "--global", "--unset-all", "credential.helper"], env=env, check=False)
    subprocess.run(
        ["git", "config", "--global", "--unset", "credential.https://github.com.username"],
        env=env,
        check=False,
    )

    # Shred SSH key
    key_file = WORKSPACE_ROOT / ".ssh" / "id_rsa"
    if key_file.exists():
        key_file.unlink()
        
    return {"status": "success"}


def _cache_socket(home_dir: Path) -> Path:
    return home_dir / ".matrx" / "runtime" / "git-credential-cache.sock"


def _git_env(home_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    return env


def _run_git(home_dir: Path, args: list[str], *, input_text: str | None = None) -> None:
    proc = subprocess.run(
        ["git", *args],
        input=input_text,
        text=True,
        env=_git_env(home_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown git error").strip()
        raise RuntimeError(detail)


def _configure_github_helpers(home_dir: Path, *, username: str) -> None:
    runtime_dir = home_dir / ".matrx" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)

    # The unset command exits 5 when no value exists; that is fine.
    proc = subprocess.run(
        ["git", "config", "--global", "--unset-all", "credential.helper"],
        env=_git_env(home_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode not in (0, 5):
        raise RuntimeError("failed to reset git credential helpers")

    cache_helper = f"cache --socket={_cache_socket(home_dir)} --timeout={DEFAULT_CACHE_TIMEOUT}"
    _run_git(home_dir, ["config", "--global", "--add", "credential.helper", cache_helper])
    _run_git(home_dir, ["config", "--global", "--add", "credential.helper", ENV_HELPER])
    _run_git(home_dir, ["config", "--global", "credential.https://github.com.username", username])


def _approve_github_token(home_dir: Path, *, token: str, username: str) -> None:
    payload = (
        "protocol=https\n"
        "host=github.com\n"
        f"username={username}\n"
        f"password={token}\n\n"
    )
    _run_git(home_dir, ["credential", "approve"], input_text=payload)
