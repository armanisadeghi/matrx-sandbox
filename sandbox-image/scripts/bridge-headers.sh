#!/usr/bin/env bash
# THE ONE place a SHELL caller builds identity headers for an AI Dream call.
#
# The Python half of this contract is
# sandbox-image/sdk/matrx_agent/bridge_headers.py — read it for why both
# halves of the request context cross the wire. Short version: a sandbox is one
# of our servers acting for a person, so every bridge call names the actor
# (X-Matrx-User-Id) AND the organization that person is working in
# (X-Organization-Id). AI Dream REFUSES a bridge call without the organization
# (HTTP 400) rather than writing into whichever tenant the code below it would
# have defaulted to.
#
# Usage (source, don't exec):
#     . /opt/sandbox/scripts/bridge-headers.sh
#     if matrx_bridge_ready; then
#         curl -fsS "${MATRX_BRIDGE_HEADERS[@]}" "$url"
#     fi
#
# matrx_bridge_ready:
#   - returns 0 and fills the MATRX_BRIDGE_HEADERS array when every required
#     variable is set;
#   - returns 1 after SAYING on stderr exactly which variable is missing and
#     what to do about it — a bridge call is never made with half the context,
#     and never silently skipped when the box was meant to be wired.
#   - returns 1 quietly only when the box carries no AI Dream wiring at all
#     (no URL and no token) — that is an unwired image, not a broken one.

MATRX_BRIDGE_REQUIRED_ENV="MATRX_AIDREAM_URL MATRX_AIDREAM_SERVICE_TOKEN USER_ID ORGANIZATION_ID"
MATRX_BRIDGE_REMEDY="The orchestrator injects USER_ID and ORGANIZATION_ID into every sandbox container from the organization the create request named; a container missing one is a provisioning defect. Recreate the sandbox from a create request that carries its organization."

matrx_bridge_missing_env() {
    local name missing=""
    for name in $MATRX_BRIDGE_REQUIRED_ENV; do
        if [ -z "${!name:-}" ]; then
            missing="${missing:+$missing, }$name"
        fi
    done
    printf '%s' "$missing"
}

matrx_bridge_ready() {
    local label="${1:-bridge}"
    local missing
    missing="$(matrx_bridge_missing_env)"
    if [ -z "$missing" ]; then
        MATRX_BRIDGE_HEADERS=(
            -H "Authorization: Bearer ${MATRX_AIDREAM_SERVICE_TOKEN}"
            -H "X-Matrx-User-Id: ${USER_ID}"
            -H "X-Organization-Id: ${ORGANIZATION_ID}"
        )
        return 0
    fi
    MATRX_BRIDGE_HEADERS=()
    if [ -z "${MATRX_AIDREAM_URL:-}" ] && [ -z "${MATRX_AIDREAM_SERVICE_TOKEN:-}" ]; then
        # No AI Dream wiring at all — an unwired image, not a defect.
        return 1
    fi
    echo "[$label] AI Dream call refused: missing $missing. $MATRX_BRIDGE_REMEDY" >&2
    return 1
}
