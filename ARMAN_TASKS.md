# Arman's Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1–8 | Full AWS setup + E2E test | EC2, S3, ECR, Terraform, image build, lifecycle verified |
| 9 | Security Groups | SSH locked to home IP |
| 10 | GitHub Secrets | 4 secrets for CI/CD |
| 11 | Rebuild on EC2 | All CRITICAL/HIGH code fixes applied |
| 12 | CI/CD Verification | 3/3 jobs passing |
| A1 | Elastic IP | `54.144.86.132` assigned, `.env` + refs updated |
| A2 | Systemd service | Auto-start on boot, auto-restart on crash |
| A3 | Open port 8000 | Terraform updated, API publicly accessible |
| A4 | Deploy workflow | GitHub Actions → Deploy passes (created `matrx-sandbox-orchestrator` ECR repo) |
| A5 | Delete old branch | `claude/design-sandbox-architecture-kLhO4` removed |
| A6 | API key auth deployed | Key generated, set in systemd + `.env`, latest code deployed to EC2 |

---

## Remaining (Claude Code Agent Tasks)

| # | Task | Priority | Notes |
|---|------|----------|-------|
| 6 | Structured Logging | MEDIUM | Already implemented in code, deployed to EC2 |
| 7 | Postgres-Backed Sandbox Registry | MEDIUM | Store abstraction done, needs Supabase connection |
| 8 | Integration Test Scaffolding | LOW | Full lifecycle tests against docker-compose |

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
docker ps -a                                # All containers (including stopped)
df -h                                       # Disk space
free -h                                     # Memory
docker images                               # What images are available?

# ─── Orchestrator logs ───
sudo journalctl -u matrx-orchestrator -f    # Live API logs (after systemd setup)
sudo journalctl -u matrx-orchestrator -n 50 # Last 50 lines

# ─── Test the API (requires API key for /sandboxes) ───
curl localhost:8000/health
curl -H "X-API-Key: $MATRX_API_KEY" localhost:8000/sandboxes

# ─── Peek inside a running sandbox ───
docker exec -it <container_id> bash         # Get a shell inside a sandbox
docker logs <container_id>                  # See sandbox startup logs

# ─── S3 storage check ───
aws s3 ls s3://matrx-sandbox-storage-prod-2024/users/  # See all user data

# ─── Maintenance ───
docker system prune -f                      # Clean up old containers/images
sudo dnf check-release-update               # Check for OS updates
```

## API Quick Reference

All endpoints except `/health` require the `X-API-Key` header.

```bash
# Health check (no auth needed)
curl http://54.144.86.132:8000/health

# Create a sandbox
curl -X POST http://54.144.86.132:8000/sandboxes \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"user_id": "test-user"}'

# Execute a command
curl -X POST http://54.144.86.132:8000/sandboxes/<sandbox-id>/exec \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"command": "whoami && python3 --version"}'

# List sandboxes
curl -H "X-API-Key: <key>" http://54.144.86.132:8000/sandboxes

# Destroy a sandbox
curl -X DELETE -H "X-API-Key: <key>" "http://54.144.86.132:8000/sandboxes/<sandbox-id>?graceful=true"
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
| Security Group | `sg-05a1b5a6163cd8ee6` (SSH: home IP only, API: 0.0.0.0/0) |
| CI/CD | `.github/workflows/ci.yml` (auto) + `deploy.yml` (manual) |
| API Key | Set in `.env` (local) and systemd service (EC2) |
