#!/usr/bin/env bash
set -euo pipefail

agent_home="${HOT_PATH:-/home/agent}"
agent_user="${AGENT_USER:-agent}"
keys="${agent_home}/.ssh/authorized_keys"
admin_keys="${ADMIN_KEYS_PATH:-/opt/sandbox/config/admin_authorized_keys}"
commit_marker="/tmp/.matrx-migration-committed"

if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ] && [ ! -f "$commit_marker" ]; then
  echo "migration hold active; refusing agent-home preparation" >&2
  exit 0
fi

ensure_new_dir() {
  local path="$1"
  if [ ! -e "$path" ]; then
    mkdir "$path"
    if [ "$(id -u)" = "0" ]; then
      chown "$agent_user:$agent_user" "$path"
    fi
  fi
}

ensure_new_dir "${agent_home}"
if [ -L "${agent_home}/.ssh" ]; then
  echo "refusing symlink .ssh directory" >&2
  exit 1
fi
ensure_new_dir "${agent_home}/.ssh"
if [ -L "$keys" ]; then
  echo "refusing symlink authorized_keys" >&2
  exit 1
fi
if [ ! -e "$keys" ]; then
  : > "$keys"
  if [ "$(id -u)" = "0" ]; then
    chown "$agent_user:$agent_user" "$keys"
  fi
  chmod 700 "${agent_home}/.ssh"
  chmod 600 "$keys"
fi
while IFS= read -r key || [ -n "$key" ]; do
  [ -z "$key" ] && continue
  if ! grep -Fqx -- "$key" "$keys" 2>/dev/null; then
    [ ! -s "$keys" ] || [ "$(tail -c 1 "$keys" 2>/dev/null || true)" = "" ] || printf '\n' >> "$keys"
    printf '%s\n' "$key" >> "$keys"
  fi
done < "$admin_keys"
