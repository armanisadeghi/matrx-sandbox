"""Guard: the orchestrator has exactly ONE public.app_config reader, ``Store.app_config_floor`` in store.py.

It is called from one place (config_attestation.py, which records the read for attestation). A second SQL/PostgREST
read of app_config would be an unrecorded read that attestation cannot see. RED on a planted reader, GREEN on the tree
(CONFIG-ATTEST-ROLLOUT).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SQL_READER = re.compile(r"(?:from|join|into|update)\s+(?:public\.)?app_config\b|rest/v1/app_config|\.table\(\s*['\"]app_config", re.I)
FLOOR_CALL = re.compile(r"\.app_config_floor\(")
SQL_HOME = "orchestrator/orchestrator/store.py"
CALL_HOME = {"orchestrator/orchestrator/config_attestation.py"}
SKIP = {"node_modules", ".venv", "__pycache__", ".git", "migrations", "tests"}


def find_violations(root: Path) -> list[str]:
    bad: list[str] = []
    for f in root.rglob("*.py"):
        parts = f.relative_to(root).parts
        if SKIP & set(parts) or f.name.startswith("test_"):
            continue
        rel = f.relative_to(root).as_posix()
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            code = line.split("#", 1)[0]
            if rel != SQL_HOME and SQL_READER.search(code):
                bad.append(f"{rel}:{i}  second app_config reader: {line.strip()[:90]}")
            if rel not in CALL_HOME and rel != SQL_HOME and FLOOR_CALL.search(code):
                bad.append(f"{rel}:{i}  app_config_floor called outside config_attestation: {line.strip()[:90]}")
    return bad


def test_guard_goes_red_on_a_planted_second_reader(tmp_path: Path) -> None:
    (tmp_path / "orchestrator" / "orchestrator").mkdir(parents=True)
    (tmp_path / "orchestrator" / "orchestrator" / "x.py").write_text('v = await c.fetchval("SELECT 1 FROM public.app_config")\n')
    assert find_violations(tmp_path)
    (tmp_path / "orchestrator" / "orchestrator" / "x.py").write_text("f = await store.app_config_floor('a')\n")
    assert find_violations(tmp_path)


def test_guard_passes_on_the_sanctioned_shape(tmp_path: Path) -> None:
    d = tmp_path / "orchestrator" / "orchestrator"
    d.mkdir(parents=True)
    (d / "store.py").write_text('v = await conn.fetchval("SELECT x FROM public.app_config WHERE app = $1", a)\n')
    (d / "config_attestation.py").write_text("f = await store.app_config_floor(APP)\n")
    assert find_violations(tmp_path) == []


def test_orchestrator_has_one_app_config_reader() -> None:
    bad = find_violations(REPO)
    assert not bad, "\n".join(bad)
