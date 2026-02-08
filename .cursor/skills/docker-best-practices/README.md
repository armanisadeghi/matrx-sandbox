# Docker Best Practices Skill

This skill teaches the AI agent how to create production-grade Dockerfiles optimized for speed, security, and minimal size.

## What This Skill Does

When you mention Docker, Dockerfiles, containers, or image optimization, this skill guides the agent to:

1. **Create optimal Dockerfiles** using multi-stage builds, BuildKit features, and layer caching
2. **Review existing Dockerfiles** against production best practices
3. **Optimize build times** using cache mounts and proper layer ordering
4. **Minimize image sizes** through base image selection and dependency management
5. **Harden security** with non-root users, minimal base images, and least privilege
6. **Generate .dockerignore** files to speed up builds
7. **Configure docker-compose.yml** with resource limits and health checks

## Files in This Skill

| File | Purpose |
|------|---------|
| **SKILL.md** | Main skill file with quick reference and common patterns |
| **reference.md** | Detailed explanations of why each pattern matters |
| **optimization.md** | Data-driven optimization strategies with benchmarks |
| **templates/** | Production-ready Dockerfile templates for common stacks |

## Templates Available

### Python FastAPI Orchestrator
**File**: `templates/python-fastapi-orchestrator.dockerfile`

Optimized for:
- FastAPI/Flask/Django services
- Multi-stage build (dev/prod targets)
- uv package manager (10x faster than pip)
- Non-root user, health checks
- Size: 340 MB (vs 1.2 GB baseline)

### Multi-Runtime Sandbox
**File**: `templates/sandbox-multi-runtime.dockerfile`

Optimized for:
- Ubuntu 22.04 with Python + Node.js
- FUSE support for S3 mounting
- AWS CLI, Chromium, system tools
- Efficient layer caching
- Size: 2.7 GB (full runtime environment)

### Next.js Web App
**File**: `templates/nextjs-web-app.dockerfile`

Optimized for:
- Next.js 16 with App Router
- Standalone output (minimal runtime)
- pnpm/npm/yarn support
- Alpine-based for small size
- Size: 280 MB (vs 1.8 GB baseline)

### Additional Templates
- `.dockerignore` template
- `docker-compose.yml` with security hardening and resource limits

## Quick Start

### Creating a New Dockerfile

1. **Choose a template** from `templates/` that matches your stack
2. **Copy to your project root** as `Dockerfile`
3. **Customize** build args, dependencies, and environment variables
4. **Build**: `docker build -t myapp:latest .`

### Reviewing an Existing Dockerfile

Ask the agent: *"Review my Dockerfile for optimization opportunities"*

The agent will check:
- Multi-stage build usage
- Layer caching efficiency
- Image size optimization
- Security hardening
- BuildKit feature utilization

### Optimizing Build Performance

Ask the agent: *"Why are my Docker builds slow?"*

The agent will analyze:
- Build context size (need `.dockerignore`?)
- Layer ordering (dependencies cached properly?)
- Cache mount usage (pip/npm caching enabled?)
- Base image selection (slim/alpine vs full?)

## Key Principles

This skill enforces these non-negotiable best practices:

1. **Never use `:latest` tags** → Always pin to specific versions
2. **Always use multi-stage builds** → Separate build deps from runtime
3. **Always create .dockerignore** → Reduce build context size
4. **Always use non-root users** → Security by default
5. **Always define health checks** → Enable self-healing
6. **Always use BuildKit** → 10x faster builds with cache mounts

## Performance Impact

Applying these best practices typically results in:

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| **Image size** | 1.2 GB | 340 MB | **72% smaller** |
| **Cold build time** | 120s | 45s | **63% faster** |
| **Warm build time** | 8s | 2s | **75% faster** |
| **Startup time** | 3.5s | 2.1s | **40% faster** |

See `optimization.md` for detailed benchmarks.

## When to Use This Skill

- Creating a new Dockerfile for Python, Node.js, or multi-runtime environments
- Reviewing/optimizing existing Dockerfiles
- Debugging slow builds or large images
- Setting up docker-compose for local development
- Configuring CI/CD for Docker image builds
- Implementing security best practices for containers

## When NOT to Use This Skill

- Running containers (use `docker run`, `docker-compose up` directly)
- Kubernetes/ECS orchestration (different patterns apply)
- Simple "run this image" scenarios (just use official images)

## Examples

### Ask the Agent

> "Create a production Dockerfile for my FastAPI app with PostgreSQL and Redis dependencies"

The agent will:
1. Read your `pyproject.toml` to understand dependencies
2. Generate a multi-stage Dockerfile using the FastAPI template
3. Include health checks for the API endpoint
4. Add docker-compose.yml with Postgres and Redis services
5. Create .dockerignore to exclude unnecessary files

> "Why is my Docker image 2 GB? It's just a Python API."

The agent will:
1. Analyze your Dockerfile layers with `docker history`
2. Identify issues (using `python:3.11` instead of `-slim`, dev deps included, etc.)
3. Suggest optimizations with expected size reduction
4. Optionally apply fixes automatically

> "Optimize my Dockerfile for faster CI/CD builds"

The agent will:
1. Check if BuildKit cache mounts are used
2. Verify layer ordering (deps before code)
3. Review .dockerignore completeness
4. Suggest GitHub Actions cache configuration
5. Estimate time savings

## Additional Resources

- **Docker BuildKit docs**: https://docs.docker.com/build/buildkit/
- **Multi-stage builds**: https://docs.docker.com/build/building/multi-stage/
- **Python container best practices**: https://testdriven.io/blog/docker-best-practices/
- **Node.js container best practices**: https://snyk.io/blog/10-best-practices-to-containerize-nodejs-web-applications-with-docker/

## Maintenance

This skill is maintained alongside the Matrx project tech stack. When dependencies update:

1. **Check base image versions** (Python, Node, Ubuntu)
2. **Update templates** with new version numbers
3. **Re-run benchmarks** in `optimization.md`
4. **Test multi-platform builds** (amd64 + arm64)

Last updated: 2026-02-08
