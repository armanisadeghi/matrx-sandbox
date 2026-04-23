import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

class CredentialRequest(BaseModel):
    kind: Literal["github", "ssh"]
    token: Optional[str] = None
    private_key: Optional[str] = None
    scope: Optional[str] = None
    known_hosts: Optional[str] = None

@router.post("/credentials")
async def add_credentials(req: CredentialRequest):
    home_dir = Path("/home/agent")
    
    if req.kind == "github":
        if not req.token:
            raise HTTPException(status_code=400, detail="Token required for github credentials")
            
        # Configure git to use a custom credential helper script
        # The script will echo the token when asked for password
        cred_dir = home_dir / ".matrx" / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        
        helper_script = cred_dir / "github_helper.sh"
        helper_script.write_text(f"#!/bin/sh\necho password={req.token}\n")
        helper_script.chmod(0o700)
        
        # Configure git
        os.system(f'git config --global credential.helper "{helper_script}"')
        os.system(f'git config --global credential.https://github.com.username oauth2')
        
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
    # Remove git credential config
    os.system('git config --global --unset credential.helper')
    os.system('git config --global --unset credential.https://github.com.username')
    
    # Shred helper script
    helper_script = Path("/home/agent/.matrx/credentials/github_helper.sh")
    if helper_script.exists():
        helper_script.unlink()
        
    # Shred SSH key
    key_file = Path("/home/agent/.ssh/id_rsa")
    if key_file.exists():
        key_file.unlink()
        
    return {"status": "success"}
