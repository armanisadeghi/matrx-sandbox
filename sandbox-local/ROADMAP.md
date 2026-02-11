# Sandbox Local — Roadmap & Ideas

## Missing / High Priority

- **Authentication on ttyd**: Currently no credentials on the web terminal. Add `TTYD_USER` and `TTYD_PASS` env vars per instance for basic auth, or implement Traefik middleware auth.
- **Dynamic instance creation**: 1-click from dashboard — pull latest `sandbox-local` image, auto-assign name/subdomain, add to Traefik. No manual compose file editing.
- **Resource monitoring**: Per-sandbox CPU/memory graphs over time (lightweight — could use cAdvisor or simple polling).
- **Volume backup/restore**: Export a sandbox's `/home/agent` volume to a tarball and restore it to another instance.
- **Instance naming**: Let users label sandboxes (e.g., "ML Experiments", "Client Demo") beyond `sandbox-1`.

## LLM Integration

- **In-sandbox LLM agent**: Run a lightweight LLM (Ollama with small models) inside each sandbox, giving it tool access to the file system, shell, and browser.
- **Chat UI**: Add a simple web chat interface per sandbox that talks to the LLM agent — user sends prompts, agent executes in the sandbox.
- **MCP bridge**: Expose each sandbox's tools via MCP so external agents (Cursor, Claude Desktop) can control them remotely.
- **Multi-provider**: Support routing to cloud LLM providers (OpenAI, Anthropic, etc.) when local models aren't sufficient.

## UI / UX

- **Code editor**: Add code-server (VS Code in browser) as an optional service per sandbox — full IDE experience alongside the terminal.
- **File browser**: Web-based file manager for `/home/agent` — upload, download, edit files without terminal.
- **Split pane**: Terminal + file browser + chat in a single view.
- **Sandbox templates**: Pre-configured environments (Python ML, Node.js API, data analysis) that come with specific packages pre-installed.

## Operational

- **Auto-shutdown**: Idle sandboxes stop after X minutes to save resources. Wake on first HTTP request via Traefik.
- **Snapshot/clone**: Snapshot a running sandbox and clone it — useful for branching experiments.
- **Shared filesystem**: Mount a shared read-only volume across all sandboxes for common datasets or tools.
- **Log aggregation**: Centralized log viewer across all sandboxes in the dashboard.
- **Health alerts**: Notify (webhook/email) when a sandbox goes unhealthy or hits resource limits.

## Architecture

- **Image auto-update**: Dashboard button or cron to rebuild `matrx-sandbox:local` from latest git and rolling-restart instances.
- **Multi-host**: Run sandboxes across multiple servers with a central orchestrator. The existing FastAPI orchestrator in `orchestrator/` is designed for this.
- **GPU support**: For ML sandboxes, pass through NVIDIA GPUs with `--gpus` flag. Requires nvidia-container-toolkit on host.
- **Persistent services**: Let sandboxes optionally run long-lived services (web servers, databases) with additional Traefik routes per sandbox (e.g., `sandbox-1-app.dev.codematrx.com`).
