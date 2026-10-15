#!/usr/bin/env python3
"""Generate the orchestrator's config-attestation manifest from code (never hand-written).

    python scripts/config_manifest.py --root <orchestrator tree> --build-sha <40-hex> --out manifest.json

The release step passes a ``git archive $RELEASE_SHA`` export of ``orchestrator/`` as ``--root`` so the
manifest is exactly the released commit. Exits 1 when any knob read cannot be named (the orchestrator has
none today; a new one must use a literal or a str constant). Shared implementation:
``matrx_utils.config_attestation``. System of record: common-docs/systems/platform/app-config/attestation/FEATURE.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from matrx_utils.config_attestation import Reader, build_manifest, extract

APP = "matrx-sandbox"
FEATURE = "infrastructure.sandbox"
SPEC = Reader(feature_arg=1, key_arg=0, feature_kw="feature", key_kw="key", default_feature=FEATURE)
READERS = {n: SPEC for n in ("knob_int", "knob_float", "knob_str", "knob_bool", "security_knob_bool", "record_read")}
# record_read(feature, key) is the explicit hook beside the store's two raw admission reads; knobs._raw(feature, key)
# is read directly too (migrate.py's auto_update_idle_seconds) — knobs.FEATURE resolves from knobs.py.
READERS["record_read"] = Reader(feature_arg=0, key_arg=1)
READERS["_raw"] = Reader(feature_arg=0, key_arg=1)
EXCLUDE = ("/tests/", "/test_", "conftest.py", "orchestrator/knobs.py", "orchestrator/config_attestation.py")
RAW_SQL = re.compile(
    r'"SELECT value FROM platform\.feature_knob WHERE feature = \$1 AND key = \$2(?: AND archived_at IS NULL)?",'
    r'\s*"([^"]+)", "([^"]+)"'
)


def generate(root: Path, build_sha: str):
    pkg = root / "orchestrator"
    ex = extract(root, [pkg], READERS, exclude=EXCLUDE)
    for f in sorted(pkg.rglob("*.py")):
        for feat, key in RAW_SQL.findall(f.read_text()):
            ex.add(feat, key, str(f.relative_to(root)))
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    manifest = build_manifest(
        app=APP, app_version=version, build_sha=build_sha, knobs=ex.knobs,
        app_config_keys={"min_supported_app_version": ["orchestrator/config_attestation.py"]},
        bootstrap={"database_url": "MATRX_DATABASE_URL (direct Postgres)", "database_realtime_url": "",
                   "publishable_key_prefix": "", "source": "server environment"},
    )
    return manifest, ex.unresolved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--build-sha", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    manifest, unresolved = generate(Path(a.root), a.build_sha)
    if unresolved:
        for u in unresolved:
            print(f"UNRESOLVED knob read {u['file']}:{u['line']}: {u['call']}", file=sys.stderr)
        return 1
    Path(a.out).write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"config manifest: {len(manifest['knobs'])} knobs declared -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
