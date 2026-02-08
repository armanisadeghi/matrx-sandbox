# Project Directory Structure

```
matrx-sandbox/
├── docs/                          # Architecture and design docs
│   ├── ARCHITECTURE.md            # Full system architecture
│   ├── CODE_REVIEW.md             # Code review report (40 issues)
│   └── DIRECTORY_STRUCTURE.md     # This file
│
├── sandbox-image/                 # Docker image for the sandbox container
│   ├── Dockerfile                 # Base sandbox image (multi-arch: x86_64 + ARM64)
│   ├── scripts/
│   │   ├── entrypoint.sh          # Container entrypoint (orchestrates startup)
│   │   ├── hot-sync.sh            # Hot storage S3 sync (up/down)
│   │   ├── cold-mount.sh          # FUSE mount/unmount for cold storage
│   │   ├── shutdown.sh            # Graceful shutdown handler
│   │   └── healthcheck.sh         # Health check for orchestrator polling
│   ├── config/
│   │   └── sandbox.conf           # Default sandbox configuration
│   └── sdk/                       # Custom Matrx SDK installed in sandbox
│       ├── pyproject.toml
│       └── matrx_agent/
│           ├── __init__.py
│           └── client.py          # Agent-side utilities
│
├── orchestrator/                  # Control plane — manages sandbox lifecycle
│   ├── Dockerfile                 # Orchestrator container image
│   ├── pyproject.toml
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── main.py                # FastAPI app entrypoint
│   │   ├── config.py              # Settings and environment config
│   │   ├── models.py              # Pydantic models (requests, responses, config, ttl)
│   │   ├── sandbox_manager.py     # Docker container lifecycle (create/destroy/mark_stopped)
│   │   ├── store.py               # SandboxStore ABC + InMemory + Postgres implementations
│   │   ├── middleware.py          # API key auth, request logging
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── sandboxes.py       # /sandboxes endpoints
│   │       └── health.py          # /health endpoints
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py            # Test fixtures, mock Docker, integration flag
│       ├── test_sandbox_manager.py # 25 unit tests
│       ├── test_storage.py
│       └── test_integration.py    # Integration tests (requires running orchestrator)
│
├── infra/                         # Infrastructure as code
│   ├── terraform/
│   │   ├── main.tf                # Root module
│   │   ├── variables.tf           # Input variables
│   │   ├── outputs.tf             # Outputs
│   │   ├── provider.tf            # AWS provider config
│   │   ├── s3.tf                  # S3 buckets and policies
│   │   ├── ec2.tf                 # EC2 instances and security groups
│   │   ├── iam.tf                 # IAM roles and policies (incl. SSM)
│   │   └── terraform.tfvars.example
│   └── scripts/
│       └── bootstrap-host.sh      # EC2 user-data script (install Docker, etc.)
│
├── .github/
│   └── workflows/
│       ├── ci.yml                 # Auto: lint + test on PR/push
│       └── deploy.yml             # Auto on main: test → ECR build → SSM deploy
│
├── ARMAN_TASKS.md                 # Manual tasks, docker commands, image customization guide
├── LOCAL_AGENT_TASKS.md           # Browser agent tasks (all complete)
├── CLAUDE_CODE_AGENT_TASKS.md     # Claude Code agent tasks (coding + CI/CD)
├── README.md                      # Project overview and quickstart
├── Makefile                       # make test, make test-integration
├── .gitignore
├── .env.example                   # Example environment variables
└── docker-compose.yml             # Local dev: default (LocalStack) + production profile
```
