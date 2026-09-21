"""Guards for the sandbox↔browser join.

These are CONTRACT guards, not a re-enactment of the join. The join itself is
proved against a real created box (the env names are visible, values redacted,
through ``GET /sandboxes/{id}/agent-env``) — a test that stands up a fake
orchestrator and a fake AI Dream and then asserts the fake did what it was told
proves only that the test is self-consistent.

What these DO prove, each of which was a live way for the join to break:

1. The two names are on ``ORCHESTRATOR_MANAGED_ENV``. Without that, the
   binding-time leak sweep would CLEAR them out of every live box the first
   time it ran, and the box would lose its browser between bindings.
2. The leak sweep actually leaves them alone, for a passthrough template too.
3. A lookup that could not answer never invents a profile id, and never half-
   injects (an id without a target, or a target without an id, is exactly the
   state the in-box client fails closed on with a confusing message).
"""

from __future__ import annotations

from orchestrator.browser_profile import (
    BROWSER_ENV_NAMES,
    EXECUTION_TARGET_ENV,
    PROFILE_ID_ENV,
    BrowserProfileLookup,
)
from orchestrator.sandbox_manager import ORCHESTRATOR_MANAGED_ENV
from orchestrator.vault_env_refresh import leaked_platform_names


def test_the_browser_names_are_orchestrator_managed() -> None:
    """Missing here, the binding sweep would strip the join from live boxes."""
    missing = [n for n in BROWSER_ENV_NAMES if n not in ORCHESTRATOR_MANAGED_ENV]
    assert not missing, (
        f"{missing} are injected by the orchestrator but absent from "
        "ORCHESTRATOR_MANAGED_ENV. Live, the binding-time leak sweep would "
        "unset them in every box and every sandbox would lose its browser."
    )


def test_the_leak_sweep_never_clears_the_browser_join() -> None:
    present = [PROFILE_ID_ENV, EXECUTION_TARGET_ENV, "ANTHROPIC_KEY"]
    for template in (None, "slim", "aidream"):
        cleared = leaked_platform_names(present, template=template, vault_names=set())
        assert PROFILE_ID_ENV not in cleared, template
        assert EXECUTION_TARGET_ENV not in cleared, template


def test_an_unanswered_lookup_injects_nothing() -> None:
    """No browser is a SENTENCE, never a guessed id and never half a pair."""
    lookup = BrowserProfileLookup(reason="you have no cloud browser here yet")
    assert lookup.present is False
    assert lookup.env() == {}
    assert lookup.diagnostic()["reason"]


def test_a_half_answer_is_no_answer() -> None:
    """An id with no target (or the reverse) would reach the in-box client as
    'missing MATRX_BROWSER_EXECUTION_TARGET' — a variable name where a person
    needs a sentence. Neither half is injected alone."""
    assert BrowserProfileLookup(profile_id="p-1").env() == {}
    assert BrowserProfileLookup(execution_target="browser_fleet").env() == {}


def test_a_real_answer_injects_exactly_the_two_names() -> None:
    lookup = BrowserProfileLookup(
        profile_id="11111111-1111-1111-1111-111111111111",
        label="Work browser",
        execution_target="browser_fleet",
    )
    assert lookup.env() == {
        PROFILE_ID_ENV: "11111111-1111-1111-1111-111111111111",
        EXECUTION_TARGET_ENV: "browser_fleet",
    }
    assert set(lookup.env()) == set(BROWSER_ENV_NAMES)
