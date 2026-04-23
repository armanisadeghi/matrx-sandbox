import asyncio
import os
import signal
import socket
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

class SignalRequest(BaseModel):
    signal: str = "SIGTERM"

@router.get("/processes")
async def list_processes():
    process = await asyncio.create_subprocess_exec(
        "ps", "aux",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=stderr.decode("utf-8"))
        
    lines = stdout.decode("utf-8").strip().split("\n")
    if not lines:
        return {"processes": []}
        
    headers = lines[0].split()
    processes = []
    
    for line in lines[1:]:
        parts = line.split(None, len(headers)-1)
        if len(parts) == len(headers):
            proc_dict = dict(zip(headers, parts))
            processes.append(proc_dict)
            
    return {"processes": processes}

@router.post("/processes/{pid}/signal")
async def signal_process(pid: int, req: SignalRequest):
    try:
        sig = getattr(signal, req.signal)
        os.kill(pid, sig)
        return {"status": "success"}
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail="Process not found")
    except AttributeError:
        raise HTTPException(status_code=400, detail="Invalid signal")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

@router.get("/ports")
async def list_ports():
    # Simple check for listening TCP ports
    process = await asyncio.create_subprocess_exec(
        "ss", "-tlnp",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    lines = stdout.decode("utf-8").strip().split("\n")
    ports = []
    
    for line in lines[1:]:
        if not line: continue
        parts = line.split()
        if len(parts) >= 4:
            local_address = parts[3]
            if ":" in local_address:
                port = local_address.split(":")[-1]
                if port.isdigit():
                    ports.append(int(port))
                    
    return {"ports": list(set(ports))}
