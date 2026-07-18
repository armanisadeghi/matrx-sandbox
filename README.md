# AI Matrx Ephemeral Sandbox

On-demand, isolated Unix sandboxes for AI agent execution. Each sandbox is a
Docker container on EC2 that appears as a dedicated machine to the AI model —
complete with shell, filesystem, browser, and internet access.

## Architecture

```
ai-matrx-admin (Next.js) → API routes → Orchestrator API (EC2)
                                          ├── Docker Container (Sandbox)
                                          │   ├── /home/agent/   (hot storage — S3-synced)
                                          │   ├── /data/cold/    (cold storage — FUSE-mounted S3)
                                          │   ├── Python 3.11, Node 20, Chromium, shell tools
                                          │   └── Matrx Agent SDK
                                          └── Supabase Postgres (sandbox registry)
```

- **Hot storage**: Small files eagerly synced from S3 at startup, synced back on shutdown.
- **Cold storage**: Large files lazily loaded via FUSE mount (x86_64 only).
- **Persistence**: Sandbox metadata stored in Supabase Postgres with RLS per user.
- **Deploy**: Push to `main` triggers GitHub Actions → ECR build → SSM deploy to EC2.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Project Structure

```
sandbox-image/      Docker image for sandbox containers
orchestrator/       FastAPI control plane (sandbox CRUD + exec)
infra/              Terraform for AWS resources (S3, EC2, IAM)
docs/               Architecture and design documentation
```

See [docs/DIRECTORY_STRUCTURE.md](docs/DIRECTORY_STRUCTURE.md) for details.

## Quick Start (Local Development)

### Prerequisites

- Docker and Docker Compose
- Python 3.11+

### 1. Build the sandbox image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 2. Set up environment

```bash
cp .env.example .env
# Edit .env with your settings (or use defaults for LocalStack)
```

### 3. Start the orchestrator (with LocalStack for S3)

```bash
docker compose up
```

### 4. Start with real AWS/Supabase backends

```bash
docker compose --profile production up orchestrator-prod
```

### 5. Create a sandbox

```bash
curl -X POST http://localhost:8000/sandboxes \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test-user"}'
```

### 6. Run a command in the sandbox

```bash
curl -X POST http://localhost:8000/sandboxes/<sandbox-id>/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "whoami && python3 --version && node --version"}'
```

### 7. Destroy the sandbox

```bash
curl -X DELETE "http://localhost:8000/sandboxes/<sandbox-id>?graceful=true"
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sandboxes` | Create a new sandbox |
| `GET` | `/sandboxes` | List all sandboxes |
| `GET` | `/sandboxes/{sandbox_id}` | Get sandbox details |
| `POST` | `/sandboxes/{id}/exec` | Execute a command |
| `DELETE` | `/sandboxes/{sandbox_id}` | Destroy a sandbox |
| `POST` | `/sandboxes/{id}/heartbeat` | Agent heartbeat |
| `POST` | `/sandboxes/{id}/complete` | Agent signals completion |
| `POST` | `/sandboxes/{id}/error` | Agent signals error |
| `GET` | `/health` | Orchestrator health check |

All endpoints except `/health` require the `X-API-Key` header.

## Deployment

Pushing to `main` triggers automatic deployment **of both tiers**:

**EC2 tier:** GitHub Actions runs tests → builds immutable SHA-tagged candidates → deploys atomically via AWS SSM → verifies the exact source/API/filesystem contract → promotes mutable ECR aliases. Any failed verification restores the prior code and image tags.

**Hosted tier (/srv):** tests advance the CI-controlled `deploy/hosted` ref to one approved commit. The host polls only that ref every 2 min (`matrx-hosted-deploy.timer` → `scripts/deploy-hosted.sh`), builds immutable candidates, runs required migrations, promotes the complete release, and verifies the exact contract. The GHA SSH deploy job is a best-effort fast path; the poller is authoritative.

See [ARMAN_TASKS.md](ARMAN_TASKS.md) for infrastructure reference and commands.

## Running Tests

```bash
cd orchestrator
pip install -e ".[dev]"
pytest
```

Or via Make:
```bash
make test                # Unit tests
make test-integration    # Integration tests (requires Docker)
```
