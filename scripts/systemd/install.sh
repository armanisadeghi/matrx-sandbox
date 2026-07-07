#!/usr/bin/env bash
# Install (or refresh) the pull-deploy systemd units on the /srv host.
# Idempotent — safe to re-run after editing the unit files.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$DIR/pull-deploy-runner.sh" /usr/local/bin/matrx-hosted-deploy-runner
cp "$DIR/matrx-hosted-deploy.service" /etc/systemd/system/
cp "$DIR/matrx-hosted-deploy.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now matrx-hosted-deploy.timer

echo "Installed. Next runs:"
systemctl list-timers matrx-hosted-deploy.timer --no-pager
echo "Logs: journalctl -u matrx-hosted-deploy.service -f"
