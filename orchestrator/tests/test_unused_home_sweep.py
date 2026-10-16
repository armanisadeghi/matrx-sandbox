"""A hosted home nobody ever used is deleted after the period — and ONLY that.

Owner ruling (Arman, 2026-10-16): sandboxes that are essentially empty are
deleted after some time. The one way this goes catastrophically wrong is a home
with somebody's work in it being deleted, so most of these guards are that:

  * every kind of user content keeps a home (the classifier is exercised by
    running the EXACT program the helper container runs, against a real
    directory, not a re-implementation of it);
  * an empty home inside the period is kept, and never even inspected;
  * an always-on home, and a home with any box up, is kept;
  * an unreadable knob turns the sweep off;
  * an empty, untouched home past the period IS deleted, through
    ``delete_user_volume``'s fence, its rows soft-deleted with the reason on
    them, one WARNING line naming sandbox, user and why;
  * deletions are bounded per pass.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator import empty_home
from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.storage_layout import user_volume_name
from orchestrator.store import InMemorySandboxStore

from tests.conftest import seed_sandbox_knobs

ORG = "33333333-3333-4333-8333-333333333333"
USERS = [f"{i}{i}{i}{i}{i}{i}{i}{i}-1111-4111-8111-111111111111" for i in range(1, 6)]
KNOBS = {
    empty_home.AFTER_DAYS_KNOB: 14,
    empty_home.MAX_PER_PASS_KNOB: 5,
    empty_home.MAX_BYTES_KNOB: 1_000_000,
}


# ── The home skeleton the image writes, and the content that makes it "used" ──

def _skeleton(home: Path) -> Path:
    """What ensure-layout.sh, prepare-agent-home.sh, the entrypoint and a
    cloud-files down-sync leave in a home nobody ever touched."""
    for d in ("cloud-files", "repos", "projects", "scratch", ".matrx/plans",
              ".matrx/skills", ".matrx/memory", ".matrx/runtime/tool-calls/_runtime",
              ".matrx/runtime/shell-logs", ".matrx/runtime/session-reports",
              ".matrx/instructions", ".ssh"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".matrx/instructions/SANDBOX_LAYOUT.md").write_text("# layout\n" * 200)
    (home / ".ssh/authorized_keys").write_text("ssh-ed25519 AAAA admin\n")
    (home / ".bashrc").write_text("[ -f ~/.sandbox_env ] && source ~/.sandbox_env\n")
    (home / ".sandbox_env").write_text("export X=1\n")
    (home / ".matrx/runtime/session-reports/boot.md").write_text("booted\n")
    (home / ".matrx/runtime/tool-calls/_runtime/1-cloud-sync.md").write_text("synced\n")
    # The person's AI Matrx files, mirrored down at boot: big, and NOT use.
    (home / "cloud-files/report.pdf").write_bytes(b"x" * 5_000_000)
    return home


CONTENT = {
    "project_file": lambda h: (h / "projects/app").mkdir() or (h / "projects/app/main.py").write_text("print(1)"),
    "cloned_repo": lambda h: (h / "repos/acme").mkdir() or (h / "repos/acme/README").write_text("r"),
    "scratch_file": lambda h: (h / "scratch/notes.txt").write_text("hi"),
    "agent_tool_call": lambda h: (h / ".matrx/runtime/tool-calls/1-shell-ab.md").write_text("ls"),
    "agent_plan": lambda h: (h / ".matrx/plans/plan.md").write_text("plan"),
    "agent_memory": lambda h: (h / ".matrx/memory/fact.md").write_text("fact"),
    "agent_skill": lambda h: (h / ".matrx/skills/s.md").write_text("s"),
    "shell_log": lambda h: (h / ".matrx/runtime/shell-logs/x.log").write_text("x"),
    "terminal_history": lambda h: (h / ".bash_history").write_text("ls\n"),
    "file_at_home_root": lambda h: (h / "todo.txt").write_text("x"),
    "file_in_new_top_dir": lambda h: (h / "work").mkdir() or (h / "work/a.txt").write_text("x"),
    "large_hidden_install": lambda h: (h / ".local").mkdir() or (h / ".local/blob").write_bytes(b"x" * 2_000_000),
}


def _run_helper_program(home: Path, max_bytes: int) -> dict:
    """Run the EXACT program the helper container runs, against ``home``."""
    out = subprocess.run(
        [sys.executable, "-c", empty_home.helper_program(), str(home), str(max_bytes)],
        check=True, capture_output=True, text=True,
    ).stdout
    line = [ln for ln in out.splitlines() if ln.startswith("EMPTY_HOME_RESULT=")]
    assert len(line) == 1, out
    return json.loads(line[0].split("=", 1)[1])


def test_a_skeleton_home_is_empty(tmp_path) -> None:
    verdict = _run_helper_program(_skeleton(tmp_path / "home"), 1_000_000)
    assert verdict["empty"] is True, verdict


def test_empty_directories_never_count(tmp_path) -> None:
    """Measured live 2026-10-16: an untouched aidream-template home carries
    empty ``workspace/`` and ``temp/`` at the top level."""
    home = _skeleton(tmp_path / "home")
    for d in ("workspace", "temp", "projects/new-thing", "repos/acme", ".cache/uv"):
        (home / d).mkdir(parents=True)
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is True, verdict


# ── A template-seeded clone (aidream/) is judged by git, not by its files ──────

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                    "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True)


def _template_clone(home: Path, tmp_path: Path) -> Path:
    """Seed ``home/aidream`` the way build-aidream.sh + entrypoint-aidream.sh
    do: git init, remote origin, shallow fetch of the release commit, reset to
    FETCH_HEAD, branch main — then ``cp -a`` into the home, with a big ignored
    .venv beside the tracked files."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (upstream / "app.py").write_text("print('aidream')\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "release")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream, check=True,
                         capture_output=True, text=True).stdout.strip()
    clone = home / "aidream"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", "https://github.com/example/aidream.git")
    _git(clone, "fetch", "-q", "--depth=1", f"file://{upstream}", sha)
    _git(clone, "reset", "-q", "--mixed", "FETCH_HEAD")
    _git(clone, "checkout", "-q", "--", ".")  # the git-archive'd tracked files
    _git(clone, "branch", "-M", "main")
    (clone / ".venv/lib").mkdir(parents=True)
    (clone / ".venv/lib/wheels.bin").write_bytes(b"x" * 3_000_000)  # > max_bytes
    return clone


CLONE_WORK = {
    "untracked_file": lambda c: (c / "notes.md").write_text("mine"),
    "dirty_edit": lambda c: (c / "app.py").write_text("print('edited')\n"),
    "unpushed_commit": lambda c: ((c / "app.py").write_text("print('v2')\n"),
                                  _git(c, "commit", "-q", "-am", "mine")),
    "stash": lambda c: ((c / "app.py").write_text("print('wip')\n"),
                        _git(c, "stash", "-q")),
}


def test_a_pristine_template_clone_is_empty(tmp_path) -> None:
    home = _skeleton(tmp_path / "home")
    _template_clone(home, tmp_path)
    for d in ("workspace", "temp"):
        (home / d).mkdir()
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is True, verdict
    assert verdict["bytes"] < 1_000_000  # the clone is never weighed


@pytest.mark.parametrize("kind", sorted(CLONE_WORK))
def test_work_in_a_template_clone_makes_a_home_used(tmp_path, kind) -> None:
    home = _skeleton(tmp_path / "home")
    CLONE_WORK[kind](_template_clone(home, tmp_path))
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is False, (kind, verdict)
    assert any(e.startswith("aidream: ") for e in verdict["evidence"]), verdict


def test_a_clone_git_cannot_read_counts_as_used(tmp_path) -> None:
    home = _skeleton(tmp_path / "home")
    clone = _template_clone(home, tmp_path)
    (clone / ".git/HEAD").write_text("garbage\n")
    assert _run_helper_program(home, 1_000_000)["empty"] is False


def test_every_template_seeded_home_dir_is_judged_as_a_clone() -> None:
    """A template that seeds a new top-level clone into the home must be named
    in TEMPLATE_SEEDED_CLONES, or every home of that template reads as used
    (and is never weighed correctly). Reads each entrypoint's seed target."""
    import re
    scripts = Path(__file__).resolve().parents[2] / "sandbox-image" / "scripts"
    seeded = set()
    for path in scripts.glob("entrypoint*.sh"):
        for m in re.finditer(r'WORK_DIR="/home/agent/([A-Za-z0-9_.-]+)"', path.read_text()):
            seeded.add(m.group(1))
    assert "aidream" in seeded, "the census found nothing; its pattern is stale"
    assert seeded <= set(empty_home.TEMPLATE_SEEDED_CLONES), seeded


@pytest.mark.parametrize("kind", sorted(CONTENT))
def test_every_kind_of_user_content_makes_a_home_used(tmp_path, kind) -> None:
    home = _skeleton(tmp_path / "home")
    CONTENT[kind](home)
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is False, (kind, verdict)


# ── The sweep, end to end on the in-memory store and a fake Docker ─────────────

class _FakeDocker:
    """Volumes are directories; ``containers.run`` runs the helper program
    against the directory bound at /home/agent, exactly as the helper would."""

    def __init__(self, homes: dict[str, Path]):
        self.homes = homes
        self.removed: list[str] = []
        self.inspected: list[str] = []
        self.attached: set[str] = set()
        docker = self

        class _Volumes:
            def get(self, name):
                from docker.errors import NotFound
                if name not in docker.homes or name in docker.removed:
                    raise NotFound(name)
                return SimpleNamespace(remove=lambda force=False: docker.removed.append(name))

        class _Containers:
            def list(self, all=False, filters=None):  # noqa: A002
                return [object()] if (filters or {}).get("volume") in docker.attached else []

            def run(self, image, command, entrypoint, volumes, **kwargs):
                assert entrypoint == ["python3"]
                assert kwargs["network_disabled"] is True
                (volume, bind), = volumes.items()
                assert bind == {"bind": "/home/agent", "mode": "ro"}
                docker.inspected.append(volume)
                args = [str(docker.homes[volume]) if a == "/home/agent" else a for a in command]
                return subprocess.run([sys.executable, *args], check=True,
                                      capture_output=True).stdout

        self.volumes = _Volumes()
        self.containers = _Containers()


def _row(sandbox_id, user, *, age_days, status=SandboxStatus.EXPIRED, labels=None):
    now = datetime.now(timezone.utc)
    return SandboxResponse(
        sandbox_id=sandbox_id, user_id=user, organization_id=ORG, status=status,
        created_at=now - timedelta(days=age_days),
        stopped_at=now - timedelta(days=age_days) + timedelta(hours=2),
        last_heartbeat_at=now - timedelta(days=age_days) + timedelta(hours=1),
        tier="hosted", labels=labels,
        persistence_volume=user_volume_name(user, ORG),
    )


@pytest.fixture
def wired(monkeypatch, tmp_path):
    (tmp_path / "journal").mkdir(mode=0o700)
    journal = HostedMigrationJournal(tmp_path / "journal")
    for target in ("orchestrator.hosted_operation_lease.HostedMigrationJournal",
                   "orchestrator.hosted_migration.HostedMigrationJournal"):
        monkeypatch.setattr(target, lambda: journal)
    for target in ("orchestrator.config.settings.host_tier",
                   "orchestrator.hosted_operation_lease.settings.host_tier",
                   "orchestrator.home_identity.settings.host_tier"):
        monkeypatch.setattr(target, "hosted")
    store = InMemorySandboxStore()
    homes: dict[str, Path] = {}
    docker = _FakeDocker(homes)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: docker)

    async def _image(_client):
        return "sha256:" + "c" * 64

    monkeypatch.setattr("orchestrator.hosted_runtime._helper_image", _image)
    monkeypatch.setattr(empty_home, "_known_used", set())
    seed_sandbox_knobs(KNOBS)

    async def add(sandbox_id, user, *, age_days, content=None, **kw):
        await store.save(_row(sandbox_id, user, age_days=age_days, **kw))
        volume = user_volume_name(user, ORG)
        homes[volume] = _skeleton(tmp_path / volume)
        if content:
            CONTENT[content](homes[volume])
        return volume

    return SimpleNamespace(store=store, docker=docker, add=add)


@pytest.mark.asyncio
async def test_an_empty_unused_home_past_the_period_is_deleted(wired, caplog) -> None:
    volume = await wired.add("sbx-old", USERS[0], age_days=20)
    with caplog.at_level(logging.WARNING, logger="orchestrator.empty_home"):
        summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == ["sbx-old"]
    assert wired.docker.removed == [volume]
    life = await wired.store.get_lifecycle("sbx-old")
    assert life["deleted"] is True
    row = await wired.store.get("sbx-old")
    assert row.config["home_deleted"]["reason"] == "unused_empty_home"
    line = [r.getMessage() for r in caplog.records if "UNUSED HOME DELETED" in r.getMessage()]
    assert len(line) == 1 and "sbx-old" in line[0] and USERS[0] in line[0] and "untouched" in line[0]
    # A second pass does nothing: the reason on the row closes the case.
    again = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert again["deleted"] == [] and wired.docker.removed == [volume]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["project_file", "agent_tool_call", "terminal_history"])
async def test_a_home_with_user_content_is_never_deleted(wired, kind) -> None:
    await wired.add("sbx-used", USERS[0], age_days=400, content=kind)
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and summary["kept_used"] == 1
    assert wired.docker.removed == []
    assert (await wired.store.get_lifecycle("sbx-used"))["deleted"] is False
    # Proven used once, never inspected again this process.
    await empty_home.sweep_unused_homes(wired.store, force=True)
    assert len(wired.docker.inspected) == 1


@pytest.mark.asyncio
async def test_an_empty_home_inside_the_period_is_kept_and_not_inspected(wired) -> None:
    await wired.add("sbx-new", USERS[0], age_days=3)
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and wired.docker.inspected == []
    assert wired.docker.removed == []


@pytest.mark.asyncio
async def test_a_newer_row_on_the_same_home_restarts_the_clock(wired) -> None:
    await wired.add("sbx-a", USERS[0], age_days=30)
    await wired.store.save(_row("sbx-b", USERS[0], age_days=2))
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and wired.docker.removed == []


@pytest.mark.asyncio
async def test_an_always_on_home_is_never_deleted_even_when_empty(wired) -> None:
    await wired.add("sbx-staff", USERS[0], age_days=60, labels={"always_on": "true"})
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and wired.docker.inspected == []


@pytest.mark.asyncio
async def test_a_home_with_a_box_up_is_never_deleted(wired) -> None:
    await wired.add("sbx-up", USERS[0], age_days=60, status=SandboxStatus.RUNNING)
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and wired.docker.inspected == []


@pytest.mark.asyncio
async def test_an_attached_container_defers_the_deletion(wired) -> None:
    volume = await wired.add("sbx-zombie", USERS[0], age_days=60)
    wired.docker.attached.add(volume)
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and wired.docker.removed == []
    assert (await wired.store.get_lifecycle("sbx-zombie"))["deleted"] is False


@pytest.mark.asyncio
async def test_unreadable_knobs_turn_the_sweep_off(wired) -> None:
    await wired.add("sbx-old", USERS[0], age_days=60)
    from orchestrator import knobs
    knobs._cache[knobs.FEATURE] = (10**12, {})  # no rows registered
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["ran"] is False and wired.docker.removed == []


@pytest.mark.asyncio
async def test_zero_days_turns_the_sweep_off(wired) -> None:
    await wired.add("sbx-old", USERS[0], age_days=60)
    seed_sandbox_knobs({**KNOBS, empty_home.AFTER_DAYS_KNOB: 0})
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["ran"] is False and wired.docker.removed == []


@pytest.mark.asyncio
async def test_deletions_are_bounded_per_pass(wired) -> None:
    for i in range(3):
        await wired.add(f"sbx-{i}", USERS[i], age_days=30 + i)
    seed_sandbox_knobs({**KNOBS, empty_home.MAX_PER_PASS_KNOB: 2})
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert len(summary["deleted"]) == 2 and summary["left_for_next_pass"] == 1
    # Longest-untouched first.
    assert set(summary["deleted"]) == {"sbx-2", "sbx-1"}


@pytest.mark.asyncio
async def test_the_sweep_never_runs_on_the_ec2_tier(wired, monkeypatch) -> None:
    await wired.add("sbx-old", USERS[0], age_days=60)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "ec2")
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["ran"] is False and wired.docker.removed == []


@pytest.mark.asyncio
async def test_an_untouched_aidream_template_home_is_deleted(wired, tmp_path) -> None:
    volume = await wired.add("sbx-aidream", USERS[0], age_days=30)
    home = wired.docker.homes[volume]
    _template_clone(home, tmp_path)
    for d in ("workspace", "temp"):
        (home / d).mkdir()
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == ["sbx-aidream"] and wired.docker.removed == [volume]


@pytest.mark.asyncio
async def test_an_aidream_home_with_an_edit_is_kept(wired, tmp_path) -> None:
    volume = await wired.add("sbx-aidream", USERS[0], age_days=30)
    CLONE_WORK["dirty_edit"](_template_clone(wired.docker.homes[volume], tmp_path))
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and summary["kept_used"] == 1
    assert wired.docker.removed == []
