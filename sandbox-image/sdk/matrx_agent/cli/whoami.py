"""``mtx whoami`` — show what identity + integrations the current sandbox has.

Also probes AI Dream's ``/api/cloud-files/integrations.aidream`` so the
``aidream.live`` boolean reflects whether the bridge is actually reachable
and configured on AI Dream's side, not just whether the env vars are set on
this side. Useful for diagnosing token-mismatch / URL-typo issues.
"""

from __future__ import annotations

import json
import os
import socket
from urllib.error import URLError
from urllib.request import Request, urlopen


def _probe_bridge(url: str) -> dict:
    if not url:
        return {"reachable": False, "configured": False, "reason": "no MATRX_AIDREAM_URL"}
    probe = url.rstrip("/") + "/api/cloud-files/integrations.aidream"
    try:
        with urlopen(Request(probe), timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        return {"reachable": False, "configured": False, "reason": f"unreachable: {exc.reason}"}
    except (ValueError, TimeoutError) as exc:
        return {"reachable": False, "configured": False, "reason": str(exc)}
    return {
        "reachable": True,
        "configured": bool(body.get("configured")),
        "version": body.get("version"),
        "quota_bytes": body.get("quota_bytes"),
        "max_upload_bytes": body.get("max_upload_bytes"),
    }


def run() -> int:
    aidream_url = os.environ.get("MATRX_AIDREAM_URL", "")
    info = {
        "sandbox_id": os.environ.get("SANDBOX_ID", "unknown"),
        "user_id": os.environ.get("USER_ID", "unknown"),
        "tier": os.environ.get("MATRX_TIER", "unknown"),
        "hostname": socket.gethostname(),
        "home": os.environ.get("HOT_PATH", os.path.expanduser("~")),
        "aidream": {
            "url": aidream_url,
            "configured": bool(aidream_url) and bool(os.environ.get("MATRX_AIDREAM_SERVICE_TOKEN")),
            "bridge": _probe_bridge(aidream_url),
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
