# Arman's Tasks

## Completed

All initial infrastructure is deployed, tested, and operational.

| # | Task | Notes |
|---|------|-------|
| 1–8 | Full AWS setup + E2E test | EC2, S3, ECR, Terraform, image build, lifecycle verified |
| 9 | Security Groups | SSH locked to home IP |
| 10 | GitHub Secrets | 4 secrets for CI/CD |
| 11 | Rebuild on EC2 | All CRITICAL/HIGH code fixes applied |
| 12 | CI/CD Verification | 3/3 jobs passing |

---

## Remaining

| # | Task | Who | Priority |
|---|------|-----|----------|
| A1 | Allocate Elastic IP | Terminal agent | HIGH |
| A2 | Systemd service for orchestrator | Terminal agent | HIGH |
| A3 | Open port 8000 to 0.0.0.0/0 in Terraform | Terminal agent | HIGH |
| A4 | Test deploy workflow | Arman (GitHub UI) | MEDIUM |
| A5 | Delete old branch | Terminal agent | LOW |

### A1. Allocate Elastic IP

```bash
aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text
aws ec2 associate-address --instance-id i-084f757c1e47d4efb --allocation-id <ALLOCATION_ID>
```

Then update `.env` with the new static IP.

### A2. Systemd service

```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@44.204.240.45

sudo tee /etc/systemd/system/matrx-orchestrator.service <<'EOF'
[Unit]
Description=Matrx Sandbox Orchestrator
After=docker.service
Requires=docker.service

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/orchestrator
Environment=MATRX_HOST=0.0.0.0
Environment=MATRX_PORT=8000
Environment=MATRX_SANDBOX_IMAGE=matrx-sandbox:latest
Environment=MATRX_S3_BUCKET=matrx-sandbox-storage-prod-2024
Environment=MATRX_S3_REGION=us-east-1
ExecStart=/usr/bin/python3.11 -m uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable matrx-orchestrator
sudo systemctl start matrx-orchestrator
sudo systemctl status matrx-orchestrator
```

### A3. Open API port to all IPs

Once the Claude Code agent adds API key auth (Task 9), update Terraform
to allow port 8000 from anywhere (the API key protects it, not the IP):

In `infra/terraform/terraform.tfvars`, change:
```hcl
api_cidr_blocks = ["0.0.0.0/0"]
```

Then: `cd infra/terraform && terraform apply`

Keep SSH locked to your home IP — only you need that.

### A4. Test deploy workflow

Go to GitHub → Actions → "Deploy" → "Run workflow" on main.

### A5. Delete old branch

```bash
git push origin --delete claude/design-sandbox-architecture-kLhO4
```

---

## SSH Cheat Sheet

Connect:
```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@44.204.240.45
```

Once inside:
```bash
# ─── System status ───
sudo systemctl status matrx-orchestrator   # Is the API running?
docker ps                                   # Any sandboxes running?
docker ps -a                                # All containers (including stopped)
df -h                                       # Disk space
free -h                                     # Memory
docker images                               # What images are available?

# ─── Orchestrator logs ───
sudo journalctl -u matrx-orchestrator -f    # Live API logs (after systemd setup)
sudo journalctl -u matrx-orchestrator -n 50 # Last 50 lines

# ─── Test the API locally ───
curl localhost:8000/health
curl localhost:8000/sandboxes               # List all sandboxes

# ─── Peek inside a running sandbox ───
docker exec -it <container_id> bash         # Get a shell inside a sandbox
docker logs <container_id>                  # See sandbox startup logs

# ─── S3 storage check ───
aws s3 ls s3://matrx-sandbox-storage-prod-2024/users/  # See all user data

# ─── Maintenance ───
docker system prune -f                      # Clean up old containers/images
sudo dnf check-release-update               # Check for OS updates
```

---

## Infrastructure Reference

| Resource | Value |
|----------|-------|
| AWS Account | `872515272894` |
| S3 Bucket | `matrx-sandbox-storage-prod-2024` |
| EC2 Instance | `i-084f757c1e47d4efb` at `44.204.240.45` |
| ECR Repo | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| Key Pair | `matrx-sandbox-key` (~/Code/secrets/) |
| IAM Role | `matrx-sandbox-host-dev` |
| Security Group | `sg-05a1b5a6163cd8ee6` |
| CI/CD | `.github/workflows/ci.yml` (auto) + `deploy.yml` (manual) |
