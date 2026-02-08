# Arman's Tasks

## Completed

| # | Task | Notes |
|---|------|-------|
| 1-8 | Full AWS setup + E2E test | EC2, S3, ECR, Terraform, image build, lifecycle verified |
| 9 | Security Groups | SSH: home IP only, API: 0.0.0.0/0 |
| 10 | GitHub Secrets | 7 secrets: AWS creds, ECR URI, EC2 instance ID, EC2 IP, SSH key |
| 11 | Rebuild on EC2 | All CRITICAL/HIGH code fixes applied |
| 12 | CI/CD Verification | Tests pass, deploy via SSM works |
| A1 | Elastic IP | `54.144.86.132` assigned |
| A2 | Systemd service | Auto-start on boot, auto-restart on crash |
| A3 | Open port 8000 | Terraform applied |
| A4 | Deploy workflow | Push-to-main triggers auto-deploy via SSM |
| A5 | Delete old branch | Done |
| A6 | API key auth | Key set in systemd + `.env`, deployed to EC2 |
| A7 | Postgres sandbox store | Connected to Supabase `automation-matrix`, tested full lifecycle |
| A8 | Code deployed to EC2 | Auth, logging, Postgres store, UUID validation all live |
| A9 | Docker installed locally | Docker Desktop on Mac, images built and verified |
| A10 | Vercel env vars | `MATRX_ORCHESTRATOR_URL` + `MATRX_ORCHESTRATOR_API_KEY` set |
| A11 | IAM for SSM | `AmazonSSMManagedInstanceCore` on EC2 role, SSM deploy policy on CI user |

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

## Docker Commands Cheat Sheet

### Building Images Locally

```bash
# Build the sandbox image (the full agent environment — ~2.9 GB)
docker build -t matrx-sandbox:latest sandbox-image/

# Build the orchestrator image (~686 MB)
docker build -t matrx-orchestrator:latest orchestrator/

# Build for x86_64 on Apple Silicon (for testing prod-like images)
docker buildx build --platform linux/amd64 -t matrx-sandbox:latest sandbox-image/
```

### Running Locally

```bash
# Start orchestrator + LocalStack (default, no AWS needed)
docker compose up

# Start with real AWS/Supabase backends
docker compose --profile production up orchestrator-prod

# Stop everything
docker compose down

# Stop and remove volumes
docker compose down -v
```

### Image Management

```bash
# List local images
docker images | grep matrx

# Remove old images to free space
docker image prune -f

# Full cleanup (images, containers, volumes, networks)
docker system prune -af

# Check disk usage
docker system df
```

### Debugging Containers

```bash
# See running containers
docker ps

# See all containers (including stopped)
docker ps -a

# Shell into a running sandbox container
docker exec -it <container-id> bash

# View container logs
docker logs <container-id>

# Inspect container details
docker inspect <container-id>
```

---

## Customizing the Sandbox Docker Image

The sandbox image (`sandbox-image/Dockerfile`) is the environment every AI agent
runs inside. Here's how to modify it.

### Adding System Packages

Edit `sandbox-image/Dockerfile` and add to one of the `apt-get install` blocks:

```dockerfile
# Add to the system packages section
RUN apt-get update && apt-get install -y --no-install-recommends \
    your-package-here \
    && rm -rf /var/lib/apt/lists/*
```

Always include `--no-install-recommends` and clean the apt cache to keep the
image lean.

### Adding Python Packages

Add to the pip install block near the end of the Dockerfile:

```dockerfile
RUN python3 -m pip install --no-cache-dir \
    your-package-here
```

Or add them to `sandbox-image/sdk/pyproject.toml` if they're part of the Matrx
agent SDK.

### Adding Node.js Packages (Global)

```dockerfile
RUN npm install -g your-package-here
```

### Adding Your Own Python Apps

Copy your app into the image and install it:

```dockerfile
COPY your-app/ /opt/sandbox/your-app/
RUN cd /opt/sandbox/your-app && python3 -m pip install --no-cache-dir -e .
```

### Testing Changes

After editing the Dockerfile:

```bash
# 1. Rebuild the image
docker build -t matrx-sandbox:latest sandbox-image/

# 2. Test locally — spin up a one-off container
docker run --rm -it matrx-sandbox:latest bash

# 3. Verify your changes inside the container
which your-tool
python3 -c "import your_package; print(your_package.__version__)"

# 4. Push to trigger deploy (image gets built and pushed to ECR)
git add sandbox-image/Dockerfile
git commit -m "Add your-package to sandbox image"
git push origin main
```

### Keeping It Lean

- Combine related `apt-get install` commands into a single RUN layer
- Always use `--no-install-recommends` and `rm -rf /var/lib/apt/lists/*`
- Use `--no-cache-dir` for pip installs
- Don't install dev/debug tools that agents won't use — image is already ~2.9 GB
- If a tool is only needed occasionally, consider installing it at runtime
  instead of baking it into the image

### Browser / Headless Testing

Chromium and Playwright are already installed in the image. To use:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
    browser.close()
```

If you need to add Puppeteer (Node.js), install it globally:

```dockerfile
RUN npm install -g puppeteer
```

---

## Infrastructure Reference

| Resource | Value |
|----------|-------|
| AWS Account | `872515272894` |
| S3 Bucket | `matrx-sandbox-storage-prod-2024` |
| EC2 Instance | `i-084f757c1e47d4efb` at `54.144.86.132` (Elastic IP) |
| EC2 Type | `t3.xlarge` |
| ECR Repos | `matrx-sandbox` + `matrx-sandbox-orchestrator` |
| Key Pair | `matrx-sandbox-key` (~/Code/secrets/) |
| IAM Role | `matrx-sandbox-host-dev` (+ AmazonSSMManagedInstanceCore) |
| IAM CI User | `matrx-admin` (+ matrx-ssm-deploy policy) |
| Security Group | `sg-05a1b5a6163cd8ee6` (SSH: home IP, API: 0.0.0.0/0) |
| CI/CD | `.github/workflows/deploy.yml` (push to main → SSM deploy) |
| API Key | In `.env` (local) and systemd service (EC2) |
| Supabase Project | `automation-matrix` (`txzxabzwovsujtloxrus`) |
| Supabase Table | `sandbox_instances` (RLS, triggers, indexes) |
| Orchestrator URL | `http://54.144.86.132:8000` |
| GitHub Secrets | AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, ECR_REPO_URI, EC2_INSTANCE_ID, EC2_PUBLIC_IP, EC2_SSH_PRIVATE_KEY (6 total) |
