# Docker Best Practices — Detailed Reference

## Why Multi-Stage Builds Matter

Multi-stage builds separate build-time dependencies from runtime dependencies, resulting in significantly smaller final images.

### Size Comparison

| Pattern | Final Image Size | Build Tools in Image? |
|---------|------------------|----------------------|
| Single-stage with build tools | 1.2 GB | Yes (wasted space) |
| Multi-stage (build → runtime) | 350 MB | No |
| Multi-stage + distroless | 150 MB | No |

### Example: FastAPI Service

**Before (single-stage):**
```dockerfile
FROM python:3.11
WORKDIR /app
RUN apt-get update && apt-get install -y build-essential  # 500 MB of compilers
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app"]
```

**After (multi-stage):**
```dockerfile
FROM python:3.11 AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y build-essential
COPY requirements.txt .
RUN pip install --user -r requirements.txt

FROM python:3.11-slim  # Much smaller base
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .
ENV PATH=/root/.local/bin:$PATH
CMD ["uvicorn", "main:app"]
```

Result: **60% smaller image**, no build tools in production container.

## BuildKit Features

BuildKit is Docker's modern build engine. Enable it globally:

```bash
export DOCKER_BUILDKIT=1
echo 'export DOCKER_BUILDKIT=1' >> ~/.zshrc
```

### Cache Mounts

Cache mounts persist across builds, dramatically speeding up dependency installs.

**Without cache mount:**
```dockerfile
RUN pip install -r requirements.txt  # Downloads every build
```
- First build: 45 seconds
- Second build (no changes): 45 seconds (re-downloads everything)

**With cache mount:**
```dockerfile
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt
```
- First build: 45 seconds
- Second build (no changes): 2 seconds (uses cache)

### How It Works

1. `--mount=type=cache,target=/root/.cache/pip` tells BuildKit to persist `/root/.cache/pip` across builds
2. pip downloads packages to that cache on first build
3. Subsequent builds reuse downloaded packages from cache
4. Only new/changed packages are downloaded

### Common Cache Mount Targets

| Tool | Cache Target |
|------|--------------|
| pip | `/root/.cache/pip` |
| npm | `/root/.npm` |
| pnpm | `/root/.local/share/pnpm/store` |
| apt | `/var/cache/apt` + `/var/lib/apt` |
| go | `/go/pkg/mod` |
| cargo | `/usr/local/cargo/registry` |

### Sharing Cache Across Services

Use `sharing=locked` for caches accessed concurrently:

```dockerfile
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y package-name
```

This prevents corruption when multiple builds run in parallel.

## Base Image Selection

### Python

| Base Image | Size | Use Case |
|------------|------|----------|
| `python:3.11` | 1.01 GB | Full Debian with all tools (dev only) |
| `python:3.11-slim` | 130 MB | Minimal Debian (best for most apps) |
| `python:3.11-alpine` | 50 MB | Alpine Linux (avoid: breaks C extensions) |
| `gcr.io/distroless/python3` | 52 MB | Google distroless (no shell, max security) |

**Recommendation**: Use `-slim` for production. Avoid Alpine unless you understand the musl vs glibc tradeoffs (many wheels break on Alpine).

### Node.js

| Base Image | Size | Use Case |
|------------|------|----------|
| `node:20` | 1.1 GB | Full Debian (dev only) |
| `node:20-slim` | 240 MB | Minimal Debian (best for most apps) |
| `node:20-alpine` | 135 MB | Alpine (works well for Node, unlike Python) |
| `gcr.io/distroless/nodejs20-debian12` | 180 MB | Distroless (no shell) |

**Recommendation**: `-alpine` works well for Node.js (no native extension issues). Use `-slim` if you need Debian tooling.

### Ubuntu

| Base Image | Size | Use Case |
|------------|------|----------|
| `ubuntu:22.04` | 77 MB | Full Ubuntu (when you need standard Linux) |
| `ubuntu:22.04-minimal` | 29 MB | Minimal Ubuntu (no man pages, docs) |

**Recommendation**: Use versioned tags like `ubuntu:22.04.5` (not `ubuntu:22.04`) for reproducibility.

## Layer Caching Strategy

Docker caches layers based on their content hash. When a layer changes, all subsequent layers are invalidated.

### Optimal Ordering

1. **System packages** (rarely change)
2. **Language runtime** (rarely change)
3. **Dependency manifests** (change occasionally)
4. **Dependency install** (change occasionally)
5. **Application code** (changes frequently)

### Example: FastAPI Application

```dockerfile
FROM python:3.11-slim

# 1. System packages — cached until base image changes
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Create user — cached unless user config changes
RUN useradd -m app

# 3. Dependency manifest — cached unless pyproject.toml changes
COPY pyproject.toml .

# 4. Install dependencies — cached unless pyproject.toml changes
RUN pip install .

# 5. Application code — rebuilt on every code change
COPY . .

USER app
CMD ["uvicorn", "main:app"]
```

### What NOT to Do

```dockerfile
# ❌ BAD: Copies all code before installing deps
COPY . .
RUN pip install -r requirements.txt
```

Every code change invalidates the pip install layer, causing full re-download.

```dockerfile
# ✅ GOOD: Installs deps first
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
```

Dependency install is cached unless `requirements.txt` changes.

## Health Checks

Health checks allow orchestrators (Docker, Compose, Kubernetes, ECS) to know when a container is ready and detect failures.

### Why They Matter

- **Startup time**: Don't route traffic to a container until it's ready
- **Self-healing**: Restart containers that fail health checks
- **Rolling updates**: Wait for new container to be healthy before stopping old one

### Python API Example

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=2)"
```

Flags:
- `--interval=30s`: Check every 30 seconds
- `--timeout=5s`: Health check must complete within 5 seconds
- `--start-period=10s`: Grace period on startup (don't fail health checks for first 10s)
- `--retries=3`: Mark unhealthy after 3 consecutive failures

### Node.js API Example

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD node -e "require('http').get('http://localhost:3000/health', (r) => process.exit(r.statusCode === 200 ? 0 : 1))"
```

### Lightweight Alternatives

If you don't want to bundle an HTTP client:

```dockerfile
# Use curl (requires curl in image)
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1

# Process check (less reliable, but no network required)
HEALTHCHECK CMD pgrep -x python3 || exit 1
```

### Implementing `/health` Endpoint

**FastAPI:**
```python
@app.get("/health")
def health():
    return {"status": "ok"}
```

**Advanced health check:**
```python
@app.get("/health")
async def health(db: Database = Depends(get_db)):
    # Check database
    await db.execute("SELECT 1")
    
    # Check S3 (example)
    s3 = boto3.client("s3")
    s3.head_bucket(Bucket=settings.bucket_name)
    
    return {"status": "ok", "checks": {"db": "ok", "s3": "ok"}}
```

## Security Hardening

### Non-Root User

**Why**: If an attacker exploits your app, they're confined to a non-root user with limited privileges.

```dockerfile
# Create user with specific UID/GID
RUN groupadd -g 1000 app && \
    useradd -m -u 1000 -g app -s /bin/bash app

# Create directories with correct ownership
RUN mkdir -p /app /data && \
    chown -R app:app /app /data

USER app
WORKDIR /app
```

**Testing**: Run `docker exec <container> whoami` — should print `app`, not `root`.

### Read-Only Root Filesystem

Prevent attackers from modifying files in the container.

```yaml
# docker-compose.yml
services:
  api:
    read_only: true
    tmpfs:
      - /tmp  # Writable tmpfs for temp files
      - /var/cache
```

Or via `docker run`:
```bash
docker run --read-only --tmpfs /tmp myimage
```

### Drop Capabilities

Linux capabilities are fine-grained privileges. Most apps don't need any.

```yaml
# docker-compose.yml
services:
  api:
    cap_drop:
      - ALL  # Drop all capabilities
    security_opt:
      - no-new-privileges:true  # Prevent privilege escalation
```

### No Secrets in Images

**❌ NEVER:**
```dockerfile
ENV DATABASE_URL="postgresql://user:password@host/db"
```

**✅ DO:**
```dockerfile
# Use runtime environment variables (docker-compose.yml, k8s secrets)
ENV DATABASE_URL=""
```

**✅ OR:**
```dockerfile
# Use BuildKit secrets (not persisted in image)
RUN --mount=type=secret,id=db_url \
    DATABASE_URL=$(cat /run/secrets/db_url) ./setup.sh
```

## .dockerignore Best Practices

`.dockerignore` excludes files from the build context (what's sent to Docker daemon).

### Why It Matters

**Without .dockerignore:**
- Build context includes `.git/`, `node_modules/`, venv, etc.
- Sending 1 GB to Docker daemon takes 10+ seconds before build even starts
- Large context slows down CI/CD

**With .dockerignore:**
- Build context is 10-50 MB
- Instant context transfer

### Template

```
# Version control
.git/
.github/
.gitignore
.gitattributes

# Dependencies (excluded because they're installed in Dockerfile)
node_modules/
__pycache__/
*.pyc
.venv/
venv/
.pytest_cache/
.mypy_cache/

# Build artifacts
dist/
build/
*.egg-info/
target/
.next/

# IDE
.vscode/
.idea/
*.swp
.DS_Store

# Environment (NEVER include real secrets)
.env
.env.*
!.env.example

# Docs (reduce context size)
docs/
*.md
!README.md

# CI/CD
.github/
.gitlab-ci.yml
Makefile

# Misc
*.log
.coverage
coverage/
```

## FUSE and Privileged Containers

Your sandbox image uses FUSE (mountpoint-s3) for cold storage. This requires special permissions.

### Required Docker Flags

```yaml
# docker-compose.yml
services:
  sandbox:
    cap_add:
      - SYS_ADMIN  # Required for FUSE mount
    devices:
      - /dev/fuse:/dev/fuse
    security_opt:
      - apparmor:unconfined  # Or custom AppArmor profile
```

Or via `docker run`:
```bash
docker run --cap-add SYS_ADMIN --device /dev/fuse sandbox:latest
```

### Security Trade-offs

`SYS_ADMIN` is a powerful capability. Mitigate risks:

1. **Network isolation**: Use `--network=none` or dedicated bridge network
2. **Resource limits**: Enforce CPU/memory limits
3. **Ephemeral containers**: Destroy after use (which you already do)
4. **User namespaces**: Map container root to non-root host user (advanced)

### Alternative: Sidecar Pattern

If you need tighter security, run FUSE mount in a privileged sidecar, share volume with unprivileged app container.

```yaml
services:
  fuse-mounter:
    image: fuse-sidecar:latest
    cap_add:
      - SYS_ADMIN
    volumes:
      - cold-storage:/mnt
  
  app:
    image: app:latest
    cap_drop:
      - ALL  # No special permissions
    volumes:
      - cold-storage:/data/cold:ro  # Read-only access to mounted volume
```

## Python-Specific Optimizations

### Using uv Instead of pip

[uv](https://github.com/astral-sh/uv) is a blazing-fast Python package installer (10-100x faster than pip).

```dockerfile
FROM python:3.11-slim

# Install uv
RUN pip install --no-cache-dir uv

# Use uv for all installs
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt
```

### Compiled Python (cython, mypyc)

For performance-critical services, compile Python to C:

```dockerfile
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y build-essential
COPY . .
RUN pip install cython && python setup.py build_ext --inplace

FROM python:3.11-slim
COPY --from=builder /app /app
CMD ["python", "main.py"]
```

### Distroless Python

Google's distroless images contain only your app and runtime dependencies (no shell, no package manager).

```dockerfile
FROM python:3.11-slim AS builder
RUN pip install --user -r requirements.txt

FROM gcr.io/distroless/python3-debian12
COPY --from=builder /root/.local /root/.local
COPY . /app
ENV PATH=/root/.local/bin:$PATH
WORKDIR /app
CMD ["main.py"]
```

**Tradeoffs:**
- ✅ Smaller attack surface (no shell to exploit)
- ✅ Smaller image size
- ❌ Harder to debug (can't `docker exec` into it)
- ❌ No shell for startup scripts

Use distroless for production APIs where you don't need to exec into containers.

## Node.js-Specific Optimizations

### Use npm ci, Not npm install

```dockerfile
# ❌ BAD: Modifies package-lock.json
RUN npm install

# ✅ GOOD: Installs exact versions from lockfile
RUN npm ci --prefer-offline
```

### Cache node_modules with BuildKit

```dockerfile
COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline
COPY . .
```

### Prune devDependencies

```dockerfile
FROM node:20-slim AS builder
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
COPY package*.json ./
RUN npm ci --omit=dev  # Prod deps only
COPY --from=builder /app/dist ./dist
CMD ["node", "dist/main.js"]
```

### Distroless Node

```dockerfile
FROM node:20-slim AS builder
RUN npm ci && npm run build

FROM gcr.io/distroless/nodejs20-debian12
COPY --from=builder /app/dist /app
WORKDIR /app
CMD ["main.js"]
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Build and Push Docker Image

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
      
      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2
      
      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ secrets.ECR_REPO_URI }}/app:latest
          cache-from: type=registry,ref=${{ secrets.ECR_REPO_URI }}/app:buildcache
          cache-to: type=registry,ref=${{ secrets.ECR_REPO_URI }}/app:buildcache,mode=max
```

### Multi-Platform Builds for ARM + x86

```yaml
- name: Build multi-platform
  uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    push: true
    tags: org/app:latest
```

## Image Size Benchmarks

Based on real-world FastAPI application:

| Configuration | Image Size | Build Time | Startup Time |
|---------------|------------|------------|--------------|
| `python:3.11` + all deps | 1.2 GB | 120s | 3.5s |
| `python:3.11-slim` + multi-stage | 340 MB | 90s | 2.1s |
| Alpine + multi-stage | 180 MB | 180s | 2.8s |
| Distroless + multi-stage | 160 MB | 95s | 1.9s |

**Recommendation**: `-slim` + multi-stage offers the best balance of size, build time, and compatibility.

## Troubleshooting

### Build is slow even with cache mounts

1. Check if BuildKit is enabled: `docker buildx version`
2. Use `--progress=plain` to see detailed build output
3. Verify cache mount syntax (no spaces before `\`)

### Image is larger than expected

1. Use `docker history <image>` to see layer sizes
2. Common culprits:
   - Forgetting `rm -rf /var/lib/apt/lists/*`
   - Installing dev dependencies in production stage
   - Copying `.git/` or `node_modules/` (fix with `.dockerignore`)

### Can't exec into distroless container

Distroless has no shell. Use debug variants:

```dockerfile
FROM gcr.io/distroless/python3-debian12:debug
```

Or use a debug sidecar:

```bash
docker run --pid=container:<container-id> --net=container:<container-id> \
  --cap-add SYS_PTRACE \
  busybox sh
```

### FUSE mount fails in container

1. Check host has `/dev/fuse`: `ls -l /dev/fuse`
2. Verify `--cap-add SYS_ADMIN` and `--device /dev/fuse`
3. Check AppArmor/SELinux isn't blocking (try `--privileged` to test)
4. On EC2, ensure instance type supports FUSE
