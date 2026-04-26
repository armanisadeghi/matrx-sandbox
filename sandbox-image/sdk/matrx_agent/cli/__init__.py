"""matrx_agent.cli — the ``mtx`` command-line tool inside every sandbox.

End users invoke ``mtx files ls``, ``mtx files cat``, etc. from a shell to
work with their AI Dream cloud_files surface. AI agents can do the same
with no special API knowledge.

Subcommands:
  mtx files ls                  → list files under the user's cld_files namespace
  mtx files cat <path>          → print one file's content
  mtx files put <local> <path>  → upload a local file
  mtx files rm <path>           → delete a file
  mtx files sync down --dest X  → bulk pull into a local dir (used at startup)
  mtx files sync up   --src X   → bulk push from a local dir (used at shutdown)
  mtx whoami                    → identity + AI Dream config in this sandbox

Authentication: orchestrator-injected env vars
  MATRX_AIDREAM_URL              — base URL of the AI Dream backend
  MATRX_AIDREAM_SERVICE_TOKEN    — service token (sandbox-internal, never user-visible)
  USER_ID                        — UUID of the user this sandbox belongs to

The CLI authenticates as a service to AI Dream and identifies the user via
``X-Matrx-User-Id``. AI Dream verifies the service token matches its expected
value and trusts the header.
"""
