# Project Directory Structure

```
matrx-sandbox/
├── docs/                          # Architecture and design docs
│   ├── ARCHITECTURE.md            # Full system architecture
│   ├── CODE_REVIEW.md             # Code review report (45 issues)
│   └── DIRECTORY_STRUCTURE.md     # This file
│
├── sandbox-image/                 # Docker image for the sandbox container
│   ├── Dockerfile                 # Base sandbox image definition
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
│   ├── pyproject.toml
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── main.py                # FastAPI app entrypoint
│   │   ├── config.py              # Settings and environment config
│   │   ├── models.py              # Pydantic models (requests, responses)
│   │   ├── sandbox_manager.py     # Docker container lifecycle (create/destroy)
│   │   ├── storage.py             # S3 operations (bucket setup, prefix management)
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── sandboxes.py       # /sandboxes endpoints
│   │       └── health.py          # /health endpoints
│   └── tests/
│       ├── __init__.py
│       ├── test_sandbox_manager.py
│       └── test_storage.py
│
├── infra/                         # Infrastructure as code
│   ├── terraform/
│   │   ├── main.tf                # Root module
│   │   ├── variables.tf           # Input variables
│   │   ├── outputs.tf             # Outputs
│   │   ├── provider.tf            # AWS provider config
│   │   ├── s3.tf                  # S3 buckets and policies
│   │   ├── ec2.tf                 # EC2 instances and security groups
│   │   ├── iam.tf                 # IAM roles and policies
│   │   └── terraform.tfvars.example
│   └── scripts/
│       └── bootstrap-host.sh      # EC2 user-data script (install Docker, etc.)
│
├── ARMAN_TASKS.md                 # Manual AWS setup tasks (all complete)
├── LOCAL_AGENT_TASKS.md           # Cowork agent tasks (browser + file ops)
├── CLAUDE_CODE_AGENT_TASKS.md     # Claude Code agent tasks (coding + CI/CD)
├── README.md                      # Project overview and quickstart
├── .gitignore
├── .env.example                   # Example environment variables
└── docker-compose.yml             # Local dev compose (orchestrator + LocalStack)
```
