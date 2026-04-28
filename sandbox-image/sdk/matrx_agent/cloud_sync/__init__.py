"""Real-time bridge between ~/cloud-files/ and AI Dream's cld_files.

The watcher (`CloudFilesWatcher`) lives inside the matrx_agent FastAPI daemon
and pushes file changes through the same `/api/cloud-files/*` bridge endpoints
the `mtx files` CLI uses. The startup `cloud-files-sync.sh down` and shutdown
`cloud-files-sync.sh up` shell hooks remain as bulk safety nets.
"""

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig
from matrx_agent.cloud_sync.watcher import CloudFilesWatcher

__all__ = ["AsyncBridgeClient", "BridgeConfig", "CloudFilesWatcher"]
