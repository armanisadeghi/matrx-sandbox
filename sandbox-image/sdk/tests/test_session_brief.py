"""Guards for the start-of-session repo tidy + session brief (tracker 1734465c).

The class this closes: checkouts on the persistent home were found on
long-merged, remote-deleted feature branches tens of thousands of commits
behind main, and nothing told the agent. The tidy must be SAFE BY
CONSTRUCTION — every case where it could lose or hide work is pinned here
against real git repos (a bare "origin" + a clone), never mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from matrx_agent import session_brief as sb
from matrx_agent.cli.__main__ import main


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def commit(repo: Path, name: str, content: str = "x") -> None:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture(autouse=True)
def _isolated_git_identity(tmp_path, monkeypatch):
    """Never read or write the real ~/.gitconfig or a real platform identity."""
    gc = tmp_path / "gitconfig"
    gc.write_text("[user]\n\tname = Real Person\n\temail = real@aimatrx.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.delenv("MATRX_GIT_USER_NAME", raising=False)
    monkeypatch.delenv("MATRX_GIT_USER_EMAIL", raising=False)
    monkeypatch.setattr(sb, "IDENTITY_FILE", tmp_path / "identity.json")
    return gc


@pytest.fixture
def world(tmp_path):
    """origin.git (default main) + a clone at repos/app + a second pusher clone."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-b", "main")
    commit(seed, "a.txt")
    git(seed, "push", "-q", "-u", "origin", "main")
    repos = tmp_path / "repos"
    repos.mkdir()
    app = repos / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(app)], check=True, capture_output=True)
    return {"origin": origin, "seed": seed, "app": app, "repos": repos, "tmp": tmp_path}


def merged_gone_branch(w, branch="feat/x"):
    """The 2026-10-21 shape: app sits on a feature branch that was merged on
    the remote and then deleted there, and main moved on."""
    app, seed = w["app"], w["seed"]
    git(app, "checkout", "-q", "-b", branch)
    commit(app, "feat.txt")
    git(app, "push", "-q", "-u", "origin", branch)
    git(seed, "fetch", "-q", "origin")
    git(seed, "merge", "-q", "--no-ff", f"origin/{branch}", "-m", "merge feat")
    commit(seed, "later.txt")
    git(seed, "push", "-q", "origin", "main")
    git(seed, "push", "-q", "origin", "--delete", branch)


def test_gone_merged_clean_branch_switches_to_default_and_fast_forwards(world):
    merged_gone_branch(world)
    info = sb.tidy_repo(world["app"])
    assert info["branch"] == "main", info
    assert info["previous_branch"] == "feat/x"
    assert info["behind_default"] == 0
    assert any(a.startswith("switched") for a in info["actions"])
    assert any(a.startswith("fast-forwarded") for a in info["actions"])
    # the local branch is kept, never deleted
    assert "feat/x" in git(world["app"], "branch", "--list", "feat/x")


def test_dry_run_changes_nothing(world):
    merged_gone_branch(world)
    head = git(world["app"], "rev-parse", "HEAD")
    info = sb.tidy_repo(world["app"], apply=False)
    assert git(world["app"], "symbolic-ref", "--short", "HEAD") == "feat/x"
    assert git(world["app"], "rev-parse", "HEAD") == head
    assert any("would switch" in n for n in info["notes"])


def test_dirty_tree_on_gone_branch_is_left_alone(world):
    merged_gone_branch(world)
    (world["app"] / "feat.txt").write_text("edited")
    info = sb.tidy_repo(world["app"])
    assert info["branch"] == "feat/x"
    assert (world["app"] / "feat.txt").read_text() == "edited"
    assert any("left alone" in w and "uncommitted" in w for w in info["warnings"])


def test_unpushed_commit_on_gone_branch_is_left_alone(world):
    merged_gone_branch(world)
    commit(world["app"], "local-only.txt")
    head = git(world["app"], "rev-parse", "HEAD")
    info = sb.tidy_repo(world["app"])
    assert info["branch"] == "feat/x"
    assert git(world["app"], "rev-parse", "HEAD") == head
    assert info["unpushed"] == 1
    assert any("left alone" in w for w in info["warnings"])


def test_gone_but_unmerged_branch_is_left_alone(world):
    app = world["app"]
    git(app, "checkout", "-q", "-b", "feat/squashed")
    commit(app, "s.txt")
    git(app, "push", "-q", "-u", "origin", "feat/squashed")
    git(world["seed"], "push", "-q", "origin", "--delete", "feat/squashed")
    info = sb.tidy_repo(app)
    assert info["branch"] == "feat/squashed"
    assert any("not merged" in w for w in info["warnings"])


def test_default_branch_fast_forwards_when_clean(world):
    commit(world["seed"], "b.txt")
    git(world["seed"], "push", "-q", "origin", "main")
    info = sb.tidy_repo(world["app"])
    assert info["behind"] == 0
    assert (world["app"] / "b.txt").exists()


def test_untracked_files_do_not_block_but_are_kept(world):
    commit(world["seed"], "b.txt")
    git(world["seed"], "push", "-q", "origin", "main")
    (world["app"] / "scratch.log").write_text("mine")
    info = sb.tidy_repo(world["app"])
    assert info["untracked"] == 1
    assert (world["app"] / "b.txt").exists()
    assert (world["app"] / "scratch.log").read_text() == "mine"


def test_dirty_default_branch_is_not_fast_forwarded(world):
    commit(world["seed"], "b.txt")
    git(world["seed"], "push", "-q", "origin", "main")
    (world["app"] / "a.txt").write_text("wip")
    info = sb.tidy_repo(world["app"])
    assert not (world["app"] / "b.txt").exists()
    assert (world["app"] / "a.txt").read_text() == "wip"
    assert info["behind"] == 1
    assert any("not fast-forwarded" in w for w in info["warnings"])


def test_diverged_default_branch_is_left_alone(world):
    commit(world["seed"], "b.txt")
    git(world["seed"], "push", "-q", "origin", "main")
    commit(world["app"], "c.txt")
    head = git(world["app"], "rev-parse", "HEAD")
    info = sb.tidy_repo(world["app"])
    assert git(world["app"], "rev-parse", "HEAD") == head
    assert any("diverged" in w for w in info["warnings"])


def test_stashes_are_counted_and_never_touched(world):
    (world["app"] / "a.txt").write_text("stash me")
    git(world["app"], "stash", "-q")
    commit(world["seed"], "b.txt")
    git(world["seed"], "push", "-q", "origin", "main")
    info = sb.tidy_repo(world["app"])
    assert info["stashes"] == 1
    assert len(git(world["app"], "stash", "list").splitlines()) == 1
    assert (world["app"] / "b.txt").exists()  # clean tree still fast-forwards


def test_network_failure_is_reported_not_fatal(world):
    git(world["app"], "remote", "set-url", "origin", str(world["tmp"] / "nowhere.git"))
    info = sb.tidy_repo(world["app"])
    assert info["fetched"] is False
    assert any(w.startswith("fetch failed") for w in info["warnings"])
    assert info["recent_commits"]


def test_operation_in_progress_blocks_moves(world):
    merged_gone_branch(world)
    gd = Path(git(world["app"], "rev-parse", "--absolute-git-dir"))
    (gd / "MERGE_HEAD").write_text(git(world["app"], "rev-parse", "HEAD") + "\n")
    info = sb.tidy_repo(world["app"])
    assert info["branch"] == "feat/x"
    assert info["operation_in_progress"] == "merge"


def test_brief_files_and_cli(world, monkeypatch, tmp_path, capsys):
    merged_gone_branch(world)
    monkeypatch.setattr(sb, "BRIEF_DIR", tmp_path / "m")
    monkeypatch.setattr(sb, "BRIEF_MD", tmp_path / "m" / "session-brief.md")
    monkeypatch.setattr(sb, "BRIEF_JSON", tmp_path / "m" / "session-brief.json")
    monkeypatch.setattr(sb, "LOCK_PATH", tmp_path / "m" / "runtime" / "lock")
    monkeypatch.setenv("MATRX_BRIEF_REPO_ROOTS", str(world["repos"]))

    assert main(["repos", "tidy"]) == 0
    out = capsys.readouterr().out
    assert "# Session brief" in out and "**app**" in out and "tidy: switched" in out
    data = json.loads((tmp_path / "m" / "session-brief.json").read_text())
    assert data["schema_version"] == sb.SCHEMA_VERSION
    assert data["repos"][0]["branch"] == "main"
    assert set(data) >= {"image", "toolchain", "repos", "warnings", "generated_at"}

    assert main(["brief", "--cached"]) == 0
    assert "# Session brief" in capsys.readouterr().out
    assert main(["brief"]) == 0  # status mode: no fetch, no changes
    assert json.loads((tmp_path / "m" / "session-brief.json").read_text())["mode"] == "status"


def test_overlapping_runs_never_tidy_twice(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "LOCK_PATH", tmp_path / "lock")
    held = sb._Lock(tmp_path / "lock")
    assert held.acquire()
    try:
        code, md = sb.run(fetch=False, apply=True, trigger="test")
        assert code == 75 and md is None
    finally:
        held.release()


def test_discover_finds_repos_one_level_deep(world, tmp_path):
    (world["repos"] / "not-a-repo").mkdir()
    found = sb.discover_repos([world["repos"], tmp_path / "missing"])
    assert [p.name for p in found] == ["app"]


def test_daemon_activation_kicks_the_brief():
    """Boot trigger: the daemon (runs as the agent user, after layout) starts
    the brief detached from its post-commit activation, never awaiting it."""
    src = (Path(sb.__file__).parent / "api" / "main.py").read_text()
    assert "session_brief.spawn_detached(" in src


# ─── git identity (2026-10-21: a test left user.name=Test in ~/.gitconfig) ──

def _global(key):
    return subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()


def test_placeholder_identity_is_flagged_loudly_when_platform_identity_unknown(_isolated_git_identity):
    _isolated_git_identity.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    facts = sb.git_identity_facts([], apply=True)
    assert facts["placeholder"] is True
    assert any("GIT IDENTITY IS A PLACEHOLDER" in w for w in facts["warnings"])
    assert _global("user.name") == "Test"  # nothing to fix it with -> untouched


def test_empty_identity_counts_as_placeholder(_isolated_git_identity):
    _isolated_git_identity.write_text("")
    assert sb.git_identity_facts([], apply=False)["placeholder"] is True


def test_placeholder_identity_is_fixed_from_platform_identity(_isolated_git_identity, monkeypatch):
    _isolated_git_identity.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("MATRX_GIT_USER_NAME", "Armani Sadeghi")
    monkeypatch.setenv("MATRX_GIT_USER_EMAIL", "dev@aimatrx.com")
    facts = sb.git_identity_facts([], apply=True)
    assert facts["placeholder"] is False and facts["actions"]
    assert _global("user.name") == "Armani Sadeghi"
    assert _global("user.email") == "dev@aimatrx.com"


def test_status_mode_never_writes_identity(_isolated_git_identity, monkeypatch):
    _isolated_git_identity.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("MATRX_GIT_USER_NAME", "Armani Sadeghi")
    monkeypatch.setenv("MATRX_GIT_USER_EMAIL", "dev@aimatrx.com")
    facts = sb.git_identity_facts([], apply=False)
    assert facts["placeholder"] is True and _global("user.name") == "Test"


def test_real_identity_is_never_replaced(_isolated_git_identity, monkeypatch, tmp_path):
    (tmp_path / "identity.json").write_text(json.dumps({"name": "Someone Else", "email": "else@aimatrx.com"}))
    facts = sb.git_identity_facts([], apply=True)
    assert facts["placeholder"] is False and not facts["actions"]
    assert _global("user.name") == "Real Person"


def test_placeholder_platform_identity_is_not_used(_isolated_git_identity, monkeypatch):
    _isolated_git_identity.write_text("")
    monkeypatch.setenv("MATRX_GIT_USER_NAME", "Test")
    monkeypatch.setenv("MATRX_GIT_USER_EMAIL", "test@example.com")
    facts = sb.git_identity_facts([], apply=True)
    assert facts["placeholder"] is True and not facts["actions"]


def test_repo_local_placeholder_and_placeholder_commits_are_reported(world):
    app = world["app"]
    git(app, "config", "user.name", "Test")
    git(app, "config", "user.email", "test@example.com")
    (app / "t.txt").write_text("x")
    subprocess.run(["git", "-C", str(app), "add", "t.txt"], check=True)
    subprocess.run(["git", "-C", str(app), "commit", "-q", "-m", "by test"], check=True,
                   env={k: v for k, v in os.environ.items() if not k.startswith("GIT_AUTHOR")})
    facts = sb.git_identity_facts([app], apply=True)
    assert "app" in facts["repo_overrides"]
    assert facts["placeholder_commits"].get("app") == 1
    md = sb.render_markdown(sb.build_brief(fetch=False, apply=False, trigger="t", repos=[app]))
    assert "Git identity" in md and "placeholder" in md
