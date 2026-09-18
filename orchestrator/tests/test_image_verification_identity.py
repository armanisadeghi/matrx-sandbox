"""An image check that asserts what the AGENT can do must RUN as the agent.

THE OUTAGE THIS CLOSES, measured 2026-09-18. `build-aidream.sh`'s Codex CLI
verification wrapped two of its probes in `su -s /bin/sh -c … agent`, as though
the container were root. `Dockerfile.aidream` ends with `USER agent`, so it
never was: `su` answered

    Password: su: Authentication failure

and the build died there. EVERY hosted aidream image build failed that way from
07:56 UTC onward — zero promotions all day, the live `matrx-sandbox:aidream` tag
stuck on an image baked five days earlier — while the CLI and the stamp were
perfectly fine (`codex-cli 0.155.0` on the candidate image). Nothing about the
image was wrong; the check was.

TWO TRAPS A FUTURE FIX WALKS INTO, both measured on the real candidate image:

1. `--user root` looks like the obvious repair and is worse. Root bypasses file
   modes, so `test ! -w /opt/matrx-codex/bin/codex` reports the pinned binary as
   WRITABLE and the check fails for the opposite reason. Two of these probes only
   mean anything as a non-root user.
2. `! su … "touch …"` was a FALSE PASS: it succeeded whenever `su` itself failed,
   so an agent-writable codex prefix would have satisfied the guard. The negative
   probe has to be attempted by the agent directly, so only a real permission
   denial passes.

So the rule this pins: the verification runs as the image's own default user,
states which user that is, and uses no privilege-changing wrapper.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "sandbox-image" / "build-aidream.sh"
DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile.aidream"


def _codex_block() -> str:
    """The EXECUTABLE lines of the Codex verification, comments stripped.

    Stripped deliberately: the block's comments name `su` and `--user root` as
    the two things it must not do, and a guard that matched its own explanation
    would be red forever — which is its own kind of dishonest test.
    """
    text = BUILD_SCRIPT.read_text()
    start = text.index("verifying the pinned Codex CLI")
    end = text.index("verifying Claude Linux sandbox prerequisites", start)
    return "\n".join(
        line
        for line in text[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_image_default_user_is_agent() -> None:
    """The fact every probe below depends on, asserted rather than assumed."""
    directives = re.findall(r"^USER\s+(\S+)\s*$", DOCKERFILE.read_text(), re.MULTILINE)
    assert directives, "Dockerfile.aidream declares no USER at all"
    assert directives[-1] == "agent", (
        "the LAST USER directive decides what `docker run` runs as; the Codex "
        f"verification is written for `agent` and this image ends as {directives[-1]!r}"
    )


def test_the_codex_verification_uses_no_privilege_wrapper() -> None:
    """THE regression. `su` here is both broken and a false pass."""
    block = _codex_block()
    assert " su " not in block and "su -s" not in block, (
        "the Codex verification must not use `su`: the container already runs as "
        "agent, so `su agent` asks for a password and fails, and `! su …` passes "
        "whenever su itself fails rather than when the write is denied"
    )
    assert "sudo" not in block, "same reason as `su`"


def test_the_codex_verification_does_not_run_as_root() -> None:
    """`--user root` is the plausible wrong fix: root bypasses file modes."""
    block = _codex_block()
    assert "--user root" not in block, (
        "root bypasses file modes, so `test ! -w` would report the pinned codex "
        "binary as writable and the check would fail for the opposite reason"
    )
    assert "--user" not in block, (
        "the verification must run as the image's own default user, so that the "
        "identity it asserts about is the identity a real box actually has"
    )


def test_the_codex_verification_states_which_user_it_is() -> None:
    """So it can never silently run as the wrong user again."""
    block = _codex_block()
    assert 'test "$(id -un)" = agent' in block, (
        "the verification must assert its own identity; without that, a future "
        "USER change in the Dockerfile silently re-points every probe"
    )


def test_the_negative_probe_is_a_real_denied_write() -> None:
    block = _codex_block()
    assert "! touch /opt/matrx-codex/bin/.probe" in block, (
        "the 'agent cannot replace codex' probe must be a direct touch, so only a "
        "genuine permission denial satisfies it"
    )
    assert "test ! -w /opt/matrx-codex/bin/codex" in block


def test_the_stamp_and_the_binary_must_still_agree() -> None:
    # The original purpose of the block, unchanged by the identity fix.
    block = _codex_block()
    assert "/etc/matrx-codex-version" in block
    assert "codex --version" in block
    assert 'test "$stamped" = "$reported"' in block
