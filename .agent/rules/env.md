---
description: Python environment configuration for the orchestrator project
---

# Python Environment

ALWAYS use the Python interpreter located at: `./orchestrator/.venv/bin/python`

The orchestrator project uses `uv` for dependency management. The virtual environment is at `orchestrator/.venv/`.

When running Python commands, always activate or reference this venv:
- Interpreter: `orchestrator/.venv/bin/python3`
- Package manager: `uv` (run from `orchestrator/` directory)
- Install deps: `cd orchestrator && uv sync`
