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

SKEL = Path(__file__).resolve().parent / "fixtures" / "ubuntu-skel"
ADMIN_KEYS = Path(__file__).resolve().parents[2] / "sandbox-image" / "config" / "admin_authorized_keys"
#: Release commit of each fixture clone, as the host's image label would carry it.
RELEASE_SHAS: list[str] = []

GITCONFIG_BOOT = (
    '[credential "https://github.com"]\n\tusername = x-access-token\n'
    "[credential]\n"
    "\thelper = cache --socket=/home/agent/.matrx/runtime/git-credential-cache.sock --timeout=1800\n"
    "\thelper = /opt/sandbox/scripts/matrx-git-credential-env\n"
)


def _skeleton(home: Path, *, aidream: bool = False) -> Path:
    """Exactly what the two live homes judged untouched on 2026-10-16 held
    (outside cloud-files), plus the layout's empty dirs and a big mirror."""
    for d in ("cloud-files", "repos", "projects", "scratch", ".matrx/plans",
              ".matrx/skills", ".matrx/memory", ".matrx/runtime/tool-calls/_runtime",
              ".matrx/runtime/shell-logs", ".matrx/runtime/session-reports",
              ".matrx/instructions", ".ssh"):
        (home / d).mkdir(parents=True, exist_ok=True)
    bashrc = (SKEL / "bashrc").read_text()
    if aidream:
        bashrc += ". /etc/profile.d/aidream.sh\n"
    bashrc += "[ -f ~/.sandbox_env ] && source ~/.sandbox_env\n"
    if aidream:
        bashrc += ('# SANDBOX_PROMPT\nexport PS1="\\[\\033[1;36m\\]sandbox:${SANDBOX_ID:-?}'
                   '\\[\\033[0m\\] \\[\\033[1;34m\\]\\w\\[\\033[0m\\] \\$ "\n'
                   "alias ll='ls -alh --color=auto'\nalias la='ls -A --color=auto'\n")
    (home / ".bashrc").write_text(bashrc)
    (home / ".profile").write_text((SKEL / "profile").read_text())
    (home / ".bash_logout").write_text((SKEL / "bash_logout").read_text())
    (home / ".gitconfig").write_text(GITCONFIG_BOOT)
    (home / ".sandbox_env").write_text("export SANDBOX_ID=sbx\n")
    (home / ".ssh/authorized_keys").write_bytes(ADMIN_KEYS.read_bytes())
    (home / ".matrx/instructions/SANDBOX_LAYOUT.md").write_text("# layout\n" * 200)
    (home / ".matrx/session.json").write_text("{}")
    (home / ".matrx/session.prev.json").write_text("{}")
    (home / ".matrx/session-report.md").write_text("report")
    (home / ".matrx/runtime/cloud-files-down-complete").write_text("")
    (home / ".matrx/runtime/cloud-sync-queue.jsonl").write_text(
        '{"event_id": "e1", "kind": "upsert", "rel_path": "a.pdf", "queued_at": 1}\n'
        '{"event_id": "e1", "done": true, "completed_at": 2}\n')
    arg0 = home / ".codex/tmp/arg0/codex-arg0413WyQ"
    arg0.mkdir(parents=True)
    (arg0 / ".lock").write_bytes(b"")
    for link in ("apply_patch", "applypatch", "codex-execve-wrapper", "codex-linux-sandbox"):
        (arg0 / link).symlink_to(empty_home.CODEX_ARG0_TARGET)
    # The person's AI Matrx files, mirrored down at boot: big, and NOT use.
    (home / "cloud-files/report.pdf").write_bytes(b"x" * 5_000_000)
    return home


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(text)


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
    # The review's holes: hidden entries are content unless boot made them.
    "claude_credentials": lambda h: _append(h / ".claude/.credentials.json", "{}"),
    "claude_transcript": lambda h: _append(h / ".claude/projects/x/1.jsonl", "{}"),
    "ssh_private_key": lambda h: _append(h / ".ssh/id_ed25519", "KEY"),
    "gitconfig_identity": lambda h: _append(h / ".gitconfig", "[user]\n\tname = Ada\n"),
    "bashrc_alias": lambda h: _append(h / ".bashrc", "alias gs='git status'\n"),
    "profile_edit": lambda h: _append(h / ".profile", "export EDITOR=vim\n"),
    "matrx_other": lambda h: _append(h / ".matrx/notes.md", "n"),
    "own_plan_sign_in": lambda h: _append(h / ".matrx/own-plan/claude/.claude/.claude.json", "{}"),
    "pending_cloud_upload": lambda h: _append(
        h / ".matrx/runtime/cloud-sync-queue.jsonl",
        '{"event_id": "e2", "kind": "upsert", "rel_path": "new.pdf", "queued_at": 3}\n'),
    "dotfile_unknown": lambda h: _append(h / ".npmrc", "//registry/:_authToken=x\n"),
    # Second review: allow-listed trees accepted anything under them.
    "cache_notes": lambda h: _append(h / ".cache/work/notes.txt", "n"),
    "codex_tmp_file": lambda h: _append(h / ".codex/tmp/mywork.py", "x"),
    "codex_arg0_extra": lambda h: _append(h / ".codex/tmp/arg0/codex-arg0413WyQ/mine.py", "x"),
    "codex_arg0_retargeted_link": lambda h: (
        (h / ".codex/tmp/arg0/codex-arg0413WyQ/apply_patch").unlink(),
        (h / ".codex/tmp/arg0/codex-arg0413WyQ/apply_patch").symlink_to("/home/agent/x")),
    "session_report_extra": lambda h: _append(h / ".matrx/runtime/session-reports/mine.md", "m"),
    "runtime_tool_call_extra": lambda h: _append(h / ".matrx/runtime/tool-calls/_runtime/mine.md", "m"),
    "symlink_in_cache": lambda h: ((h / ".cache").mkdir(exist_ok=True),
                                   (h / ".cache/link").symlink_to("/etc/passwd")),
    "symlink_replacing_layout_file": lambda h: (
        (h / ".matrx/session.json").unlink(),
        (h / ".matrx/session.json").symlink_to("/home/agent/projects/x")),
    "extra_authorized_key": lambda h: _append(h / ".ssh/authorized_keys", "ssh-ed25519 AAAAmine me\n"),
    "gitconfig_helper_suffix": lambda h: (h / ".gitconfig").write_text(GITCONFIG_BOOT.replace(
        "matrx-git-credential-env", "matrx-git-credential-env --also-mine")),
}


def _run_helper_program(home: Path, max_bytes: int) -> dict:
    """Run the EXACT program the helper container runs, against ``home``."""
    out = subprocess.run(
        [sys.executable, "-c", empty_home.helper_program(), str(home), str(max_bytes),
         json.dumps(RELEASE_SHAS)],
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
    for d in ("workspace", "temp", "projects/new-thing", "repos/acme", ".config/empty"):
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
    (upstream / ".gitignore").write_text(".venv/\n__pycache__/\n.env\ndata/\n")
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
    (clone / ".venv/lib/site-packages/pkg-1.0.dist-info").mkdir(parents=True)
    (clone / ".venv/pyvenv.cfg").write_text("home = /opt/uv-python\n")
    (clone / ".venv/lib/wheels.bin").write_bytes(b"x" * 3_000_000)  # > max_bytes
    RELEASE_SHAS.append(sha)
    return clone


CLONE_WORK = {
    "untracked_file": lambda c: (c / "notes.md").write_text("mine"),
    "dirty_edit": lambda c: (c / "app.py").write_text("print('edited')\n"),
    "unpushed_commit": lambda c: ((c / "app.py").write_text("print('v2')\n"),
                                  _git(c, "commit", "-q", "-am", "mine")),
    "stash": lambda c: ((c / "app.py").write_text("print('wip')\n"),
                        _git(c, "stash", "-q")),
    # Ignored by the clone's .gitignore, so plain `git status` never shows them.
    "ignored_env_file": lambda c: (c / ".env").write_text("SECRET=1\n"),
    "ignored_data_dump": lambda c: ((c / "data").mkdir(), (c / "data/big.csv").write_text("a,b\n")),
    "assume_unchanged_edit": lambda c: ((c / "app.py").write_text("print('hidden')\n"),
                                        _git(c, "update-index", "--assume-unchanged", "app.py")),
    "skip_worktree_edit": lambda c: ((c / "app.py").write_text("print('hidden')\n"),
                                     _git(c, "update-index", "--skip-worktree", "app.py")),
    "commit_hidden_behind_fetch_head": lambda c: (
        (c / "app.py").write_text("print('v2')\n"), _git(c, "commit", "-q", "-am", "mine"),
        (c / ".git/FETCH_HEAD").write_text(subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=c, capture_output=True, text=True).stdout)),
}


def test_a_pristine_template_clone_is_empty(tmp_path) -> None:
    home = _skeleton(tmp_path / "home")
    _template_clone(home, tmp_path)
    for d in ("workspace", "temp"):
        (home / d).mkdir()
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is True, verdict
    assert verdict["bytes"] < 1_000_000  # the clone is never weighed


@pytest.fixture(autouse=True)
def _fresh_release_shas():
    RELEASE_SHAS.clear()
    yield
    RELEASE_SHAS.clear()


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

class _FakeHelper:
    """A detached helper container: runs the program on wait(); a home listed
    in ``docker.hang`` never finishes, as a wedged inspection would."""

    def __init__(self, docker, volume, command):
        self.docker, self.volume, self.command = docker, volume, command
        self.out = b""
        self.killed = self.removed = False

    def wait(self, timeout=None):
        if self.volume in self.docker.hang:
            raise TimeoutError("read timed out")  # what requests raises, in kind
        args = [str(self.docker.homes[self.volume]) if a == "/home/agent" else a
                for a in self.command]
        done = subprocess.run([sys.executable, *args], capture_output=True)
        self.out = done.stdout
        return {"StatusCode": done.returncode}

    def logs(self, stdout=True, stderr=False):
        return self.out

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True
        self.docker.helpers_removed += 1


class _FakeDocker:
    """Volumes are directories; ``containers.run`` runs the helper program
    against the directory bound at /home/agent, exactly as the helper would."""

    def __init__(self, homes: dict[str, Path]):
        self.homes = homes
        self.removed: list[str] = []
        self.inspected: list[str] = []
        self.attached: set[str] = set()
        self.hang: set[str] = set()
        self.helpers_removed = 0
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
                assert kwargs["network_disabled"] is True and kwargs["detach"] is True
                (volume, bind), = volumes.items()
                assert bind == {"bind": "/home/agent", "mode": "ro"}
                docker.inspected.append(volume)
                return _FakeHelper(docker, volume, command)

        class _Images:
            def list(self):
                return [SimpleNamespace(labels={"com.aimatrx.aidream.sha": s}) for s in RELEASE_SHAS]

        self.volumes = _Volumes()
        self.containers = _Containers()
        self.images = _Images()


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


@pytest.mark.asyncio
async def test_a_hung_inspection_is_killed_removed_and_keeps_the_home(wired, monkeypatch) -> None:
    volume = await wired.add("sbx-hang", USERS[0], age_days=60)
    wired.docker.hang.add(volume)
    summary = await empty_home.sweep_unused_homes(wired.store, force=True)
    assert summary["deleted"] == [] and summary["failed"] == 1
    assert wired.docker.removed == [] and wired.docker.helpers_removed == 1
    assert (await wired.store.get_lifecycle("sbx-hang"))["deleted"] is False


@pytest.mark.asyncio
async def test_the_reaper_tick_never_waits_on_the_sweep(monkeypatch) -> None:
    """A wedged pass must not stall expiry/liveness/revive: launch returns at
    once, and a second launch while one runs is refused (single flight)."""
    import asyncio
    gate = asyncio.Event()

    async def wedged(store, *, force=False):
        await gate.wait()
        return {"deleted": []}

    monkeypatch.setattr(empty_home, "sweep_unused_homes", wedged)
    monkeypatch.setattr(empty_home, "_running", None)
    monkeypatch.setattr(empty_home, "_last_sweep_at", None)
    assert await asyncio.wait_for(asyncio.to_thread(lambda: None), 1) is None
    assert empty_home.launch_sweep(object()) is True
    monkeypatch.setattr(empty_home, "_last_sweep_at", None)  # due again
    assert empty_home.launch_sweep(object()) is False  # still running
    gate.set()
    await asyncio.sleep(0)
    await empty_home._running


@pytest.mark.asyncio
async def test_the_reaper_starts_the_sweep_without_awaiting_it(monkeypatch) -> None:
    from contextlib import ExitStack
    from orchestrator import reaper
    calls = []
    monkeypatch.setattr(empty_home, "launch_sweep", lambda store: calls.append(store) or True)

    async def _empty(*a, **k):
        return []

    async def _healthy(*a, **k):
        return {"stopped": [], "refreshed": 0}

    async def _revive(*a, **k):
        return {"revived": [], "failed": 0, "left_for_next_tick": 0}

    store = InMemorySandboxStore()
    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    monkeypatch.setattr(reaper, "_lease_reaper_fleet", lambda _t: (ExitStack(), set()))
    monkeypatch.setattr("orchestrator.reconcile.reap_zombie_containers", _empty)
    monkeypatch.setattr("orchestrator.reconcile.reconcile_liveness", _healthy)
    monkeypatch.setattr("orchestrator.always_on.revive_always_on", _revive)
    summary = await reaper._reap_once()
    assert calls == [store] and summary["unused_home_sweep_started"] is True


def test_every_bashrc_boot_line_is_written_by_the_image() -> None:
    """The allow-list may only name lines sandbox-image really appends."""
    root = Path(__file__).resolve().parents[2]
    corpus = "".join(p.read_text() for p in [
        *(root / "sandbox-image" / "scripts").glob("entrypoint*.sh"),
        root / "sandbox-local" / "scripts" / "entrypoint-local.sh",
        root / "sandbox-image" / "Dockerfile.aidream",
    ])
    for line in empty_home.BASHRC_BOOT_LINES:
        assert line in corpus, line


def test_the_skel_fixture_is_what_the_allow_list_pins() -> None:
    import hashlib
    for name, rel in (("profile", ".profile"), ("bash_logout", ".bash_logout")):
        assert hashlib.sha256((SKEL / name).read_bytes()).hexdigest() == empty_home.SKEL_SHA256[rel]
    assert hashlib.sha256((SKEL / "bashrc").read_bytes()).hexdigest() == empty_home.SKEL_BASHRC_SHA256


def test_an_aidream_boot_skeleton_is_empty(tmp_path) -> None:
    assert _run_helper_program(_skeleton(tmp_path / "home", aidream=True), 1_000_000)["empty"] is True


def test_a_clone_from_an_unknown_release_is_used(tmp_path) -> None:
    """No image on the host carries its release commit and it has no remote
    refs: nothing proves its commits are upstream's, so it is kept."""
    home = _skeleton(tmp_path / "home")
    _template_clone(home, tmp_path)
    RELEASE_SHAS.clear()
    assert _run_helper_program(home, 1_000_000)["empty"] is False


def test_a_venv_changed_after_its_boot_copy_is_used(tmp_path, monkeypatch) -> None:
    import time
    monkeypatch.setattr(empty_home, "VENV_COPY_WINDOW_SECONDS", 1)
    home = _skeleton(tmp_path / "home")
    clone = _template_clone(home, tmp_path)
    assert _run_helper_program(home, 1_000_000)["empty"] is True
    time.sleep(2.2)
    (clone / ".venv/lib/site-packages/mine-0.1.dist-info").mkdir()  # a pip install
    (clone / ".venv/lib/site-packages/mine-0.1.dist-info/RECORD").write_text("x")
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is False and "venv" in str(verdict["evidence"]), verdict


def test_venv_bytecode_after_the_copy_is_not_use(tmp_path, monkeypatch) -> None:
    import time
    monkeypatch.setattr(empty_home, "VENV_COPY_WINDOW_SECONDS", 1)
    home = _skeleton(tmp_path / "home")
    clone = _template_clone(home, tmp_path)
    time.sleep(2.2)
    pyc = clone / ".venv/lib/site-packages/__pycache__"
    pyc.mkdir()
    (pyc / "x.cpython-313.pyc").write_bytes(b"x")
    assert _run_helper_program(home, 1_000_000)["empty"] is True


def test_the_pinned_authorized_keys_is_the_image_admin_file() -> None:
    import hashlib
    assert (hashlib.sha256(ADMIN_KEYS.read_bytes()).hexdigest()
            == empty_home.SKEL_SHA256[".ssh/authorized_keys"])


@pytest.mark.parametrize("planted", ["mydata.csv", "deep/x.txt", "fake.pyc/"])
def test_non_bytecode_under_a_venv_pycache_is_use(tmp_path, monkeypatch, planted) -> None:
    """Only a regular *.pyc directly inside __pycache__ is exempt."""
    import time
    monkeypatch.setattr(empty_home, "VENV_COPY_WINDOW_SECONDS", 1)
    home = _skeleton(tmp_path / "home")
    clone = _template_clone(home, tmp_path)
    time.sleep(2.2)
    pyc = clone / ".venv/lib/site-packages/__pycache__"
    pyc.mkdir()
    (pyc / "ok.cpython-313.pyc").write_bytes(b"x")
    target = pyc / planted.rstrip("/")
    if planted.endswith("/"):  # a directory named like byte-code, with data in it
        target.mkdir()
        (target / "data.txt").write_text("x")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    verdict = _run_helper_program(home, 1_000_000)
    assert verdict["empty"] is False and "__pycache__" in str(verdict["evidence"]), verdict
