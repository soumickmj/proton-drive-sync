#!/usr/bin/env bash
# Assembles the single-file executable: installer.sh followed by the embedded daemon,
# plus a SHA256SUMS file for the release.
#
# Author: Soumick Chatterjee, PhD (https://github.com/soumickmj) — MIT licence.
set -euo pipefail
cd "$(dirname "$0")"

v_sh=$(sed -n 's/^VERSION=//p' src/installer.sh)
v_py=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/pdsync.py)
[ "$v_sh" = "$v_py" ] || { echo "version mismatch: installer.sh=$v_sh pdsync.py=$v_py" >&2; exit 1; }

mkdir -p dist
cat src/installer.sh src/pdsync.py > dist/proton-drive-sync
chmod 755 dist/proton-drive-sync
cp install-deps.sh dist/install-deps.sh
(cd dist && sha256sum proton-drive-sync install-deps.sh > SHA256SUMS)
echo "built dist/proton-drive-sync $v_sh"
