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
#   - returns 1 quietly ONLY when the box carries no identity at all (none of
#     MATRX_AIDREAM_URL, MATRX_AIDREAM_SERVICE_TOKEN, USER_ID,
#     ORGANIZATION_ID) — that is an unwired image, not a broken one. A box
#     carrying ANY of them and missing the rest is a defect and says so, by
#     variable name. Until 2026-09-17 the quiet branch keyed on "no URL and no
#     token", so a fully wired box whose SHELL simply could not see the
#     container env (sshd passes none of it) took the SILENT path: `git push`
#     fell through to an absent GITHUB_TOKEN and exited 0.
#
# Before it checks, this helper loads /etc/matrx/bridge-env.sh — the identity
# every entrypoint publishes out of the container environment
# (scripts/write-bridge-env.sh, which also carries the ruling on why the shell
# may hold the service token). That is what makes `ssh box 'git push'` work:
# a non-interactive shell reads neither /etc/profile nor ~/.bashrc, but it does
# run this helper.

MATRX_BRIDGE_REQUIRED_ENV="MATRX_AIDREAM_URL MATRX_AIDREAM_SERVICE_TOKEN USER_ID ORGANIZATION_ID"
MATRX_BRIDGE_ENV_FILE="${MATRX_BRIDGE_ENV_FILE:-/etc/matrx/bridge-env.sh}"
MATRX_BRIDGE_REMEDY="The orchestrator injects USER_ID and ORGANIZATION_ID into every sandbox container from the organization the create request named; a container missing one is a provisioning defect. Recreate the sandbox from a create request that carries its organization."

matrx_bridge_load_env() {
    # The container identity, for shells that sshd handed nothing. A value
    # already exported WINS — the process environment is never overridden.
    local name saved_url saved_token saved_user saved_org
    [ -r "$MATRX_BRIDGE_ENV_FILE" ] || return 0
    saved_url="${MATRX_AIDREAM_URL:-}"
    saved_token="${MATRX_AIDREAM_SERVICE_TOKEN:-}"
    saved_user="${USER_ID:-}"
    saved_org="${ORGANIZATION_ID:-}"
    # shellcheck disable=SC1090
    . "$MATRX_BRIDGE_ENV_FILE"
    [ -n "$saved_url" ] && MATRX_AIDREAM_URL="$saved_url"
    [ -n "$saved_token" ] && MATRX_AIDREAM_SERVICE_TOKEN="$saved_token"
    [ -n "$saved_user" ] && USER_ID="$saved_user"
    [ -n "$saved_org" ] && ORGANIZATION_ID="$saved_org"
    for name in $MATRX_BRIDGE_REQUIRED_ENV; do
        [ -z "${!name:-}" ] || export "$name"
    done
    return 0
}

matrx_bridge_has_no_identity() {
    # True only when NONE of the four is set anywhere.
    local name
    for name in $MATRX_BRIDGE_REQUIRED_ENV; do
        [ -z "${!name:-}" ] || return 1
    done
    return 0
}

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
    matrx_bridge_load_env
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
    if matrx_bridge_has_no_identity; then
        # NOTHING is set — an unwired image, not a defect. Any partial
        # identity falls through to the loud refusal below.
        return 1
    fi
    echo "[$label] AI Dream call refused: missing $missing. $MATRX_BRIDGE_REMEDY" >&2
    return 1
}
