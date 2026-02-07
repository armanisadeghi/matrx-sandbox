# Arman's Tasks

## Completed

All initial infrastructure is deployed, tested, and operational.

| # | Task | Notes |
|---|------|-------|
| 1 | AWS Account Setup | IAM user `matrx-admin`, access key generated |
| 2 | S3 Bucket | `matrx-sandbox-storage-prod-2024` (us-east-1) |
| 3 | EC2 Key Pair | `matrx-sandbox-key` at ~/Code/secrets/ |
| 4 | AWS CLI | Configured and verified |
| 5 | ECR Repository | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| 6 | Terraform Deploy | EC2 `i-084f757c1e47d4efb` at `44.204.240.45` |
| 7 | Sandbox Image | Built on EC2 (2.22GB), all code fixes applied |
| 8 | E2E Test | Full lifecycle verified: create/exec/destroy + S3 sync |
| 9 | Security Groups | Locked SSH + API to `68.5.62.36/32` |
| 10 | GitHub Secrets | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ACCOUNT_ID`, `ECR_REPO_URI` |
| 11 | Rebuild on EC2 | Image rebuilt with all CRITICAL/HIGH fixes |
| 12 | CI/CD Verification | All 3 CI jobs passing (test, build-sandbox, build-orchestrator) |

---

## Remaining

| # | Task | Priority | Notes |
|---|------|----------|-------|
| A1 | Allocate Elastic IP | HIGH | Prevents IP change on instance stop/start |
| A2 | Set up orchestrator as systemd service | HIGH | Currently runs via nohup, won't survive reboot |
| A3 | Test deploy workflow | MEDIUM | Trigger `deploy.yml` manually from GitHub Actions |
| A4 | Delete old claude branch from GitHub | LOW | `claude/design-sandbox-architecture-kLhO4` still on remote |

### A1. Allocate Elastic IP

The EC2 public IP changes when the instance stops/starts (or when Terraform updates user_data). Allocate an Elastic IP to make it permanent:

```bash
# From your terminal:
aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text
# Note the allocation ID, then:
aws ec2 associate-address --instance-id i-084f757c1e47d4efb --allocation-id <ALLOCATION_ID>
```

Then update `terraform.tfvars` if your own IP changed, and update `.env` with the new static IP.

### A2. Set up orchestrator as systemd service on EC2

The orchestrator is currently started manually. To survive reboots:

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

### A3. Test deploy workflow

Go to GitHub > Actions > "Deploy" workflow > "Run workflow" on main. This will build and push the sandbox + orchestrator images to ECR.

### A4. Delete old claude branch

```bash
git push origin --delete claude/design-sandbox-architecture-kLhO4
```

---

## Quick Test Commands

SSH to EC2:
```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@44.204.240.45
```

Health check:
```bash
curl http://44.204.240.45:8000/health
```

Create sandbox:
```bash
curl -X POST http://44.204.240.45:8000/sandboxes \
  -H "Content-Type: application/json" \
  -d '{"user_id": "my-test"}'
```

Execute command (replace SANDBOX_ID):
```bash
curl -X POST http://44.204.240.45:8000/sandboxes/<SANDBOX_ID>/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "whoami && python3 --version && echo WORKING"}'
```

Destroy sandbox:
```bash
curl -X DELETE "http://44.204.240.45:8000/sandboxes/<SANDBOX_ID>?graceful=true"
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
| Security Group | `sg-05a1b5a6163cd8ee6` (SSH + API locked to your IP) |
| CI/CD | `.github/workflows/ci.yml` (auto) + `deploy.yml` (manual) |
