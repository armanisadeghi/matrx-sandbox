import asyncio
import os
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

router = APIRouter()

class WebSocketEventHandler(FileSystemEventHandler):
    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self.queue = queue
        self.loop = asyncio.get_running_loop()

    def _enqueue(self, event_data: dict) -> None:
        # Drop under backpressure instead of growing the queue without bound:
        # a slow/stuck client must not let a busy filesystem balloon memory.
        try:
            self.queue.put_nowait(event_data)
        except asyncio.QueueFull:
            pass

    def _put_event(self, event_type: str, src_path: str, is_directory: bool, dest_path: Optional[str] = None):
        event_data = {
            "type": event_type,
            "path": src_path,
            "is_dir": is_directory
        }
        if dest_path:
            event_data["dest_path"] = dest_path

        self.loop.call_soon_threadsafe(self._enqueue, event_data)

    def on_created(self, event):
        self._put_event("created", event.src_path, event.is_directory)

    def on_deleted(self, event):
        self._put_event("deleted", event.src_path, event.is_directory)

    def on_modified(self, event):
        self._put_event("modified", event.src_path, event.is_directory)

    def on_moved(self, event):
        self._put_event("moved", event.src_path, event.is_directory, event.dest_path)


def _stop_observer(observer) -> None:
    """Blocking observer teardown — run via asyncio.to_thread so stop()/join()
    don't block the event loop (they wait on the inotify thread to drain)."""
    observer.stop()
    observer.join()


@router.websocket("/fs/watch")
async def watch_fs(websocket: WebSocket, path: str = "/home/agent"):
    await websocket.accept()

    if not os.path.exists(path):
        await websocket.close(code=1008, reason="Path does not exist")
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    event_handler = WebSocketEventHandler(queue)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()

    async def pump_events() -> None:
        while True:
            event_data = await queue.get()
            await websocket.send_json(event_data)

    async def detect_disconnect() -> None:
        # The client doesn't need to send anything, but we must still read so a
        # disconnect is noticed even when no FS events are flowing — otherwise a
        # dead client on an idle path keeps the inotify observer alive forever
        # (watch + thread leak, eventually exhausting inotify watches).
        try:
            while True:
                await websocket.receive()
        except WebSocketDisconnect:
            return

    pump = asyncio.create_task(pump_events())
    watcher = asyncio.create_task(detect_disconnect())
    try:
        await asyncio.wait([pump, watcher], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (pump, watcher):
            if not t.done():
                t.cancel()
        await asyncio.gather(pump, watcher, return_exceptions=True)
        await asyncio.to_thread(_stop_observer, observer)
        try:
            await websocket.close()
        except Exception:
            pass
