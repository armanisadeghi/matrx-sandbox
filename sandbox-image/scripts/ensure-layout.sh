#!/usr/bin/env bash
# ensure-layout.sh — create the canonical /home/agent/ directory tree.
#
# Called from every entrypoint variant (production, aidream, local) so the
# layout is identical regardless of how the sandbox booted. Idempotent —
# safe to re-run on every boot. Writes the agent-facing SANDBOX_LAYOUT.md
# inside .matrx/instructions/ so the agent sees the conventions on session
# start (it can fs_read it once and pin the layout in working memory).
#
# The agent ↔ system contract for paths inside the sandbox lives here. If
# the layout changes, update SANDBOX_LAYOUT.md in the SAME commit.

set -uo pipefail

AGENT_HOME="${AGENT_HOME:-/home/agent}"
AGENT_USER="${AGENT_USER:-agent}"

mkdir -p \
  "$AGENT_HOME/.matrx/plans" \
  "$AGENT_HOME/.matrx/skills" \
  "$AGENT_HOME/.matrx/instructions" \
  "$AGENT_HOME/.matrx/memory" \
  "$AGENT_HOME/.matrx/runtime/tool-calls" \
  "$AGENT_HOME/.matrx/runtime/shell-logs" \
  "$AGENT_HOME/.matrx/runtime/session-reports" \
  "$AGENT_HOME/cloud-files" \
  "$AGENT_HOME/repos" \
  "$AGENT_HOME/projects" \
  "$AGENT_HOME/scratch"

cat > "$AGENT_HOME/.matrx/instructions/SANDBOX_LAYOUT.md" <<'LAYOUT_EOF'
# Sandbox layout — read this first

You are inside a Matrx sandbox. The container is your machine; you have
full shell + filesystem access. The conventions below let you (and any
future you) find things without guessing.

## Workspace root

`/home/agent` — always use absolute paths. There is no per-user nesting,
no synthetic prefix, no UUID. This is your home; treat it like one.

## Top-level directories

| Path | Purpose |
|---|---|
| `/home/agent/.matrx/` | **Your private workspace.** Hidden from `ls` by default; not user-facing. Use it for plans, learned skills, persistent self-notes, memory entries, and runtime artifacts. |
| `/home/agent/cloud-files/` | The user's synced files. **Up-direction is live**: anything you write here is auto-pushed to AI Dream's `cld_files` within ~5 seconds via the in-process watcher. **Down-direction is currently boundary-only**: edits the user makes in the AI Dream UI during your session won't appear here until the next sandbox boot (the down-sync runs at startup). Live down-direction is in flight; until then, if you need the absolute latest UI-side state mid-session, run `mtx files sync down`. Treat this as a shared surface with the user. |
| `/home/agent/repos/` | Git checkouts. Convention: `repos/<owner>/<repo>` (e.g. `repos/aidream/aidream-current`). Default destination for `git clone`. |
| `/home/agent/projects/` | User-created project folders. The line between `repos/` and `projects/` is "did this come from `git clone` or did the user/agent create it from scratch." |
| `/home/agent/scratch/` | Throwaway / experimentation. Wipe freely — nothing here is precious. |

## Inside `.matrx/`

| Path | Purpose |
|---|---|
| `.matrx/plans/` | Multi-step plans you write for yourself (`<topic>.md`). Persists across turns. |
| `.matrx/skills/` | Reusable snippets / learned patterns (`<skill-name>.md`). Drop one when you discover something worth remembering. |
| `.matrx/instructions/` | Persistent self-notes ("when working on X, always Y"). This file lives here. |
| `.matrx/memory/` | Long-term memory entries — facts about the project, the user, decisions made. |
| `.matrx/runtime/tool-calls/` | Per-tool-call records. Every shell/python invocation writes a markdown file here with frontmatter (inputs, exit code, duration, conversation_id) plus full stdout/stderr. Service actions (cloud-sync uploads, etc.) write to the `_runtime/` subdirectory under the same root. **Use these as your audit trail** — if you need to know "what did I run 10 steps back," `fs_read` the relevant file. Filename: `<unix_ts>-<tool>-<short_call_id>.md`. |
| `.matrx/runtime/shell-logs/` | Legacy spill files from large shell outputs. New work writes to `tool-calls/` instead. |
| `.matrx/runtime/session-reports/` | Startup session reports written by matrx_agent's persistence module. |

## What NOT to do

- Don't put log files, temp output, or scratch data in cwd — that pollutes the workspace and makes `ls` unhelpful for the user. Use `scratch/` or `.matrx/runtime/`.
- Don't try to escape `/home/agent` for file storage. Tool calls accept absolute paths anywhere on the rootfs (you're root-equivalent inside the container) but anything outside `/home/agent` doesn't survive `Reset`. The persistent volume is mounted at `/home/agent`.
- Don't ask the user to paste file contents you can read yourself. `fs_read /home/agent/aidream/file.py` is a single tool call.

## Useful CLI extras

These are available in addition to the standard `mtx whoami` / `mtx files {ls,cat,put,rm,sync}`:

- `mtx files versions <path>` — list version history for a cloud file. **`:aidream` image only**; falls back to a friendly "spawn an :aidream sandbox" hint on `:core`/`:local`.
- `mtx files restore <path> <version>` — restore a previous version. **`:aidream` only**.
- `mtx files diff <path> <v1> <v2>` — print a unified diff between two versions. **`:aidream` only**.

The auto-sync watcher uses the same versioning under the hood; every
auto-uploaded change creates a new version, so `mtx files versions` is
the audit trail for what got pushed when.

## Why this matters

The user's view of "what's going on in the sandbox" comes from the FE
inspector reading the same paths you read. If you keep your work in
`scratch/` and `.matrx/`, their `ls /home/agent` stays clean. If you put
the user's project files in `cloud-files/`, they appear in the AI Dream
Files panel without manual upload.
LAYOUT_EOF

# Permissions — only chown if running as root (e.g., entrypoint).
if [ "$(id -u)" = "0" ]; then
  chown -R "$AGENT_USER:$AGENT_USER" \
    "$AGENT_HOME/.matrx" \
    "$AGENT_HOME/cloud-files" \
    "$AGENT_HOME/repos" \
    "$AGENT_HOME/projects" \
    "$AGENT_HOME/scratch"
fi
