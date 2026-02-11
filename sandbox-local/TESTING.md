# Sandbox Local — Testing Guide

## Prerequisites

- Docker and Docker Compose installed
- Traefik running with `proxy` network and wildcard DNS for `*.dev.codematrx.com`
- Both images built:
  ```bash
  docker build -t matrx-sandbox:core ../sandbox-image/
  docker build -t matrx-sandbox:local .
  ```

## Launch Instances

```bash
cd sandbox-local/
docker compose up -d          # Start all 5
docker compose up -d sandbox-1  # Start just one
```

## Verify Health

```bash
# All should show "healthy"
docker ps --filter "name=sandbox-" --format 'table {{.Names}}\t{{.Status}}'
```

## Web Terminal Access

Open in your browser — each URL gives you a full interactive terminal:

| Instance | URL |
|----------|-----|
| sandbox-1 | https://sandbox-1.dev.codematrx.com |
| sandbox-2 | https://sandbox-2.dev.codematrx.com |
| sandbox-3 | https://sandbox-3.dev.codematrx.com |
| sandbox-4 | https://sandbox-4.dev.codematrx.com |
| sandbox-5 | https://sandbox-5.dev.codematrx.com |

**Expected**: Black terminal background, logged in as `agent` user with a colored prompt.

## Things to Test in Each Sandbox

```bash
# Check identity
whoami              # → agent
echo $SANDBOX_ID    # → sbx-001, sbx-002, etc.
echo $SANDBOX_MODE  # → local

# Python
python3 --version   # → 3.11.x
python3 -c "import pandas; print(pandas.__version__)"
python3 -c "import matrx_tools; print('SDK OK')"

# Node
node --version      # → v20.x
npm --version

# Tools
git --version
rg --version
jq --version
tmux -V
curl --version | head -1

# Sudo works
sudo whoami         # → root

# Persistent storage — create a file, restart, verify it persists
echo "test" > ~/persist-test.txt
# Then: docker restart sandbox-1
# Reconnect and: cat ~/persist-test.txt → should show "test"
```

## Dashboard Access

The Server Manager dashboard at `https://mcp.dev.codematrx.com/admin` has a **Sandboxes** tab showing all instances with:
- Status, terminal links, detail views
- Logs, environment variables, exec commands
- Embedded terminal iframe
- Start/stop/restart controls

## Exec via API

```bash
# Run a command inside a sandbox from outside
curl -s -k -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"python3 --version"}' \
  https://mcp.dev.codematrx.com/api/sandboxes/sandbox-1/exec
```

## Stop / Restart

```bash
docker compose stop sandbox-3     # Stop one
docker compose start sandbox-3    # Start it back
docker compose restart             # Restart all
docker compose down                # Stop all (data preserved in volumes)
docker compose down -v             # Stop all AND delete volumes (data lost)
```
