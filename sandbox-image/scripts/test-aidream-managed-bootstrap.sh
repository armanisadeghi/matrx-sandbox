#!/usr/bin/env bash
# Executable guard for the isolated managed aidream bootstrap.  It exercises a
# real ``python -I -B`` process with hostile cwd/PYTHONPATH/HOME inputs.
set -euo pipefail

BOOTSTRAP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/aidream-managed-bootstrap.py"
PYTHON="${PYTHON:-python3}"
scratch="$(mktemp -d)"
scratch="$(cd "$scratch" && pwd -P)"
cleanup() { chmod -R u+w "$scratch" 2>/dev/null || true; rm -rf "$scratch"; }
trap cleanup EXIT

trusted_parent="$scratch/trusted-parent"
template="$trusted_parent/aidream-template"
mkdir -p "$template/aidream/settings" "$template/config" "$scratch/home" "$scratch/cwd"
printf 'SENTINEL = "immutable-import"
' > "$template/aidream/__init__.py"
cat > "$template/run.py" <<'PY'
import aidream
import os
import sys
from pathlib import Path

if os.getenv("EXPECT_MAIN"):
    assert __name__ == "__main__"
assert sys.path[0] == os.getcwd()
temp = Path(os.environ["MATRX_TEMP_DIR"])
for directory in (Path(os.environ["LOG_DIR"]), temp, temp / "reports"):
    directory.mkdir(parents=True, exist_ok=True)
Path(os.environ["PROBE_OUT"]).write_text(aidream.SENTINEL, encoding="utf-8")
PY
cat > "$template/config/settings.py" <<'PY'
import os
from pathlib import Path

temp = Path(os.environ["MATRX_TEMP_DIR"])
temp.joinpath("logs").mkdir(parents=True, exist_ok=True)
temp.joinpath("reports").mkdir(parents=True, exist_ok=True)
Path(os.environ["IMPORT_TRACE"]).open("a", encoding="utf-8").write("config\n")
PY
cat > "$template/aidream/settings/__init__.py" <<'PY'
import os
from pathlib import Path

Path(os.environ["MATRX_TEMP_DIR"], "logs").mkdir(parents=True, exist_ok=True)
Path(os.environ["IMPORT_TRACE"]).open("a", encoding="utf-8").write("settings\n")
PY
printf 'raise RuntimeError("hostile sitecustomize ran")
' > "$scratch/home/sitecustomize.py"
printf 'raise RuntimeError("hostile cwd module ran")
' > "$scratch/cwd/aidream.py"

bootstrap_copy="$scratch/bootstrap.py"
sed \
    -e "s|Path(\"/opt/aidream-template\")|Path(\"$template\")|" \
    -e "s|Path(\"/opt\")|Path(\"$trusted_parent\")|" \
    "$BOOTSTRAP" > "$bootstrap_copy"
# The production image proves root ownership and a read-only mount in
# build-aidream.sh. This local fixture cannot create either, so retain the
# executable -I import-chain proof while swapping only those host predicates.
sed -i.bak \
    -e 's/metadata.st_uid != 0/metadata.st_uid not in (0, os.getuid())/g' \
    -e 's/if not os.statvfs(TEMPLATE_ROOT).f_flag & getattr(os, "ST_RDONLY", 1):/if False:/' \
    "$bootstrap_copy"
rm -f "$bootstrap_copy.bak"
chmod -R a-w "$trusted_parent"
chmod 0555 "$bootstrap_copy"

PROBE_OUT="$scratch/positive" MATRX_TEMP_DIR="$scratch/default-temp" LOG_DIR="$scratch/default-log" \
    EXPECT_MAIN=1 HOME="$scratch/home" PYTHONPATH="$scratch/cwd" \
    "$PYTHON" -I -B "$bootstrap_copy"
test "$(cat "$scratch/positive")" = "immutable-import"

# Reproduce the released guard gap in an isolated bootstrap copy: run.py alone
# writes reports but not the temp logs that the two settings writers create.
old_bootstrap="$scratch/old-bootstrap.py"
sed 's/for import_file in VERIFY_IMPORT_FILES:/for import_file in ():/g' "$bootstrap_copy" > "$old_bootstrap"
if PROBE_OUT="$scratch/old-positive" IMPORT_TRACE="$scratch/old-trace" \
    MATRX_TEMP_DIR="$scratch/old-temp" LOG_DIR="$scratch/old-log" HOME="$scratch/home" PYTHONPATH="$scratch/cwd" \
    "$PYTHON" -I -B "$old_bootstrap" --verify-imports; then
    test -d "$scratch/old-temp/reports"
    test -d "$scratch/old-log"
    test ! -d "$scratch/old-temp/logs"
else
    echo "released run.py-only import check unexpectedly failed" >&2
    exit 1
fi

IMPORT_TRACE="$scratch/trace" MATRX_TEMP_DIR="$scratch/temp" LOG_DIR="$scratch/log" HOME="$scratch/home" \
    PYTHONPATH="$scratch/cwd" PROBE_OUT="$scratch/verify-positive" \
    "$PYTHON" -I -B "$bootstrap_copy" --verify-imports
test -d "$scratch/temp/logs"
test -d "$scratch/temp/reports"
test -d "$scratch/log"
test "$(cat "$scratch/trace")" = $'config\nsettings'

# Omit one canonical writer in a scratch copy: the trace forces the guard red.
omit_bootstrap="$scratch/omit-bootstrap.py"
sed 's/for import_file in VERIFY_IMPORT_FILES:/for import_file in ():/g' "$bootstrap_copy" > "$omit_bootstrap"
IMPORT_TRACE="$scratch/omit-trace" MATRX_TEMP_DIR="$scratch/omit-temp" LOG_DIR="$scratch/omit-log" HOME="$scratch/home" \
    PYTHONPATH="$scratch/cwd" PROBE_OUT="$scratch/omit-positive" \
    "$PYTHON" -I -B "$omit_bootstrap" --verify-imports
if [ "$(cat "$scratch/omit-trace" 2>/dev/null || true)" = $'config\nsettings' ]; then
    echo "expected omitted startup writers to fail trace guard" >&2
    exit 1
fi

# A symlinked root must be refused even though it points at the same fixture.
ln -s "$template" "$scratch/template-link"
sed "s|Path(\"$template\")|Path(\"$scratch/template-link\")|" "$bootstrap_copy" > "$scratch/symlink-bootstrap.py"
if "$PYTHON" -I -B "$scratch/symlink-bootstrap.py" >"$scratch/symlink.out" 2>&1; then
    echo "expected symlinked trusted root to fail" >&2
    exit 1
fi
grep -q "symlinked trusted path" "$scratch/symlink.out"
