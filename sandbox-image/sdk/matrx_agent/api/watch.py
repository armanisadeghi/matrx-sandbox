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

    def _put_event(self, event_type: str, src_path: str, is_directory: bool, dest_path: Optional[str] = None):
        event_data = {
            "type": event_type,
            "path": src_path,
            "is_dir": is_directory
        }
        if dest_path:
            event_data["dest_path"] = dest_path
            
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event_data)

    def on_created(self, event):
        self._put_event("created", event.src_path, event.is_directory)

    def on_deleted(self, event):
        self._put_event("deleted", event.src_path, event.is_directory)

    def on_modified(self, event):
        self._put_event("modified", event.src_path, event.is_directory)

    def on_moved(self, event):
        self._put_event("moved", event.src_path, event.is_directory, event.dest_path)


@router.websocket("/fs/watch")
async def watch_fs(websocket: WebSocket, path: str = "/home/agent"):
    await websocket.accept()
    
    if not os.path.exists(path):
        await websocket.close(code=1008, reason="Path does not exist")
        return

    queue = asyncio.Queue()
    event_handler = WebSocketEventHandler(queue)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()

    try:
        while True:
            # Send file system events to the client
            event_data = await queue.get()
            await websocket.send_json(event_data)
    except WebSocketDisconnect:
        pass
    finally:
        observer.stop()
        observer.join()
