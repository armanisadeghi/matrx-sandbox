#!/usr/bin/env bash
# check-home-ownership.sh — the forcing function for THE HOME OWNERSHIP LAW:
#
#     every path under the agent's home is owned by the agent user.
#
# Green only when a real agent shell (uid 1000) can actually write anywhere in
# its own home. It stats the real filesystem of the real booted box — no mocks,
# no fixtures — and it FAILS on the exact condition that shipped to production
# on 2026-09-15: a fresh EC2 `bare` box whose ~/projects, ~/.cache, ~/.local and
# ~/.matrx were root:root because the root-run S3 restore created them and
# nothing chowned them back (the chokepoint now lives at the end of
# ensure-layout.sh).
#
# Run it anywhere:
#   bash /opt/sandbox/scripts/check-home-ownership.sh          # /home/agent
#   AGENT_HOME=/tmp/x AGENT_USER=agent bash .../check-home-ownership.sh
#
# Exit 0 = every path is agent-owned. Exit 1 = the offenders are named, with a
# real write attempt proving the consequence, plus the remedy.
set -uo pipefail

AGENT_HOME="${AGENT_HOME:-/home/agent}"
AGENT_USER="${AGENT_USER:-agent}"
MAX_SHOWN="${MATRX_OWNERSHIP_MAX_SHOWN:-20}"

if [ ! -d "$AGENT_HOME" ]; then
    echo "FAIL  home ownership: $AGENT_HOME does not exist" >&2
    exit 1
fi
if ! agent_uid="$(id -u "$AGENT_USER" 2>/dev/null)"; then
    echo "FAIL  home ownership: user '$AGENT_USER' does not exist in this image" >&2
    exit 1
fi

offenders="$(find "$AGENT_HOME" -xdev ! -uid "$agent_uid" -print 2>/dev/null || true)"

if [ -z "$offenders" ]; then
    total="$(find "$AGENT_HOME" -xdev -print 2>/dev/null | wc -l | tr -d ' ')"
    echo "PASS  home ownership: all $total path(s) under $AGENT_HOME are owned by $AGENT_USER (uid $agent_uid)"
    exit 0
fi

count="$(printf '%s\n' "$offenders" | wc -l | tr -d ' ')"
echo "FAIL  home ownership: $count path(s) under $AGENT_HOME are NOT owned by $AGENT_USER (uid $agent_uid)." >&2
printf '%s\n' "$offenders" | head -n "$MAX_SHOWN" | while IFS= read -r path; do
    echo "  $(stat -c '%U:%G %A' "$path" 2>/dev/null || echo '?:? ?')  $path" >&2
done
if [ "$count" -gt "$MAX_SHOWN" ]; then
    echo "  … and $((count - MAX_SHOWN)) more" >&2
fi

# Prove the consequence rather than asserting it: try the write an agent makes.
probe="$AGENT_HOME/projects/.ownership-probe-$$"
if [ -d "$AGENT_HOME/projects" ]; then
    if [ "$(id -u)" = "0" ]; then
        consequence="$(sudo -n -u "$AGENT_USER" mkdir "$probe" 2>&1 && sudo -n -u "$AGENT_USER" rmdir "$probe" 2>/dev/null; )"
    else
        consequence="$(mkdir "$probe" 2>&1 && rmdir "$probe" 2>/dev/null; )"
    fi
    [ -n "$consequence" ] && echo "  consequence: $consequence" >&2
fi

cat >&2 <<'REMEDY'
  This is the class the ensure-layout.sh ownership chokepoint exists to close:
  a boot step that runs as root (S3 hot-sync restore, a template copy, an
  install) created paths in the agent's home and nothing handed them back.
  Remedy on a live box:  sudo chown -R agent:agent /home/agent
  Real fix: make the step run before ensure-layout.sh, which repairs every
  root-owned path on every boot — and never add a root write to the home after
  it.
REMEDY
exit 1
