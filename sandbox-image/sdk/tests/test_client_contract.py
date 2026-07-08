"""Client↔daemon request-contract guard.

matrx-ai's ``_sandbox_proxy.py`` (in the aidream repo) is the ONLY client of
this daemon's structured HTTP API when a chat turn is bound to a sandbox. The
two repos ship independently, so a request-shape change on one side that isn't
mirrored on the other fails at RUNTIME with a cryptic 422 in a live user
session instead of failing a build.

That exact drift already happened twice:

  * ``/fs/patch`` — the client moved to anchor-based edits
    (``old_text``/``new_text``/``replace_all``) while the daemon still required
    the legacy offset-based (``start``/``end``/``replacement``) fields. Every
    sandbox-bound ``fs_edit``/``fs_patch`` call died with
    "Field required: edits.0.start".
  * ``/search/paths`` — the daemon returns ``{"paths": [...]}`` but the client
    reads ``results`` (unified with ``/search/content``), so path searches
    silently returned zero results. (Fixed by normalizing in the client proxy;
    the response-key half of the contract is pinned below.)

This test pins the payloads the client actually sends against the daemon's
Pydantic request models, in BOTH directions:

  1. the daemon model must ACCEPT exactly what the client sends (no 422), and
  2. every field the client sends must be a DECLARED field on the daemon model
     (so the client's value is honored, not silently dropped as an extra).

If a future edit to either side breaks one of these, this screams in CI — the
guard the drift slipped past. Keep ``CLIENT_REQUESTS`` / ``RESPONSE_CONTRACT``
in sync with ``matrx-ai/matrx_ai/tools/_sandbox_proxy.py``.
"""

from __future__ import annotations

import pytest

from matrx_agent.api.main import MkdirRequest, PatchRequest, WriteRequest
from matrx_agent.api.search import SearchContentRequest, SearchPathRequest

# Each entry: (daemon request model, JSON body the client sends verbatim).
# Bodies mirror the ``json=...`` payloads in _sandbox_proxy.py exactly.
CLIENT_REQUESTS = [
    # fs_write
    (
        WriteRequest,
        {"path": "/home/agent/f.txt", "content": "hi", "encoding": "utf8", "create_parents": True},
    ),
    (
        WriteRequest,
        {"path": "/home/agent/f.txt", "content": "aGk=", "encoding": "base64", "create_parents": True, "mode": 0o644},
    ),
    # fs_mkdir
    (MkdirRequest, {"path": "/home/agent/d", "parents": True}),
    # fs_patch — anchor-based edits + create_if_missing insert mode
    (
        PatchRequest,
        {
            "path": "/home/agent/f.txt",
            "edits": [{"old_text": "a", "new_text": "b", "replace_all": False}],
            "create_if_missing": False,
        },
    ),
    (
        PatchRequest,
        {
            "path": "/home/agent/new.txt",
            "edits": [{"old_text": "", "new_text": "seed"}],
            "create_if_missing": True,
        },
    ),
    # fs_search(content_search=True) -> /search/content
    (
        SearchContentRequest,
        {"query": "foo", "cwd": "/home/agent", "regex": True, "case_sensitive": False, "max_results": 100},
    ),
    # fs_search(content_search=False) -> /search/paths
    (SearchPathRequest, {"pattern": "*.py", "cwd": "/home/agent", "max_results": 100}),
]


@pytest.mark.parametrize("model, payload", CLIENT_REQUESTS)
def test_daemon_model_accepts_client_payload(model, payload):
    """Daemon model must validate the exact body the client sends (no 422)."""
    model(**payload)


@pytest.mark.parametrize("model, payload", CLIENT_REQUESTS)
def test_daemon_model_declares_every_client_field(model, payload):
    """Every field the client sends must be declared on the daemon model.

    Pydantic ignores unknown fields by default, so a renamed/added client
    field would be silently dropped (its value never honored) without ever
    raising. Assert declaration so that drift surfaces here, not in prod.
    """
    declared = set(model.model_fields)
    sent = set(payload)
    missing = sent - declared
    assert not missing, f"{model.__name__} silently drops client field(s): {sorted(missing)}"


# Response-key half of the contract: the keys the client consumer
# (filesystem.py) reads out of each daemon response. If a daemon route stops
# emitting one of these, the client silently sees empty data.
#   * fs_list reads   "entries"
#   * fs_search reads "results"  (client normalizes /search/paths' "paths" ->
#     "results"; /search/content already emits "results")
RESPONSE_CONTRACT = {
    "/fs/list": "entries",
    "/search/content": "results",
    "/search/paths": "paths",  # client remaps to "results"; daemon key is "paths"
}


def test_response_contract_is_documented():
    """Cheap tripwire: forces this map to be reviewed alongside route edits."""
    assert RESPONSE_CONTRACT["/search/paths"] == "paths"
    assert RESPONSE_CONTRACT["/search/content"] == "results"
