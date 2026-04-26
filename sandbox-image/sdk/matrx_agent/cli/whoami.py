"""``mtx whoami`` — show what identity + integrations the current sandbox has."""

from __future__ import annotations

import json
import os
import socket


def run() -> int:
    info = {
        "sandbox_id": os.environ.get("SANDBOX_ID", "unknown"),
        "user_id": os.environ.get("USER_ID", "unknown"),
        "tier": os.environ.get("MATRX_TIER", "unknown"),
        "hostname": socket.gethostname(),
        "home": os.environ.get("HOT_PATH", os.path.expanduser("~")),
        "aidream": {
            "url": os.environ.get("MATRX_AIDREAM_URL", ""),
            "configured": bool(os.environ.get("MATRX_AIDREAM_URL"))
                          and bool(os.environ.get("MATRX_AIDREAM_SERVICE_TOKEN")),
        },
        "s3": {
            "bucket": os.environ.get("S3_BUCKET", ""),
            "hot_prefix": os.environ.get("MATRX_HOT_PREFIX", ""),
            "cold_prefix": os.environ.get("MATRX_COLD_PREFIX", ""),
            "configured": bool(os.environ.get("S3_BUCKET")),
        },
    }
    print(json.dumps(info, indent=2))
    return 0
