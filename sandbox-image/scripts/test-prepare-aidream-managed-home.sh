#!/usr/bin/env bash
# Executable no-follow guard for the managed HOME creator.  It models the
# hosted root-owned sticky /run mount and separately forces unsafe incumbents.
set -euo pipefail

HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prepare-aidream-managed-home.py"
scratch="$(mktemp -d)"
scratch="$(cd "$scratch" && pwd -P)"
trap 'rm -rf "$scratch"' EXIT

helper_copy="$scratch/helper.py"
sed \
    -e "s|PARENT = \"/run\"|PARENT = \"$scratch/run\"|" \
    -e 's/metadata.st_uid != 0/metadata.st_uid != os.getuid()/g' \
    -e 's/os.fchown(child_fd, 0, 0)/os.fchown(child_fd, os.getuid(), os.getgid())/' \
    "$HELPER" > "$helper_copy"
mkdir "$scratch/run"
chmod 1777 "$scratch/run"

python3 "$helper_copy"
test "$(stat -f '%Lp' "$scratch/run/aidream-managed-home")" = 555

rm -rf "$scratch/run/aidream-managed-home"
mkdir "$scratch/run/aidream-managed-home"
if python3 "$helper_copy" >"$scratch/nonroot.out" 2>&1; then
    echo "expected existing non-root managed HOME to fail" >&2
    exit 1
fi
grep -q "belongs to a non-root user" "$scratch/nonroot.out"

rmdir "$scratch/run/aidream-managed-home"
ln -s "$scratch" "$scratch/run/aidream-managed-home"
if python3 "$helper_copy" >"$scratch/symlink.out" 2>&1; then
    echo "expected symlinked managed HOME to fail" >&2
    exit 1
fi
grep -q "is a symlink" "$scratch/symlink.out"
