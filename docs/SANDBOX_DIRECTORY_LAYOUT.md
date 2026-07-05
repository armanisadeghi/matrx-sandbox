# Sandbox Directory Layout

Canonical layout created at startup by [`sandbox-image/scripts/ensure-layout.sh`](../sandbox-image/scripts/ensure-layout.sh). Called from every entrypoint variant (production, aidream, local). Idempotent — safe to re-run on every boot.

**Workspace root:** `/home/agent` — hot storage, default CWD, persistent volume (hosted tier).

## Directory tree

```
/home/agent/
├── .matrx/                         # agent-private workspace
│   ├── plans/                      # multi-step plans (<topic>.md)
│   ├── skills/                     # reusable patterns (<skill-name>.md)
│   ├── instructions/               # self-notes; includes SANDBOX_LAYOUT.md
│   ├── memory/                     # long-term memory entries
│   ├── locks/                      # in-progress operation flags (runtime)
│   ├── session.json                # session manifest — current snapshot (runtime)
│   ├── session.prev.json           # prior manifest, rotated on each write (runtime)
│   ├── session-report.md           # human-readable "what we restored" (runtime)
│   └── runtime/
│       ├── tool-calls/             # per-tool-call audit trail
│       ├── shell-logs/             # legacy large shell output spill
│       └── session-reports/        # reserved dir (ensure-layout); report lives at .matrx/ root
├── cloud-files/                    # AI Dream synced files (live upload)
├── repos/                          # git clones → repos/<owner>/<repo>
├── projects/                       # user/agent-created work (not from clone)
└── scratch/                        # throwaway / experiments
```

Directories under `.matrx/` are created at boot by `ensure-layout.sh`. The `locks/` dir and the three session files appear once the `matrx_agent` daemon starts (persistence module).

## Top-level directories

| Path | Purpose |
|---|---|
| `.matrx/` | Agent-private workspace: plans, skills, memory, runtime artifacts |
| `cloud-files/` | User's synced files; writes auto-push to AI Dream |
| `repos/` | Git checkouts (convention: `repos/<owner>/<repo>`) |
| `projects/` | Non-cloned project folders |
| `scratch/` | Ephemeral work; safe to wipe |

## Runtime files under `.matrx/` (persistence)

Written by [`matrx_agent.persistence`](../sandbox-image/sdk/matrx_agent/persistence/) — not created by `ensure-layout.sh`.

| Path | Purpose |
|---|---|
| `session.json` | Machine-readable manifest: git repos, cwd, ports, processes, auto-stash results. Written every 5 min + on shutdown. |
| `session.prev.json` | Previous manifest, rotated when `session.json` is rewritten. Startup reads this for the session report. |
| `session-report.md` | Human-readable summary for the frontend welcome panel. Rendered on startup from the prior manifest. |
| `locks/checkpoint` | Transient marker while a periodic checkpoint is writing; prevents overlap with shutdown. |

## System paths (outside `/home/agent`)

From [`sandbox-image/config/sandbox.conf`](../sandbox-image/config/sandbox.conf):

| Path | Purpose |
|---|---|
| `/data/cold` | Cold storage (FUSE-mounted S3, x86_64 only) |
| `/tmp/s3cache` | Cold storage cache |

Only `/home/agent` is the persistent workspace; paths outside it do not survive a reset.

## Template-specific

The `:aidream` template adds extra paths under `/home/agent/aidream/` (e.g. `temp/`, `common/sample_data/`) via orchestrator env vars — not part of the core layout above.
