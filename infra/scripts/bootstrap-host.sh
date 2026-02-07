#!/bin/bash
# EC2 User Data — Bootstrap a sandbox host with Docker and dependencies
# This runs once on first boot of the EC2 instance.
set -euo pipefail

exec > /var/log/sandbox-bootstrap.log 2>&1
echo "=== Matrx Sandbox Host Bootstrap ==="
echo "Started at $(date -u)"

# ─── Install Docker ───────────────────────────────────────────────────────────
echo "Installing Docker..."
dnf update -y
dnf install -y docker git
systemctl enable docker
systemctl start docker

# Add ec2-user to docker group
usermod -aG docker ec2-user

# ─── Install Docker Compose ──────────────────────────────────────────────────
echo "Installing Docker Compose..."
COMPOSE_VERSION="v2.24.5"
curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
    -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# ─── Install Python 3.11 ─────────────────────────────────────────────────────
echo "Installing Python 3.11..."
dnf install -y python3.11 python3.11-pip

# ─── Install the orchestrator ─────────────────────────────────────────────────
# In production, this would pull from a package registry or ECR
echo "Python and Docker are ready. Orchestrator should be deployed separately."

# ─── Configure Docker daemon ─────────────────────────────────────────────────
cat > /etc/docker/daemon.json <<'DOCKEREOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
DOCKEREOF
# Note: default-ulimits removed — conflicts with Amazon Linux 2023's
# systemd Docker config which manages ulimits via systemd service overrides.

systemctl restart docker

# ─── Pull the sandbox base image ─────────────────────────────────────────────
# In production, pull from ECR
# docker pull <account>.dkr.ecr.<region>.amazonaws.com/matrx-sandbox:latest

echo "=== Bootstrap Complete ==="
echo "Finished at $(date -u)"
