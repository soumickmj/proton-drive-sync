#!/usr/bin/env bash
# End-to-end test of single-folder sync mode against the fake CLI.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d)
trap 'kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true; cp "$W/log" "${TEST_LOG:-/dev/null}"; rm -rf "$W"' EXIT
export HOME=$W/home XDG_CONFIG_HOME=$W/home/.config XDG_DATA_HOME=$W/home/.local/share \
       XDG_CACHE_HOME=$W/home/.cache XDG_STATE_HOME=$W/home/.local/state FAKE_REMOTE=$W/remote
R=$FAKE_REMOTE/my-files/Sync
L=$W/local
mkdir -p "$R/dir" "$L" "$XDG_CONFIG_HOME/proton-drive-sync" "$XDG_DATA_HOME/proton-drive-sync"
cp "$HERE/../src/pdsync.py" "$XDG_DATA_HOME/proton-drive-sync/"
printf '#!/bin/sh\nexec /usr/bin/python3 -I %s "$@"\n' "$HERE/fake_proton_drive.py" > "$W/pd"; chmod +x "$W/pd"
cat > "$XDG_CONFIG_HOME/proton-drive-sync/config.json" <<EOF
{"mode": "sync", "local_path": "$L", "remote_path": "/my-files/Sync", "credentials_store": "unsafe_file",
 "binary": "$W/pd", "local_interval": 1, "remote_interval": 2}
EOF
echo remote-a > "$R/a.txt"; echo remote-b > "$R/dir/b.txt"; echo local-c > "$L/c.txt"
touch -d '1 minute ago' "$L/c.txt"
/usr/bin/python3 -I "$XDG_DATA_HOME/proton-drive-sync/pdsync.py" run 2> "$W/log" & PID=$!
fails=0
check() { # check DESC CMD...
    local d=$1; shift
    for _ in $(seq 1 40); do if "$@" 2>/dev/null; then echo "PASS $d"; return; fi; sleep 0.5; done
    echo "FAIL $d"; fails=$((fails+1))
}
old() { touch -d '10 seconds ago' "$@"; }

check "remote file downloaded"         grep -qx remote-a "$L/a.txt"
check "remote nested file downloaded"  grep -qx remote-b "$L/dir/b.txt"
check "local file uploaded"            grep -qx local-c "$R/c.txt"

echo local-edit > "$L/a.txt"; old "$L/a.txt"
check "local edit uploaded"            grep -qx local-edit "$R/a.txt"

sleep 3; echo remote-edit > "$R/dir/b.txt"
check "remote edit downloaded"         grep -qx remote-edit "$L/dir/b.txt"

rm "$L/c.txt"
check "local delete -> remote trash"   test ! -e "$R/c.txt"
check "  ...and it is in Proton trash" sh -c "ls '$FAKE_REMOTE/.trash' | grep -q '^c.txt'"

mkdir -p "$L/newdir/sub"; echo n > "$L/newdir/sub/n.txt"; old "$L/newdir/sub/n.txt"
check "new local folder tree uploaded" grep -qx n "$R/newdir/sub/n.txt"

sleep 3; rm -rf "$R/dir"
check "remote folder delete -> local removed" test ! -e "$L/dir"
check "  ...kept in local trash"       sh -c "find '$XDG_DATA_HOME/proton-drive-sync/local-trash' -name b.txt | grep -q ."

sleep 3; kill -STOP $PID
echo both-local > "$L/a.txt"; old "$L/a.txt"; echo both-remote > "$R/a.txt"; touch -d '5 seconds ago' "$R/a.txt"
kill -CONT $PID
check "conflict: remote version wins the name" grep -qx both-remote "$L/a.txt"
conflict_copy() { find "$1" -maxdepth 1 -name 'a (conflict *).txt' -exec grep -lx both-local {} + | grep -q .; }
check "conflict: local copy kept locally"    conflict_copy "$L"
check "conflict: local copy uploaded"        conflict_copy "$R"

mv "$L/newdir/sub/n.txt" "$L/newdir/sub/renamed.txt"
check "local rename propagated"        sh -c "test -e '$R/newdir/sub/renamed.txt' && test ! -e '$R/newdir/sub/n.txt'"

echo ~lock > "$L/.~lock.x#"; echo swp > "$L/x.swp"; sleep 4
check "temp files ignored"             sh -c "test ! -e '$R/.~lock.x#' && test ! -e '$R/x.swp'"

# Mass-deletion guard: wipe the whole remote folder content (simulates an accident)
for i in $(seq 1 60); do echo $i > "$L/f$i"; done; touch -d '10 seconds ago' "$L"/f*
check "60 files uploaded"              test -e "$R/f60"
sleep 3; find "$R" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
sleep 6
check "guard: local files NOT deleted" test -e "$L/f60"
check "guard: logged"                  grep -q "sync paused" "$W/log"

# Edit on both sides while the daemon's remote listing is stale (long remote_interval):
# the remote edit must not be overwritten.
kill $PID; wait $PID 2>/dev/null || true
rm -rf "$R" "$L" "$XDG_STATE_HOME/proton-drive-sync/sync-state.json"; mkdir -p "$R" "$L"
sed -i 's/"remote_interval": 2/"remote_interval": 600/' "$XDG_CONFIG_HOME/proton-drive-sync/config.json"
echo base > "$R/s.txt"
/usr/bin/python3 -I "$XDG_DATA_HOME/proton-drive-sync/pdsync.py" run 2>> "$W/log" & PID=$!
check "stale: initial download"        grep -qx base "$L/s.txt"
sleep 3
echo remote-new > "$R/s.txt"; touch -d '5 seconds ago' "$R/s.txt"
echo local-new > "$L/s.txt"; old "$L/s.txt"
# done when either a conflict copy exists or the remote edit was overwritten
stale_done() { grep -qx local-new "$R/s.txt" || find "$R" -maxdepth 1 -name 's (conflict *).txt' | grep -q .; }
check "stale: daemon acted"            stale_done
check "stale: remote edit not overwritten" grep -qx remote-new "$R/s.txt"
check "stale: local edit kept as conflict copy" \
    sh -c "find '$R' -maxdepth 1 -name 's (conflict *).txt' -exec grep -lx local-new {} + | grep -q ."

echo "---- log tail"; tail -n 5 "$W/log"
grep -E "Traceback|crashed" "$W/log" && { echo "FAIL exceptions in log"; fails=$((fails+1)); }
echo "failures: $fails"; exit $fails
