"""The server's OWN WORDS for a failed bridge call, in one shape.

A refused sandbox → AI Dream call is not a log line: it is something a person
has to be able to read, because the remedy is theirs (join the organization,
recreate the box, connect an account). AI Dream answers a refusal with a JSON
body naming a ``code``, a ``message`` and a ``remedy`` — for example 403
``organization_membership_required`` or 503 ``membership_unverifiable`` — and
throwing that away is how a user's edit disappeared with one WARNING in a log
nobody reads.

So every caller that catches a bridge failure normalises it HERE, and puts the
result on a surface a person or the orchestrator reads (the watcher's
``held_writes``, the downstream subscriber's ``last_refusal``). One shape:

    {"status": 403, "code": "organization_membership_required",
     "message": "…", "remedy": "…", "retryable": False, "error": "<repr>"}

``retryable`` is the same judgement the watcher's retry ladder makes, kept in
one place so "is this worth retrying" and "what do we tell the person" can
never disagree.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

#: Statuses worth trying again on the hot ladder. Everything else is the
#: server saying no — retrying it immediately just spends the fleet's budget.
RETRYABLE_STATUSES = frozenset({408, 425, 429})

_TEXT_LIMIT = 400


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUSES or status_code >= 500


def _first_str(obj: Any, *names: str) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    for name in names:
        value = obj.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_TEXT_LIMIT]
    return None


def describe_bridge_failure(error: BaseException) -> dict[str, Any]:
    """Normalise any bridge exception into the one refusal shape.

    A transport error (no response at all) is described as an unreachable
    server, which is retryable — the box is not being refused, it simply could
    not ask.
    """
    if not isinstance(error, httpx.HTTPStatusError):
        return {
            "status": None,
            "code": "bridge_unreachable",
            "message": str(error)[:_TEXT_LIMIT] or error.__class__.__name__,
            "remedy": None,
            "retryable": True,
            "error": f"{error.__class__.__name__}: {error}"[:_TEXT_LIMIT],
        }

    response = error.response
    status = response.status_code
    body: Any = None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is still worth quoting
        body = None

    detail = body.get("detail") if isinstance(body, dict) else None
    code = _first_str(detail, "code", "error_code") or _first_str(body, "code", "error_code")
    message = (
        _first_str(detail, "message", "detail", "error")
        or _first_str(body, "message", "detail", "error")
    )
    remedy = _first_str(detail, "remedy", "fix") or _first_str(body, "remedy", "fix")
    if message is None and isinstance(detail, str):
        message = detail.strip()[:_TEXT_LIMIT]
    if message is None:
        try:
            message = (response.text or "").strip()[:_TEXT_LIMIT] or None
        except Exception:  # noqa: BLE001
            message = None

    return {
        "status": status,
        "code": code,
        "message": message or f"HTTP {status}",
        "remedy": remedy,
        "retryable": is_retryable_status(status),
        "error": f"HTTP {status}: {message or ''}"[:_TEXT_LIMIT],
    }


def refusal_sentence(described: dict[str, Any]) -> str:
    """One human line naming what the server said and what to do about it."""
    parts = []
    status = described.get("status")
    code = described.get("code")
    parts.append(f"HTTP {status}" if status is not None else "no response")
    if code:
        parts.append(str(code))
    message = described.get("message")
    if message:
        parts.append(str(message))
    remedy = described.get("remedy")
    if remedy:
        parts.append(f"Remedy: {remedy}")
    return " — ".join(parts)


__all__ = [
    "RETRYABLE_STATUSES",
    "describe_bridge_failure",
    "is_retryable_status",
    "refusal_sentence",
]
