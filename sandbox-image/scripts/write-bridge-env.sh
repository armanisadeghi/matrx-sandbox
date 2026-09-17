#!/usr/bin/env bash
# THE ONE place a sandbox publishes its identity to everything that is NOT the
# API daemon: shells (ssh, `docker exec`, cron), the `mtx` CLI, the git
# credential helper.
#
# Why this file exists
# --------------------
# The orchestrator injects the sandbox's identity as CONTAINER environment —
# USER_ID, ORGANIZATION_ID, MATRX_AIDREAM_URL, MATRX_AIDREAM_SERVICE_TOKEN.
# PID 1 and the API daemon (started from the entrypoint with `sudo -E`) see it.
# **sshd does not pass its own environment to a login**, so a person or agent
# who shells in saw NONE of it: `git push` fell through to an absent
# GITHUB_TOKEN and exited 0 (the connected GitHub account silently not there),
# and `mtx files ls` claimed "AI Dream not configured for this sandbox" on a
# box that is fully wired. That is a lie, and the law is that the request
# context is CARRIED, never rebuilt (common-docs/policies/
# context-is-carried-never-rebuilt.md, rule 1) and that nothing fails silently.
#
# So every entrypoint calls this script, which writes the same identity the
# daemon holds into a file that every shell path can read:
#
#   /etc/matrx/bridge-env.sh   root:agent 0640, CONTAINER filesystem only
#
# It is sourced by:
#   - /etc/profile.d/00-matrx-bridge.sh  → login shells (ssh, `bash -l`)
#   - /home/agent/.sandbox_env           → interactive shells (.bashrc)
#   - scripts/bridge-headers.sh          → EVERY shell bridge caller, whatever
#                                          shell type invoked it (this is what
#                                          covers `ssh box 'git push'`, which
#                                          reads neither profile nor bashrc)
#   - matrx_agent.bridge_headers         → the `mtx` CLI, same reason
#
# THE RULING ON THE SERVICE TOKEN (2026-09-17)
# --------------------------------------------
# The shell MAY hold MATRX_AIDREAM_SERVICE_TOKEN, because inside this container
# there is no boundary to protect and pretending otherwise would only produce
# silent failures:
#   1. The only SSH logins this image permits are `agent` and `root`
#      (Dockerfile: "AllowUsers agent root"), and `agent` has
#      `ALL=(ALL) NOPASSWD:ALL` (Dockerfile /etc/sudoers.d/agent). A shell user
#      is therefore root-equivalent and can read PID 1's full environment:
#      `sudo tr '\0' '\n' < /proc/1/environ`.
#   2. Even without sudo: the API daemon runs as `agent` (`sudo -E -u agent`),
#      so its /proc/<pid>/ is owned by `agent` and `tr '\0' '\n' <
#      /proc/<pid>/environ` hands the same token to any `agent` shell
#      (verified 2026-09-17 on a Linux 6.18 host with exactly that spawn shape).
# Withholding the token from shells buys zero confidentiality and costs an
# honest `git push`. What is genuinely wrong — and is NOT this script's to fix —
# is that the token is one SHARED master that lets any box act as any user
# against cloud-files, the secrets vault and the GitHub token endpoint. That is
# the open item at the bottom of docs/incidents/2026-09-13-platform-env-leak.md
# ("The bridge token is a shared master, not sandbox-scoped"); the remedy is a
# per-sandbox scoped token minted by aidream, at which point this file simply
# carries the scoped one.
#
# Two rules this file obeys:
#   - It lives in /etc (the container layer), NEVER in /home/agent, because the
#     home is a per-user Docker volume that outlives the container: a token
#     written there would survive rotation and follow the user to the next box.
#   - It is rewritten from the live container env on every boot, so it can
#     never disagree with the daemon.

set -euo pipefail

BRIDGE_ENV_DIR="${MATRX_BRIDGE_ENV_DIR:-/etc/matrx}"
BRIDGE_ENV_FILE="${MATRX_BRIDGE_ENV_FILE:-$BRIDGE_ENV_DIR/bridge-env.sh}"
PROFILE_DROPIN="${MATRX_BRIDGE_PROFILE_DROPIN:-/etc/profile.d/00-matrx-bridge.sh}"
BRIDGE_ENV_OWNER="${MATRX_BRIDGE_ENV_OWNER:-root:agent}"

mkdir -p "$BRIDGE_ENV_DIR"

umask 027
{
    echo "# Written by write-bridge-env.sh at container start — do not edit."
    echo "# The identity this sandbox carries into every AI Dream call."
    for name in SANDBOX_ID USER_ID ORGANIZATION_ID MATRX_AIDREAM_URL MATRX_AIDREAM_SERVICE_TOKEN; do
        value="${!name:-}"
        [ -n "$value" ] || continue
        printf 'export %s=%q\n' "$name" "$value"
    done
} > "$BRIDGE_ENV_FILE"

chown "$BRIDGE_ENV_OWNER" "$BRIDGE_ENV_FILE" 2>/dev/null || true
chmod 0640 "$BRIDGE_ENV_FILE"

# Login shells (ssh) read /etc/profile.d/*.sh. The drop-in is world-readable;
# the file it sources is not.
mkdir -p "$(dirname "$PROFILE_DROPIN")"
cat > "$PROFILE_DROPIN" <<EOF
# Matrx sandbox identity — see /opt/sandbox/scripts/write-bridge-env.sh
[ -r "$BRIDGE_ENV_FILE" ] && . "$BRIDGE_ENV_FILE"
EOF
chmod 0644 "$PROFILE_DROPIN"

missing=""
for name in USER_ID ORGANIZATION_ID MATRX_AIDREAM_URL MATRX_AIDREAM_SERVICE_TOKEN; do
    [ -n "${!name:-}" ] || missing="${missing:+$missing, }$name"
done
if [ -z "$missing" ]; then
    echo "[bridge-env] identity published to $BRIDGE_ENV_FILE (user, organization, AI Dream URL + token)"
else
    # Loud, with the remedy: a box that carries SOME identity and not the rest
    # is a provisioning defect, and every shell caller on it will refuse.
    echo "[bridge-env] WARNING: this sandbox is missing $missing — shells, the mtx CLI and git credentials will refuse AI Dream calls and say so. The orchestrator injects USER_ID and ORGANIZATION_ID into every sandbox container from the organization the create request named; recreate the sandbox from a create request that carries its organization." >&2
fi
