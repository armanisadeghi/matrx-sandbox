#!/usr/bin/env bash
# Install the canonical git credential configuration for the agent user.
#
# Two sources are supported, in order:
#   1. git-credential-cache, populated by POST /credentials
#   2. the user's refreshable AI Matrx GitHub App token, with injected PATs
#      retained as the explicit fallback for local/legacy environments.
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

mkdir -p "$AGENT_HOME/.matrx/runtime"
chmod 700 "$AGENT_HOME/.matrx" "$AGENT_HOME/.matrx/runtime" 2>/dev/null || true

git config --global --unset-all credential.helper >/dev/null 2>&1 || true
git config --global --add credential.helper "cache --socket=$CACHE_SOCKET --timeout=$CACHE_TIMEOUT"
git config --global --add credential.helper "$ENV_HELPER"
git config --global credential.https://github.com.username "$GITHUB_USERNAME_DEFAULT"

if [ -n "${MATRX_AIDREAM_URL:-}" ] && [ -n "${MATRX_AIDREAM_SERVICE_TOKEN:-}" ] && [ -n "${USER_ID:-}" ]; then
    echo "[git-credentials] Refreshable AI Matrx GitHub connection helper configured."
elif [ -n "${GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_PAT:-${MATRX_GITHUB_TOKEN:-}}}}" ]; then
    echo "[git-credentials] GitHub HTTPS credential helper configured from injected env fallback."
else
    echo "[git-credentials] GitHub HTTPS helper configured; no connected account or env fallback detected."
fi
