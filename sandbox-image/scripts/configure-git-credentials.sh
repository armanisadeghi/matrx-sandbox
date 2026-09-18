#!/usr/bin/env bash
# Install the canonical git credential configuration for the agent user.
#
# Two sources are supported, in order:
#   1. git-credential-cache, populated by POST /credentials
#   2. the user's refreshable AI Matrx GitHub App token, with the person's own
#      GITHUB_TOKEN vault item as the ONE explicit fallback. GH_TOKEN,
#      GITHUB_PAT and MATRX_GITHUB_TOKEN were dropped on 2026-09-18: they named
#      whatever the CONTAINER was born carrying, which silently outranked the
#      live bridge (see matrx-git-credential-env for the full account).
#
# The helper config is intentionally token-free and safe to persist in the
# user's mounted /home/agent volume.

set -euo pipefail

AGENT_HOME="${AGENT_HOME:-/home/agent}"
CACHE_SOCKET="${MATRX_GIT_CREDENTIAL_CACHE_SOCKET:-$AGENT_HOME/.matrx/runtime/git-credential-cache.sock}"
# GitHub App user tokens expire after eight hours. A year-long credential
# cache made a refreshed provider token unreachable until the container
# restarted. Thirty minutes keeps normal git operations fast while ensuring
# the helper re-enters the server-owned refresh lifecycle well before expiry.
CACHE_TIMEOUT="${MATRX_GIT_CREDENTIAL_CACHE_TIMEOUT:-1800}"
ENV_HELPER="${MATRX_GIT_ENV_HELPER:-/opt/sandbox/scripts/matrx-git-credential-env}"
GITHUB_USERNAME_DEFAULT="${GITHUB_USERNAME:-${GITHUB_USER:-x-access-token}}"

export HOME="$AGENT_HOME"

if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ]; then
    echo "migration activation preserves the mounted home; refusing git credential writes" >&2
    exit 0
fi

mkdir -p "$AGENT_HOME/.matrx/runtime"
chmod 700 "$AGENT_HOME/.matrx" "$AGENT_HOME/.matrx/runtime" 2>/dev/null || true

git config --global --unset-all credential.helper >/dev/null 2>&1 || true
git config --global --add credential.helper "cache --socket=$CACHE_SOCKET --timeout=$CACHE_TIMEOUT"
git config --global --add credential.helper "$ENV_HELPER"
git config --global credential.https://github.com.username "$GITHUB_USERNAME_DEFAULT"

# The bridge needs the WHOLE request context — actor and organization — so say
# plainly which half is missing rather than announcing a helper that will refuse
# at first use (bridge-headers.sh owns the variable list).
# shellcheck source=bridge-headers.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bridge-headers.sh"
_bridge_missing="$(matrx_bridge_missing_env)"
if [ -z "$_bridge_missing" ]; then
    echo "[git-credentials] Refreshable AI Matrx GitHub connection helper configured."
elif [ -n "${MATRX_AIDREAM_URL:-}" ] || [ -n "${MATRX_AIDREAM_SERVICE_TOKEN:-}" ]; then
    echo "[git-credentials] AI Matrx GitHub connection UNAVAILABLE: missing ${_bridge_missing}. ${MATRX_BRIDGE_REMEDY}" >&2
elif [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "[git-credentials] GitHub HTTPS credential helper configured from the GITHUB_TOKEN vault fallback."
else
    echo "[git-credentials] GitHub HTTPS helper configured; no connected account or env fallback detected."
fi
