import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

class GitCloneRequest(BaseModel):
    url: str
    dest: str
    branch: Optional[str] = None
    depth: Optional[int] = None

class GitAddRequest(BaseModel):
    paths: List[str]
    cwd: str

class GitCommitRequest(BaseModel):
    message: str
    cwd: str
    author: Optional[str] = None
    amend: bool = False

class GitPushRequest(BaseModel):
    cwd: str
    remote: str = "origin"
    branch: Optional[str] = None
    force_with_lease: bool = False

class GitPullRequest(BaseModel):
    cwd: str
    remote: str = "origin"
    branch: Optional[str] = None
    rebase: bool = False

class GitBranchRequest(BaseModel):
    action: str # "create", "delete", "switch"
    name: str
    cwd: str

class GitStashRequest(BaseModel):
    action: str # "push", "pop", "list", "drop"
    cwd: str
    message: Optional[str] = None

async def run_git(args: List[str], cwd: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")

@router.post("/git/clone")
async def git_clone(req: GitCloneRequest):
    args = ["clone"]
    if req.branch:
        args.extend(["-b", req.branch])
    if req.depth:
        args.extend(["--depth", str(req.depth)])
    args.extend([req.url, req.dest])
    
    code, out, err = await run_git(args, cwd="/home/agent")
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.get("/git/status")
async def git_status(cwd: str):
    # Simplistic status parsing
    code, out, err = await run_git(["status", "--porcelain", "-b"], cwd=cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    
    lines = out.split("\n")
    branch_line = lines[0] if lines else ""
    # Very basic parsing, would need more robust parsing for production
    return {"branch": branch_line, "raw_output": out}

@router.get("/git/diff")
async def git_diff(cwd: str, path: Optional[str] = None, staged: bool = False):
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args.extend(["--", path])
        
    code, out, err = await run_git(args, cwd=cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"diff": out}

@router.post("/git/add")
async def git_add(req: GitAddRequest):
    args = ["add"] + req.paths
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success"}

@router.post("/git/commit")
async def git_commit(req: GitCommitRequest):
    args = ["commit", "-m", req.message]
    if req.amend:
        args.append("--amend")
    if req.author:
        args.extend(["--author", req.author])
        
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.post("/git/push")
async def git_push(req: GitPushRequest):
    args = ["push", req.remote]
    if req.branch:
        args.append(req.branch)
    if req.force_with_lease:
        args.append("--force-with-lease")
        
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.post("/git/pull")
async def git_pull(req: GitPullRequest):
    args = ["pull", req.remote]
    if req.branch:
        args.append(req.branch)
    if req.rebase:
        args.append("--rebase")
        
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.post("/git/branch")
async def git_branch(req: GitBranchRequest):
    args = []
    if req.action == "create":
        args = ["branch", req.name]
    elif req.action == "delete":
        args = ["branch", "-D", req.name]
    elif req.action == "switch":
        args = ["checkout", req.name]
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
        
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.post("/git/stash")
async def git_stash(req: GitStashRequest):
    args = ["stash", req.action]
    if req.message and req.action == "push":
        args.extend(["-m", req.message])
        
    code, out, err = await run_git(args, cwd=req.cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success", "output": out}

@router.get("/git/log")
async def git_log(cwd: str, limit: int = 50):
    args = ["log", f"-n{limit}", "--pretty=format:%H|%h|%an|%ad|%s", "--date=iso"]
    code, out, err = await run_git(args, cwd=cwd)
    if code != 0:
        raise HTTPException(status_code=400, detail=err)
        
    logs = []
    for line in out.split("\n"):
        if not line: continue
        parts = line.split("|", 4)
        if len(parts) == 5:
            logs.append({
                "sha": parts[0],
                "short": parts[1],
                "author": parts[2],
                "date": parts[3],
                "subject": parts[4]
            })
    return {"logs": logs}
