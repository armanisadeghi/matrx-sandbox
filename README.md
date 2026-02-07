# AI Matrx Ephemeral Sandbox

On-demand, isolated Unix sandboxes for AI agent execution. Each sandbox is a
Docker container on EC2 that appears as a dedicated machine to the AI model —
complete with shell, filesystem, browser, and internet access.

## Architecture

```
Orchestrator API  →  Docker Container (Sandbox)
                     ├── /home/agent/   (hot storage — real files, S3-synced)
                     ├── /data/cold/    (cold storage — FUSE-mounted S3)
                     ├── Python 3.11, Node 20, Chromium, shell tools
                     └── Matrx Agent SDK
```

- **Hot storage**: Small files (config, memory, text) eagerly copied from S3 at
  startup, synced back on shutdown. Zero-latency local files.
- **Cold storage**: Large files (videos, datasets) lazily loaded via FUSE mount.
  Appear local but download on first access.
- **Lifecycle**: Create → hot sync down → FUSE mount → agent works → graceful
  shutdown → hot sync up → destroy.

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
- AWS CLI (configured) or LocalStack for local S3

### 1. Build the sandbox image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 2. Set up environment

```bash
cp .env.example .env
# Edit .env with your S3 bucket name (or use LocalStack)
```

### 3. Start the orchestrator (with LocalStack for S3)

```bash
docker compose up
```

### 4. Create a sandbox

```bash
curl -X POST http://localhost:8000/sandboxes \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test-user"}'
```

### 5. Run a command in the sandbox

```bash
curl -X POST http://localhost:8000/sandboxes/<sandbox-id>/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "whoami && python3 --version && node --version"}'
```

### 6. Destroy the sandbox

```bash
curl -X DELETE "http://localhost:8000/sandboxes/<sandbox-id>?graceful=true"
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sandboxes` | Create a new sandbox |
| `GET` | `/sandboxes` | List all sandboxes |
| `GET` | `/sandboxes/{id}` | Get sandbox details |
| `POST` | `/sandboxes/{id}/exec` | Execute a command |
| `DELETE` | `/sandboxes/{id}` | Destroy a sandbox |
| `POST` | `/sandboxes/{id}/heartbeat` | Agent heartbeat |
| `POST` | `/sandboxes/{id}/complete` | Agent signals completion |
| `POST` | `/sandboxes/{id}/error` | Agent signals error |
| `GET` | `/health` | Orchestrator health check |

## Production Deployment

See [ARMAN_TASKS.md](ARMAN_TASKS.md) for step-by-step AWS setup instructions.

## Running Tests

```bash
cd orchestrator
pip install -e ".[dev]"
pytest
```
