# Adding Utilities to Sandbox Containers

This guide shows you exactly where to add different types of utilities that will be available in every sandbox container.

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
