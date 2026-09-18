# Adding Utilities to Sandbox Containers

This guide shows you exactly where to add different types of utilities that will be available in every sandbox container.

## Managed Claude runtime prerequisites

The existing `matrx-sandbox:aidream` image owns the two official Linux
prerequisites for Claude Code's Bash sandbox: `bubblewrap` (`bwrap`) and
`socat`. `Dockerfile.aidream` fails its build if either executable is missing,
and `build-aidream.sh` verifies both again by starting the finished image. The
aidream managed-runtime capability probe also performs a live `bwrap` mount-
namespace smoke test and refuses execution if isolation is unavailable. Do not
move these tools into another image or service: managed Claude runs inside the
existing aidream variant.

Release builds also pin the exact full aidream commit in the image label and
root-owned `/etc/aidream-image-sha`. The staged checkout keeps that real commit
as `HEAD` (never a synthetic build commit). Managed autostart calls
`mtx aidream serve --require-image-source`, which fails closed unless the
immutable `/opt/aidream-template` checkout is clean and exactly matches the
baked SHA. Managed autostart always serves that certified template. The durable
`/home/agent/aidream` checkout remains the user's editable worktree and is never
reset or deleted to start the API; it can be older, newer, or dirty without
changing the managed server revision. `mtx aidream` commands still target the
editable checkout by default. Operator diagnostics run `aidream_source_exact`
against the immutable template, matching the process they health-check on
port 8001 rather than producing false drift from the user's worktree. The
hosted aidream container uses a Docker-enforced read-only root filesystem and
drops `SYS_ADMIN` plus `/dev/fuse`; sudo inside the sandbox cannot remount,
bind-shadow, or write the template/venv. EC2 does not autostart this managed
API. Managed autostart
first proves the mount is read-only, then executes the root-owned helper with
an inert ephemeral home, fixed source/stamp paths, `/dev/null` global Git
config, and a fixed privileged-mode Bash that ignores exported functions and
profiles. Injection-sensitive shell, Python,
Git, loader, venv, and uv variables are removed before dispatch, and managed
serve uses the template venv's Python in isolated mode. Exact verification
rejects untracked tampering. Only the user home and explicit runtime tmpfs/log
paths remain writable.

## The toolchain contract (every variant owes an agent the same floor)

Five defects found by an agent working in a real EC2 `development` box
(2026-09-18) were one defect: nothing held the image variants to a single
floor. They are now **one contract**, identical in `Dockerfile`,
`Dockerfile.slim` and — by inheritance plus their own build-time verification —
`Dockerfile.development`, `Dockerfile.aidream` and `sandbox-local/Dockerfile`.

| The contract | Why | Defect it closes |
|---|---|---|
| **Node ≥ 22 LTS**, corepack enabled, pinned `pnpm` first on PATH | Stagehand v4 and its generation need Node 22 (`ReferenceError: WebSocket is not defined` on 20) | P1-1 |
| **`npm i -g` works as the agent**, prefix `/opt/npm-global` (agent-owned, on PATH) | the prefix was `/usr`, whose `lib/node_modules` is root-owned → `EACCES` | P1-2 |
| **Install scripts run** — `dangerously-allow-all-scripts=true` in `/opt/npm-global/etc/npmrc` | npm 11 ships an `allow-scripts` allowlist; without a policy a dependency's postinstall is SKIPPED with only a warning | P1-3 |
| **`python3` is a FINAL release ≥ 3.12** (deadsnakes 3.12 on jammy; `python3.11` is GONE) | Ubuntu 22.04's `python3.11` package is **3.11.0rc1**, a 2022 release candidate | P2-1 |
| **`browse` is preinstalled** | agents typed `browse` and got `command not found` | P2-2 |
| **Platform-launched binaries live OUTSIDE `/opt/npm-global`** — root-owned prefix, resolved on `/usr/local/bin:/usr/bin:/bin` | the agent-writable prefix means agent-replaceable; a privileged launch carrying a brokered credential must not exec a binary the model can swap | XT-09 |

**Why `/opt/npm-global` and not `~/.npm-global`.** The home is restored
wholesale at boot — from a per-user Docker volume (hosted) or S3 (ec2 templates
that enable it). A global prefix inside the home is therefore either wiped by a
restore or resurrected *stale* across an image swap, and it would also put
agent-installed binaries under the home-ownership chokepoint. `/opt/npm-global`
is built with the image, replaced with the image, and untouched by every home
restore. It is published twice — as `ENV` for the container, and in
`/etc/profile.d/matrx-npm-global.sh` for shells `sshd` starts, which inherit
none of the container env.

**🚨 A BINARY THE PLATFORM LAUNCHES NEVER GOES IN `/opt/npm-global`.** The
prefix is agent-writable BY DESIGN, so everything in it is agent-replaceable by
design too. That is correct for the agent's own tools and wrong for anything
the platform execs on the agent's behalf with a credential attached: the hosted
Codex runtime launches `codex` carrying a brokered OpenAI capability, so a
`codex` the model could overwrite would be a credential-exfiltration path
rather than an inconvenience. Such a tool gets its OWN root-owned prefix
(`Dockerfile.aidream` installs the pinned Codex CLI into `/opt/matrx-codex`,
symlinked at `/usr/local/bin/codex`), the launching code resolves it against
`/usr/local/bin:/usr/bin:/bin` and nothing else, and the build proves BOTH
halves on the finished image — the agent can run it, the agent cannot replace
it. Add a platform-launched binary the same way, with the same two build
assertions.

**THE INSTALL-SCRIPT POLICY, stated.** Inside a Matrx sandbox the agent's own
installs run their lifecycle scripts. The isolation boundary here is the
**container**, not npm's allowlist: the box is single-tenant, disposable,
already grants passwordless sudo, and carries no platform credentials (see the
platform-env isolation rule in `CLAUDE.md`). An allowlist that a
non-interactive agent cannot answer converts a loud install failure into a
silently half-installed package — half the native JS ecosystem (esbuild, sharp,
puppeteer, playwright) depends on postinstall. If a future template ever does
hold credentials, it flips this file's npmrc, not the agent's workflow.

**`browse`** is the platform browser CLI: a thin front end on the **same**
canonical Browser Manager path `matrx_tools.tools.browser` uses
(`sdk/matrx_agent/cli/browse.py`, also reachable as `mtx browse`). It is never a
second browser — the sandbox still launches no Chromium and holds no profile.

```bash
browse open https://example.com --text
browse click "More information"
browse shot page.png
```

**The guard.** `scripts/test-toolchain.sh` proves the whole contract on a real
box — it runs the real binaries, really installs a global package as the agent,
really runs a dependency postinstall, and asserts `sys.version_info.releaselevel`
rather than matching a string (that is the field that catches `3.11.0rc1`):

```bash
docker run --rm -u agent matrx-sandbox:core bash /opt/sandbox/scripts/test-toolchain.sh
```

It is **red on any image built before 2026-09-18** (7 failures) and green on the
current one. The paper half — the variants may not drift from each other or
from `sdk/matrx_agent/cli/toolchain.py`'s constants — is
`sdk/tests/test_cli_toolchain.py`.

**Existing boxes are never force-migrated** (SBX-006), so a box created before
this build keeps its old toolchain until it is recreated. `mtx toolchain ensure`
closes four of the five on a box as it stands — it installs Node 22 from the
official tarball (no root needed), fixes the npm prefix and policy, and creates
the `mtx`/`browse` shims. It **cannot** change `python3`: the SDK is installed
into that interpreter. `mtx toolchain check` says so in plain words with the
remedy (recreate the box) rather than passing quietly.

---

## The agent project toolchain (already there — don't re-add it)

Every image — `:core` (which backs the `bare`, `node-22` and `python-3.13`
templates), `:slim`, and `:aidream`/`:development` by inheritance — ships
**`uv`**, **`pnpm`**, **`gh`** and **`node`**, pinned by the `UV_VERSION` /
`PNPM_VERSION` / `NODE_MAJOR` build ARGs in `Dockerfile` and `Dockerfile.slim`.
The build fails loudly if any of them is missing. The version floors they must
clear are **The toolchain contract** above.

Agents start projects with the SDK CLI, not by improvising flags:

```bash
mtx new python demo && cd ~/projects/demo && uv run pytest
mtx new node webdemo && cd ~/projects/webdemo && pnpm install && pnpm test
```

`mtx new` lays down a **flat** project (module and test at the project root, no
`src/`, no build backend) with one passing test and prints the next commands.
Source: `sdk/matrx_agent/cli/new.py`.

The forcing-function check is `scripts/test-toolchain.sh` — it runs the real
binaries and really executes the scaffolded test suite:

```bash
docker run --rm -u agent matrx-sandbox:slim bash /opt/sandbox/scripts/test-toolchain.sh
```

Run it after any change to the toolchain block or to `mtx new`. The unit half
(layout, refusal-with-remedy, dispatcher wiring) is `sdk/tests/test_cli_new.py`.

---

## Quick Reference

| Type | Location | Available in Container At |
|------|----------|---------------------------|
| **Bash scripts** | `scripts/my-tool.sh` | `/opt/sandbox/scripts/my-tool.sh` |
| **Python modules** | `sdk/matrx_agent/my_module.py` | `import matrx_agent.my_module` |
| **Config files** | `config/my-config.conf` | `/opt/sandbox/config/my-config.conf` |
| **System tools** | `Dockerfile` (apt-get install) | Available in `$PATH` |
| **Python packages** | `sdk/pyproject.toml` (dependencies) | `import package_name` |

---

## Adding Shell Scripts

**When to use:** Command-line tools, system utilities, wrapper scripts

### 1. Create the script

```bash
sandbox-image/scripts/my-utility.sh
```

Example:
```bash
#!/usr/bin/env bash
# Description: Example utility script
set -euo pipefail

echo "Running my custom utility"
# Your logic here
```

### 2. Make it executable (optional)

```bash
chmod +x sandbox-image/scripts/my-utility.sh
```

The Dockerfile already runs `chmod +x /opt/sandbox/scripts/*.sh`, so this is optional.

### 3. Rebuild the image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 4. Use in containers

```bash
# From bash inside container
/opt/sandbox/scripts/my-utility.sh

# Or add to PATH in Dockerfile
ENV PATH="/opt/sandbox/scripts:$PATH"
# Then just:
my-utility.sh
```

---

## Adding Python Functions

**When to use:** Reusable Python code, API clients, data processing utilities

### 1. Create a new module

```bash
sandbox-image/sdk/matrx_agent/my_module.py
```

Example:
```python
"""My custom utilities for agents."""

def process_data(data: str) -> str:
    """Process some data."""
    return data.upper()

class MyHelper:
    """Helper class for agents."""
    
    def __init__(self, config: dict):
        self.config = config
    
    def do_something(self) -> None:
        print(f"Doing something with {self.config}")
```

### 2. Add dependencies if needed

Edit `sandbox-image/sdk/pyproject.toml`:

```toml
[project]
dependencies = [
    "httpx>=0.25",
    "pydantic>=2.0",
    "your-new-package>=1.0",  # ← Add here
]
```

### 3. Rebuild the image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 4. Use in containers

```python
# From Python inside container
from matrx_agent.my_module import process_data, MyHelper

result = process_data("hello")
helper = MyHelper({"key": "value"})
helper.do_something()
```

---

## Adding Configuration Files

**When to use:** Config templates, default settings, reference files

### 1. Create the config file

```bash
sandbox-image/config/my-config.yaml
```

Example:
```yaml
# My custom configuration
settings:
  timeout: 30
  retries: 3
  endpoints:
    - https://api.example.com
```

### 2. Rebuild the image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 3. Use in containers

```bash
# From bash
cat /opt/sandbox/config/my-config.yaml

# From Python
import yaml
with open("/opt/sandbox/config/my-config.yaml") as f:
    config = yaml.safe_load(f)
```

---

## Adding System Packages

**When to use:** CLI tools, libraries, system utilities (jq, ffmpeg, etc.)

### 1. Edit the Dockerfile

Find the appropriate `apt-get install` section in `sandbox-image/Dockerfile`:

```dockerfile
# ─── System packages ─────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    bash \
    curl \
    wget \
    git \
    jq \
    # Add your packages here ↓
    ffmpeg \
    imagemagick \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*
```

**Important:**
- Add packages to the **appropriate section** (system tools, network tools, etc.)
- Keep the `&& rm -rf /var/lib/apt/lists/*` at the end
- Use `--no-install-recommends` to minimize image size

### 2. Rebuild the image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 3. Use in containers

```bash
# Tool is available in PATH
ffmpeg -version
jq --version
```

---

## Adding Python Packages (Pre-installed)

**When to use:** Python libraries that every agent will need

### 1. Edit the Dockerfile

Find the Python packages section in `sandbox-image/Dockerfile`:

```dockerfile
# ─── Common Python packages for agent use ─────────────────────────────────────
RUN python3 -m pip install --no-cache-dir \
    httpx \
    requests \
    aiohttp \
    pydantic \
    rich \
    click \
    # Add your packages here ↓
    scikit-learn \
    pillow \
    && echo "Done"
```

### 2. Rebuild the image

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 3. Use in containers

```python
import sklearn
from PIL import Image
```

---

## Example: Adding a PDF Processing Utility

Let's add a complete PDF processing utility as an example.

### 1. Add Python SDK function

**File:** `sandbox-image/sdk/matrx_agent/pdf_utils.py`

```python
"""PDF processing utilities for agents."""
import subprocess
from pathlib import Path


def pdf_to_text(pdf_path: str) -> str:
    """Extract text from PDF using pdftotext."""
    result = subprocess.run(
        ["pdftotext", pdf_path, "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def pdf_to_images(pdf_path: str, output_dir: str) -> list[Path]:
    """Convert PDF pages to images."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    subprocess.run(
        ["pdftoppm", pdf_path, str(output_path / "page"), "-png"],
        check=True,
    )
    
    return sorted(output_path.glob("page-*.png"))
```

### 2. Add system dependencies

**Edit:** `sandbox-image/Dockerfile`

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ... existing packages ...
    poppler-utils \  # Provides pdftotext and pdftoppm
    && rm -rf /var/lib/apt/lists/*
```

### 3. Add convenience script

**File:** `sandbox-image/scripts/extract-pdf.sh`

```bash
#!/usr/bin/env bash
# Extract text from PDF file
set -euo pipefail

if [ $# -eq 0 ]; then
    echo "Usage: extract-pdf.sh <pdf-file>"
    exit 1
fi

pdftotext "$1" -
```

### 4. Rebuild

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```

### 5. Use in containers

```python
# From Python
from matrx_agent.pdf_utils import pdf_to_text, pdf_to_images

text = pdf_to_text("document.pdf")
images = pdf_to_images("document.pdf", "/tmp/pages")
```

```bash
# From bash
extract-pdf.sh document.pdf
```

---

## Testing New Utilities

### Build and run interactively

```bash
# Build the image
docker build -t matrx-sandbox:latest sandbox-image/

# Run a test container
docker run -it --rm matrx-sandbox:latest /bin/bash

# Test your utilities
$ /opt/sandbox/scripts/my-utility.sh
$ python3 -c "from matrx_agent.my_module import process_data; print(process_data('test'))"
```

### Run automated tests

```bash
# If you have tests in sdk/tests/
docker run --rm matrx-sandbox:latest pytest /opt/sandbox/sdk/tests/
```

---

## Best Practices

### ✅ DO

- **Keep scripts focused** — One utility per script
- **Add error handling** — Use `set -euo pipefail` in bash, try/except in Python
- **Document parameters** — Add docstrings and help text
- **Test thoroughly** — Verify utilities work in clean container
- **Pin versions** — Specify exact versions for apt packages and pip dependencies
- **Minimize size** — Only add what's needed

### ❌ DON'T

- **Don't hardcode paths** — Use environment variables (`$HOT_PATH`, `$COLD_PATH`)
- **Don't assume root** — Scripts run as `agent` user (UID 1000)
- **Don't add secrets** — Secrets should be passed at runtime, not baked in
- **Don't install dev tools** — Keep the image lean (no compilers unless necessary)
- **Don't forget cleanup** — Always `rm -rf /var/lib/apt/lists/*` after apt-get

---

## Debugging

### View installed packages

```bash
# Python packages
docker run --rm matrx-sandbox:latest pip list

# System packages
docker run --rm matrx-sandbox:latest dpkg -l

# Check file exists
docker run --rm matrx-sandbox:latest ls -la /opt/sandbox/scripts/
```

### Inspect image layers

```bash
docker history matrx-sandbox:latest
```

### Check image size

```bash
docker images matrx-sandbox:latest
```

---

## Summary

| What You Want | Where to Add It |
|---------------|-----------------|
| Bash script | `scripts/your-script.sh` |
| Python function | `sdk/matrx_agent/your_module.py` |
| Config file | `config/your-config.yaml` |
| System tool | Dockerfile (`apt-get install`) |
| Python package | Dockerfile (`pip install`) or `sdk/pyproject.toml` |

After making changes, always rebuild:

```bash
docker build -t matrx-sandbox:latest sandbox-image/
```
