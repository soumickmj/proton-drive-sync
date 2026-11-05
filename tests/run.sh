#!/usr/bin/env bash
# Runs all tests against the fake CLI. No Proton account, network or FUSE needed.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
fails=0
for f in src/installer.sh install-deps.sh build.sh tests/test_sync.sh; do bash -n "$f"; done
"$PY" -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' src/pdsync.py
echo "== mode 1 (on-demand) logic"
"$PY" -I tests/test_mount_logic.py || fails=$((fails + 1))
echo "== mode 2 (single folder) end to end"
bash tests/test_sync.sh || fails=$((fails + 1))
[ $fails -eq 0 ] && echo "ALL TESTS PASSED" || { echo "$fails suite(s) failed"; exit 1; }
