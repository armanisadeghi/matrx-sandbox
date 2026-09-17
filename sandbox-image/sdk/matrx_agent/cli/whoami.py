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
    limits = body.get("limits") or {}
    return {
        "reachable": True,
        "configured": bool(body.get("configured")),
        "version": body.get("version"),
        # AI Dream's bridge nests these under `limits` — fall back to the
        # top-level keys in case the response shape changes again.
        "quota_bytes": limits.get("quota_bytes") or body.get("quota_bytes"),
        "max_upload_bytes": limits.get("max_upload_bytes") or body.get("max_upload_bytes"),
    }


def run() -> int:
    from matrx_agent.bridge_headers import missing_bridge_env

    aidream_url = os.environ.get("MATRX_AIDREAM_URL", "")
    # The SDK version on disk is NOT the image's baked version once a binding-time
    # refresh has run (matrx_agent/selfupdate.py) — read the real one.
    try:
        from matrx_agent import selfupdate

        sdk = {
            "version": selfupdate.installed_version(),
            "baked_image_version": os.environ.get("MATRX_IMAGE_VERSION", "unknown"),
            "refreshed": selfupdate.read_stamp() or None,
        }
    except Exception as exc:  # never let diagnostics break `mtx whoami`
        sdk = {"version": "unknown", "error": str(exc)}
    info = {
        "sandbox_id": os.environ.get("SANDBOX_ID", "unknown"),
        "sdk": sdk,
        "user_id": os.environ.get("USER_ID", "unknown"),
        # Both halves of the request context every AI Dream call carries. A
        # box with no organization cannot call the bridge at all (AI Dream
        # refuses it), so show it here where a person goes to diagnose.
        "organization_id": os.environ.get("ORGANIZATION_ID", "unknown"),
        "tier": os.environ.get("MATRX_TIER", "unknown"),
        "hostname": socket.gethostname(),
        "home": os.environ.get("HOT_PATH", os.path.expanduser("~")),
        "aidream": {
            "url": aidream_url,
            "configured": not missing_bridge_env(),
            "missing_env": missing_bridge_env(),
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
