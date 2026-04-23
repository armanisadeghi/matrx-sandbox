import asyncio
import json
import time
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

class ExecRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    stdin: Optional[str] = None
    timeout: Optional[int] = None

async def stream_output(req: ExecRequest):
    import os
    
    # Merge custom env with current env
    env = os.environ.copy()
    if req.env:
        env.update(req.env)
        
    start_time = time.monotonic()
    
    try:
        process = await asyncio.create_subprocess_shell(
            req.command,
            stdin=asyncio.subprocess.PIPE if req.stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=req.cwd,
            env=env
        )
        
        if req.stdin and process.stdin:
            process.stdin.write(req.stdin.encode())
            process.stdin.write_eof()
            
        async def read_stream(stream, stream_name):
            while True:
                line = await stream.readline()
                if not line:
                    break
                # Send as SSE format
                data = json.dumps({"data": line.decode("utf-8", errors="replace")})
                yield f"event: {stream_name}\ndata: {data}\n\n".encode("utf-8")
                
        async def run_with_timeout():
            if req.timeout:
                try:
                    await asyncio.wait_for(process.wait(), timeout=req.timeout)
                except asyncio.TimeoutError:
                    process.terminate()
                    # give it a moment to terminate, then kill
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        process.kill()
                    return True # Timed out
            else:
                await process.wait()
            return False

        # Read both stdout and stderr concurrently
        # Since StreamingResponse expects an async generator yielding bytes, 
        # we can't easily interleave them without a queue.
        # So we use an asyncio.Queue to gather output from both streams
        
        q = asyncio.Queue()
        
        async def reader(stream, name):
            async for chunk in stream:
                await q.put((name, chunk))
                
        stdout_task = asyncio.create_task(reader(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(reader(process.stderr, "stderr"))
        wait_task = asyncio.create_task(run_with_timeout())
        
        async def generate():
            while True:
                # If stdout and stderr are done and queue is empty, we are done
                if stdout_task.done() and stderr_task.done() and q.empty():
                    break
                    
                try:
                    # Wait for an item in the queue with a timeout so we can check completion
                    name, chunk = await asyncio.wait_for(q.get(), timeout=0.1)
                    data = json.dumps({"data": chunk.decode("utf-8", errors="replace")})
                    yield f"event: {name}\ndata: {data}\n\n".encode("utf-8")
                    q.task_done()
                except asyncio.TimeoutError:
                    continue
            
            # Wait for the process to actually finish
            timed_out = await wait_task
            duration_ms = int((time.monotonic() - start_time) * 1000)
            
            exit_code = process.returncode if not timed_out else 124 # standard timeout exit code
            
            # send exit event
            data = json.dumps({
                "exit_code": exit_code,
                "cwd": req.cwd or os.getcwd(),
                "duration_ms": duration_ms
            })
            yield f"event: exit\ndata: {data}\n\n".encode("utf-8")
            
        return StreamingResponse(generate(), media_type="text/event-stream")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/exec/stream")
async def exec_stream(req: ExecRequest):
    return await stream_output(req)
