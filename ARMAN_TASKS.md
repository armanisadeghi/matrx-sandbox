# Arman's Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1-8 | Full AWS setup + E2E test | EC2, S3, ECR, Terraform, image build, lifecycle verified |
| 9 | Security Groups | SSH: home IP only, API: 0.0.0.0/0 |
| 10 | GitHub Secrets | 4 secrets for CI/CD |
| 11 | Rebuild on EC2 | All CRITICAL/HIGH code fixes applied |
| 12 | CI/CD Verification | 3/3 jobs passing |
| A1 | Elastic IP | `54.144.86.132` assigned |
| A2 | Systemd service | Auto-start on boot, auto-restart on crash |
| A3 | Open port 8000 | Terraform applied |
| A4 | Deploy workflow | Passes, created `matrx-sandbox-orchestrator` ECR repo |
| A5 | Delete old branch | Done |
| A6 | API key auth | Key set in systemd + `.env`, deployed to EC2 |
| A7 | Postgres sandbox store | Connected to Supabase `automation-matrix`, tested full lifecycle |
| A8 | Code deployed to EC2 | Auth, logging, Postgres store, UUID validation all live |

---

## SSH Cheat Sheet

Connect:
```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@54.144.86.132
```

Once inside:
```bash
# ─── System status ───
sudo systemctl status matrx-orchestrator   # Is the API running?
docker ps                                   # Any sandboxes running?
df -h                                       # Disk space
free -h                                     # Memory

# ─── Orchestrator logs ───
sudo journalctl -u matrx-orchestrator -f    # Live API logs
sudo journalctl -u matrx-orchestrator -n 50 # Last 50 lines

# ─── Test the API (requires API key for /sandboxes) ───
curl localhost:8000/health
curl -H "X-API-Key: $MATRX_API_KEY" localhost:8000/sandboxes

# ─── S3 storage check ───
aws s3 ls s3://matrx-sandbox-storage-prod-2024/users/

# ─── Maintenance ───
docker system prune -f
sudo dnf check-release-update
```

## API Quick Reference

All endpoints except `/health` require the `X-API-Key` header. User IDs must be valid Supabase auth UUIDs.

```bash
# Health check (no auth needed)
curl http://54.144.86.132:8000/health

# Create a sandbox (user_id must be a Supabase auth UUID)
curl -X POST http://54.144.86.132:8000/sandboxes \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"user_id": "<supabase-user-uuid>"}'

# Execute a command
curl -X POST http://54.144.86.132:8000/sandboxes/<sandbox-id>/exec \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"command": "whoami && python3 --version"}'

# List sandboxes
curl -H "X-API-Key: <key>" http://54.144.86.132:8000/sandboxes

# Destroy a sandbox
curl -X DELETE -H "X-API-Key: <key>" \
  "http://54.144.86.132:8000/sandboxes/<sandbox-id>?graceful=true"
```

---

## Infrastructure Reference

| Resource | Value |
|----------|-------|
| AWS Account | `872515272894` |
| S3 Bucket | `matrx-sandbox-storage-prod-2024` |
| EC2 Instance | `i-084f757c1e47d4efb` at `54.144.86.132` (Elastic IP) |
| ECR Repos | `matrx-sandbox` + `matrx-sandbox-orchestrator` |
| Key Pair | `matrx-sandbox-key` (~/Code/secrets/) |
| IAM Role | `matrx-sandbox-host-dev` |
| Security Group | `sg-05a1b5a6163cd8ee6` (SSH: home IP, API: 0.0.0.0/0) |
| CI/CD | `.github/workflows/ci.yml` (auto) + `deploy.yml` (manual) |
| API Key | In `.env` (local) and systemd service (EC2) |
| Supabase Project | `automation-matrix` (`txzxabzwovsujtloxrus`) |
| Supabase Table | `sandbox_instances` (RLS, triggers, indexes) |
| Orchestrator URL | `http://54.144.86.132:8000` |
