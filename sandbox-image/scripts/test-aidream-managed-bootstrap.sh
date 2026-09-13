#!/usr/bin/env bash
# Executable guard for the isolated managed aidream bootstrap.  It exercises a
# real ``python -I -B`` process with hostile cwd/PYTHONPATH/HOME inputs.
set -euo pipefail

BOOTSTRAP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/aidream-managed-bootstrap.py"
PYTHON="${PYTHON:-python3}"
scratch="$(mktemp -d)"
scratch="$(cd "$scratch" && pwd -P)"
trap 'rm -rf "$scratch"' EXIT

template="$scratch/template"
mkdir -p "$template/aidream" "$scratch/home" "$scratch/cwd"
printf 'SENTINEL = "immutable-import"
' > "$template/aidream/__init__.py"
cat > "$template/run.py" <<'PY'
import aidream
import os
import sys
from pathlib import Path

assert __name__ == "__main__"
assert sys.path[0] == os.getcwd()
Path(os.environ["PROBE_OUT"]).write_text(aidream.SENTINEL, encoding="utf-8")
PY
printf 'raise RuntimeError("hostile sitecustomize ran")
' > "$scratch/home/sitecustomize.py"
printf 'raise RuntimeError("hostile cwd module ran")
' > "$scratch/cwd/aidream.py"

bootstrap_copy="$scratch/bootstrap.py"
sed "s|Path(\"/opt/aidream-template\")|Path(\"$template\")|" "$BOOTSTRAP" > "$bootstrap_copy"
# The production image proves root ownership and a read-only mount in
# build-aidream.sh. This local fixture cannot create either, so retain the
# executable -I import-chain proof while swapping only those host predicates.
sed -i.bak \
    -e 's/metadata.st_uid != 0/metadata.st_uid not in (0, os.getuid())/g' \
    -e 's/if not os.statvfs(TEMPLATE_ROOT).f_flag & getattr(os, "ST_RDONLY", 1):/if False:/' \
    "$bootstrap_copy"
rm -f "$bootstrap_copy.bak"
chmod 0555 "$bootstrap_copy"

PROBE_OUT="$scratch/positive" HOME="$scratch/home" PYTHONPATH="$scratch/cwd" \
    "$PYTHON" -I -B "$bootstrap_copy"
test "$(cat "$scratch/positive")" = "immutable-import"

# A symlinked root must be refused even though it points at the same fixture.
ln -s "$template" "$scratch/template-link"
sed "s|Path(\"$template\")|Path(\"$scratch/template-link\")|" "$bootstrap_copy" > "$scratch/symlink-bootstrap.py"
if "$PYTHON" -I -B "$scratch/symlink-bootstrap.py" >"$scratch/symlink.out" 2>&1; then
    echo "expected symlinked trusted root to fail" >&2
    exit 1
fi
grep -q "symlinked trusted path" "$scratch/symlink.out"
