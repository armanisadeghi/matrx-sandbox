# Matrx Sandbox: Client Integration Guide

This guide documents the newly available Sandbox API endpoints that power the Next.js `matrx-admin` cloud editor.

All endpoints are hosted by the orchestrator and proxy directly into the fast `matrx_agent` daemon running inside the container.

---

## 1. File System Operations (`/sandboxes/{id}/fs/...`)

The new filesystem API replaces the old `exec` shell approach with native, high-performance operations.

### Basic CRUD
*   `GET /fs/list?path=/home/agent` -> Returns JSON `entries` array with names, sizes, modes, and kinds (file/dir).
*   `GET /fs/stat?path=/home/agent/file.txt` -> Returns stat info for a single path.
*   `GET /fs/read?path=/home/agent/file.txt&encoding=utf8` -> Streams the file content directly. Use `encoding=base64` for binary files.
*   `PUT /fs/write` -> Overwrites a file atomically. Body: `{"path": "...", "content": "...", "create_parents": true}`.
*   `POST /fs/patch` -> Apply diffs to a file. Body: `{"path": "...", "edits": [{"start": 0, "end": 10, "replacement": "..."}]}`.
*   `DELETE /fs/delete?path=...&recursive=true` -> Deletes a file or folder.
*   `POST /fs/mkdir` -> Body: `{"path": "...", "parents": true}`.
*   `POST /fs/rename`, `POST /fs/copy` -> Body: `{"from_path": "...", "to_path": "..."}`.

### Bulk Operations & Zip
*   `POST /fs/upload` -> Multipart form upload for dragging and dropping files.
*   `GET /fs/download?path=/home/agent/src&format=zip` -> Downloads an entire directory structure as a compressed zip file.
*   `POST /fs/batch` -> Execute multiple delete/mkdir actions in one request.

### Real-time File Watcher
*   `ws://{orchestrator}/sandboxes/{id}/fs/watch?path=/home/agent`
*   Connect via WebSocket to receive a stream of JSON events when files change on disk (`created`, `deleted`, `modified`, `moved`).

---

## 2. Interactive Terminal (PTY) & Execution

### True Terminal (WebSocket)
*   `ws://{orchestrator}/sandboxes/{id}/pty?cols=120&rows=30`
*   Provides a true pseudo-terminal running `bash`. Forward raw bytes directly to `xterm.js`.
*   Send JSON control frames like `{"type": "resize", "cols": 100, "rows": 40}` or `{"type": "signal", "name": "SIGINT"}`.

### Streaming Background Exec
*   `POST /exec/stream` -> Body: `{"command": "npm install", "cwd": "/home/agent"}`
*   Returns an HTTP **Server-Sent Events (SSE)** stream. Parses standard output and standard error continuously without buffering.

---

## 3. Git Workflows

No need to shell out manually. The daemon now exposes robust git endpoints running inside the agent's filesystem.

*   `POST /git/clone` -> Body: `{"url": "...", "dest": "...", "branch": "..."}`
*   `GET /git/status`, `GET /git/diff`, `GET /git/log` -> Returns JSON-parsed git states.
*   `POST /git/commit`, `POST /git/push`, `POST /git/pull`
*   `POST /git/branch`, `POST /git/stash`

### Git Credentials Helper
*   `POST /credentials` -> Body: `{"kind": "github", "token": "ghp_..."}`. Safely configures `git config --global credential.helper` using a protected in-memory/chmod-restricted script. The token is never stored in plaintext history or `.git-credentials`.
*   `POST /credentials/revoke` -> Destroys the helper script and unsets git config.

---

## 4. Process & Port Management

*   `GET /processes` -> Returns a JSON list of running processes inside the sandbox (like `ps aux`).
*   `POST /processes/{pid}/signal` -> Sends an OS signal (e.g. `SIGTERM`, `SIGKILL`) to a specific process ID.
*   `GET /ports` -> Scans the sandbox network namespace and returns a JSON array of active, listening TCP ports (e.g. `[3000, 8080]`). Use this to dynamically populate your UI's "Port Forwarding" or "Preview URL" panels.
