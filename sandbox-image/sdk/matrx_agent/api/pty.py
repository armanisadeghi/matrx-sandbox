import asyncio
import json
import os
import pty
import select
import signal
import struct
import fcntl
import termios
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from matrx_agent.api import _auth

router = APIRouter()


def _write_client_text(fd: int, pid: int, text: str) -> None:
    """Apply a PTY control frame or write ordinary terminal input verbatim.

    Keystrokes arrive one character at a time.  Some perfectly ordinary input
    (notably a digit such as ``2``) is also valid JSON, so successful JSON
    parsing alone cannot distinguish a control frame from terminal input.
    """
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        message = None

    if isinstance(message, dict) and message.get("type") == "resize":
        set_winsize(fd, message.get("rows", 30), message.get("cols", 120))
        return
    if isinstance(message, dict) and message.get("type") == "signal":
        sig_name = message.get("name")
        if isinstance(sig_name, str) and hasattr(signal, sig_name):
            os.kill(pid, getattr(signal, sig_name))
        return

    os.write(fd, text.encode())

def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

@router.websocket("/pty")
async def pty_endpoint(websocket: WebSocket, cols: int = 120, rows: int = 30):
    if not _auth.ws_token_ok(websocket):
        await websocket.close(code=1008, reason="invalid or missing agent token")
        return
    await websocket.accept()

    # Create the PTY and fork
    pid, fd = pty.fork()

    if pid == 0:
        # Child process: set terminal size and exec bash
        set_winsize(0, rows, cols)
        
        # Set some environment variables for the shell
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        
        # exec bash
        os.execvpe("bash", ["bash"], env)
    else:
        # Parent process: handle WebSocket and PTY IO
        set_winsize(fd, rows, cols)
        
        # Make fd non-blocking
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        loop = asyncio.get_running_loop()

        async def read_from_pty():
            try:
                while True:
                    # We use run_in_executor to not block on reading from the pty
                    # But since fd is non-blocking, we can just use loop.add_reader,
                    # but polling is simpler here for a quick implementation.
                    await asyncio.sleep(0.01)
                    try:
                        data = os.read(fd, 4096)
                        if data:
                            await websocket.send_bytes(data)
                    except BlockingIOError:
                        pass
                    except OSError:
                        # PTY closed
                        break
            except Exception:
                pass

        async def read_from_ws():
            try:
                while True:
                    message = await websocket.receive()
                    text = message.get("text")
                    data = message.get("bytes")
                    if text is not None:
                        _write_client_text(fd, pid, text)
                    elif data is not None:
                        os.write(fd, data)
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        # Run both tasks concurrently. Whichever finishes first (PTY closed or
        # client disconnected), we tear the other down and reap the child + fd
        # in ONE place so it happens exactly once. The old code only cleaned up
        # in read_from_ws's finally and cancelled the pending task without
        # awaiting it, so a bash child + PTY fd could leak on every disconnect
        # (fd exhaustion + zombie accumulation over time).
        task1 = asyncio.create_task(read_from_pty())
        task2 = asyncio.create_task(read_from_ws())
        try:
            await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (task1, task2):
                if not task.done():
                    task.cancel()
            # Let the cancellations settle so no coroutine is still touching fd.
            await asyncio.gather(task1, task2, return_exceptions=True)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
