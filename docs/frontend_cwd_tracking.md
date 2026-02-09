# Server-Side CWD Tracking Implementation

## Overview

We have implemented server-side tracking of the Current Working Directory (CWD) for sandbox execution. This resolves the issue where `cd` commands were not persisting across stateless API calls.

## How it Works

The Orchestrator now maintains an in-memory map of `sandbox_id -> cwd`.
When a command is executed via `POST /sandboxes/{id}/exec`:

1.  The command is wrapped to run inside the last known CWD (defaulting to `/home/agent`).
2.  After execution, the new CWD is captured from the shell and updated in memory.
3.  The response includes the new `cwd`.

## Frontend Integration Guide

### 1. Automatic Behavior

- **No changes required for basic functionality.** Commands like `cd folder`, `ls` will now work as expected because the server remembers the directory state.

### 2. Updating the Terminal Prompt (Recommended)

You should update the terminal prompt to reflect the current directory returned by the API.

- **Read `response.cwd`**: The `ExecResponse` contains the full path after command execution.
  - Example: `/home/agent/projects/my-app`
- **Update UI**:
  - `agent@sandbox:~/projects/my-app$`

### 3. Optional Override

You can force a command to run in a specific directory by sending `cwd` in the request:

```json
POST /sandboxes/{id}/exec
{
  "command": "ls -la",
  "cwd": "/tmp"  // Optional override
}
```

## API Changes

### `ExecRequest`

| Field     | Type                | Description                                      |
| --------- | ------------------- | ------------------------------------------------ |
| `command` | `string`            | The shell command to run.                        |
| `cwd`     | `string` (Optional) | Override the working directory for this command. |

### `ExecResponse`

| Field       | Type     | Description                                         |
| ----------- | -------- | --------------------------------------------------- |
| `exit_code` | `int`    | Process exit code (0 = success).                    |
| `stdout`    | `string` | Standard output.                                    |
| `stderr`    | `string` | Standard error.                                     |
| `cwd`       | `string` | The working directory _after_ the command finished. |
