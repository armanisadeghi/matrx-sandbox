"""matrx_agent.persistence — in-container session manifest, checkpoint, auto-stash.

The orchestrator (in /srv/projects/matrx-sandbox/orchestrator/) gives a sandbox
its persistent storage layer (S3 prefix on EC2, Docker volume on hosted). This
module lives *inside* the sandbox container and:

  - Writes ``/home/agent/.matrx/session.json`` every 5 minutes (and on shutdown)
    capturing what the user is doing — repos, dirty git state, processes, cwd.
  - On shutdown, runs ``git_autostash`` to preserve uncommitted work as both a
    local stash and (when creds allow) a pushed ``matrx/auto-stash/<ts>`` branch.
  - On startup, reads the prior session.json (if present) and renders
    ``/home/agent/.matrx/session-report.md`` so users always know what was
    preserved and what was lost.

Designed to fail loudly but never block container shutdown — every write is
wrapped in try/except + a tight timeout, with the failure recorded in the
manifest's ``transient_things_we_could_not_save`` field.
"""

from matrx_agent.persistence.manifest import SessionManifest, write_manifest, read_prior_manifest
from matrx_agent.persistence.checkpoint import CheckpointDaemon
from matrx_agent.persistence.session_report import render_report

__all__ = [
    "SessionManifest",
    "write_manifest",
    "read_prior_manifest",
    "CheckpointDaemon",
    "render_report",
]
