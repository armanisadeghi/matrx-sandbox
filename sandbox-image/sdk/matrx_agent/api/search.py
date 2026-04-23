import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

class SearchContentRequest(BaseModel):
    query: str
    cwd: str = "/home/agent"
    regex: bool = True
    case_sensitive: bool = False
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    max_results: int = 100

class SearchPathRequest(BaseModel):
    pattern: str
    cwd: str = "/home/agent"
    hidden: bool = False
    max_results: int = 100

@router.post("/search/content")
async def search_content(req: SearchContentRequest):
    args = ["rg", "--json"]
    if not req.case_sensitive:
        args.append("-i")
    if not req.regex:
        args.append("-F")
    
    if req.include:
        for inc in req.include:
            args.extend(["-g", inc])
    if req.exclude:
        for exc in req.exclude:
            args.extend(["-g", f"!{exc}"])
            
    args.extend(["-m", str(req.max_results)])
    args.append(req.query)
    
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=req.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    # ripgrep returns 0 if matches found, 1 if none found, 2 on error
    if process.returncode == 2:
        raise HTTPException(status_code=400, detail=stderr.decode("utf-8"))
        
    results = []
    for line in stdout.decode("utf-8").split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            if data["type"] == "match":
                results.append({
                    "path": data["data"]["path"]["text"],
                    "lines": data["data"]["lines"]["text"],
                    "line_number": data["data"]["line_number"],
                    "submatches": data["data"]["submatches"]
                })
        except json.JSONDecodeError:
            pass
            
    return {"results": results}

@router.post("/search/paths")
async def search_paths(req: SearchPathRequest):
    # Depending on what's installed, use fd or find. Assuming fd is installed (common for these environments)
    args = ["fd", "--color=never"]
    if req.hidden:
        args.append("-H")
    
    args.append(req.pattern)
    
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=req.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    # fd returns 0 if found, 1 if not found
    if process.returncode > 1:
        # Fallback to standard find if fd isn't present
        find_args = ["find", ".", "-name", f"*{req.pattern}*"]
        process = await asyncio.create_subprocess_exec(
            *find_args,
            cwd=req.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0 and process.returncode != 1:
            raise HTTPException(status_code=400, detail=stderr.decode("utf-8"))
            
    paths = [p for p in stdout.decode("utf-8").split("\n") if p]
    return {"paths": paths[:req.max_results]}
