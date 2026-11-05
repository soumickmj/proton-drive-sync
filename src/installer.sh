#!/usr/bin/env bash
# proton-drive-sync — installer and manager for Proton Drive on Linux.
#
# Built on the official Proton Drive CLI. Installs everything inside your home
# directory (no root needed unless system packages such as FUSE are missing).
#
#   ./proton-drive-sync            install / reconfigure
#   proton-drive-sync status|start|stop|restart|logs|login|logout|update|reset-state|cli ...|uninstall|version
#   proton-drive-sync pin|unpin|free PATH... | pins | cache | set cache-limit|expire-days|min-free VALUE
#
# Author: Soumick Chatterjee, PhD (https://github.com/soumickmj) — MIT licence.
# Unofficial; not affiliated with or endorsed by Proton AG.
set -euo pipefail
umask 077

APP=proton-drive-sync
VERSION=1.1.1
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/$APP"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP"
CONFIG_FILE="$CONFIG_DIR/config.json"
BIN_DIR="$HOME/.local/bin"
CLI="$DATA_DIR/bin/proton-drive"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"
TRAY_UNIT="$UNIT_DIR/$APP-tray.service"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
LOG_FILE="$STATE_DIR/daemon.log"

INDEX_URL="https://proton.me/download/drive/cli/index.html"
DOWNLOAD_PREFIX="https://proton.me/download/drive/cli/"
# mfusepy: ISC-licensed pure-Python libfuse binding (https://github.com/mxmlnkn/mfusepy), pinned by hash.
MFUSEPY_URL="https://files.pythonhosted.org/packages/c5/d5/56bc9326bf75f7ea0f4fe1178f5c9f20d1a1e15e288ecf66088513538ccd/mfusepy-3.1.1-py3-none-any.whl"
MFUSEPY_SHA256="69fb70cfc7f7cce595e6ff586f8451d8298f01f18286a157f24f564b24ec5a37"

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

if [ -t 1 ]; then B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'; else B= R= G= Y= N=; fi
say()  { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
ask()  { # ask "question" default -> REPLY
    local q=$1 d=${2-}
    if [ -n "$d" ]; then read -r -p "$q [$d]: " REPLY </dev/tty || true; REPLY=${REPLY:-$d}
    else read -r -p "$q: " REPLY </dev/tty || true; fi
}
confirm() { ask "$1 (y/n)" "${2:-y}"; case "$REPLY" in [yY]*) return 0;; *) return 1;; esac; }

# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------

pick_python() { # PDS_PYTHON=/path/to/python3 overrides (e.g. a user-installed Python, no admin rights)
    local p
    for p in "${PDS_PYTHON:-}" "$(cat "$CONFIG_DIR/python" 2>/dev/null || true)" \
             /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        [ -n "$p" ] && [ -x "$p" ] || continue
        if "$p" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PY=$p; return 0
        fi
    done
    return 1
}

fetch() { # fetch URL OUTFILE  (HTTPS only)
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only -O "$2" "$1"
    else
        "$PY" -I -c 'import sys, urllib.request; assert sys.argv[1].startswith("https://"); urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
    fi
}

hash_of() { # hash_of sha512|sha256 FILE
    "$PY" -I -c 'import hashlib, sys
h = hashlib.new(sys.argv[1])
with open(sys.argv[2], "rb") as f:
    for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
print(h.hexdigest())' "$1" "$2"
}

abspath() { "$PY" -I -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"; }

cfg_get() { "$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$CONFIG_FILE" "$1"; }

has_systemd_user() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

pkg_install_cmd() { # prints the package-manager command installing "$@" (generic names: fuse3 python3)
    local want=" $* " pkgs=
    if command -v apt-get >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "apt-get update && apt-get install -y$pkgs"
    elif command -v dnf >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3 fuse3-libs"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "dnf install -y$pkgs"
    elif command -v yum >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3 fuse3-libs"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "yum install -y$pkgs"
    elif command -v zypper >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3 libfuse3-3"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "zypper --non-interactive install$pkgs"
    elif command -v pacman >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3"; [[ $want == *" python3 "* ]] && pkgs+=" python"
        echo "pacman -S --needed --noconfirm$pkgs"
    elif command -v apk >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "apk add$pkgs"
    elif command -v xbps-install >/dev/null 2>&1; then
        [[ $want == *" fuse3 "* ]] && pkgs+=" fuse3"; [[ $want == *" python3 "* ]] && pkgs+=" python3"
        echo "xbps-install -y$pkgs"
    else
        return 1
    fi
}

# Runs a command as root. Supports: (1) current user has sudo, (2) another local account has
# sudo — you type that account's password into su/sudo directly, this script never sees it,
# (3) print the command for an administrator.
run_privileged() {
    local cmd=$1
    say ""
    say "${B}Administrator rights are needed once for:${N}"
    say "    $cmd"
    local can_sudo=n
    if command -v sudo >/dev/null 2>&1 && sudo -n -l >/dev/null 2>&1; then can_sudo=y; fi
    say "  1) Use sudo with my account$([ $can_sudo = y ] && echo ' (looks available)')"
    say "  2) Another account on this computer has sudo (you will type ITS password)"
    say "  3) Show the command; I will ask an administrator and re-run this installer"
    ask "Choose" "$([ $can_sudo = y ] && echo 1 || echo 2)"
    case "$REPLY" in
        1) sudo sh -c "$cmd" ;;
        2) ask "Name of the account with sudo rights"
           local admin=$REPLY
           id "$admin" >/dev/null 2>&1 || die "No such user: $admin"
           say "Enter the password of '$admin' when 'su' asks, then again if 'sudo' asks."
           su - "$admin" -c "sudo sh -c '$cmd'" ;;
        *) say ""; say "Ask an administrator to run, as root:"; say "    $cmd"
           say "Then run this installer again."; exit 0 ;;
    esac
}

# ------------------------------------------------------------------------------------------
# dependency checks
# ------------------------------------------------------------------------------------------

ensure_python() {
    pick_python && return 0
    warn "Python 3.9+ is required."
    local cmd; cmd=$(pkg_install_cmd python3) || die "Unknown package manager; please install Python 3.9+."
    run_privileged "$cmd"
    pick_python || die "Python 3.9+ still not found."
}

fuse_ok() {
    [ -e /dev/fuse ] && [ -r /dev/fuse ] && [ -w /dev/fuse ] || return 1
    local fm; fm=$(command -v fusermount3 || command -v fusermount || true)
    [ -n "$fm" ] || return 1
    "$PY" -I -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("fuse3") or ctypes.util.find_library("fuse") else 1)'
}

ensure_fuse() {
    fuse_ok && return 0
    if [ ! -e /dev/fuse ]; then
        die "/dev/fuse does not exist (container or kernel without FUSE). Mode 1 cannot work here; use mode 2."
    fi
    if [ -e /dev/fuse ] && { [ ! -r /dev/fuse ] || [ ! -w /dev/fuse ]; } && getent group fuse >/dev/null 2>&1; then
        run_privileged "usermod -aG fuse $(id -un)"
        die "Added you to the 'fuse' group. Log out and back in, then run this installer again."
    fi
    local cmd; cmd=$(pkg_install_cmd fuse3) || die "Unknown package manager; please install FUSE 3 (fuse3)."
    run_privileged "$cmd"
    fuse_ok || die "FUSE is still not usable. Check that /dev/fuse is accessible and fusermount3 is installed."
}

# ------------------------------------------------------------------------------------------
# downloads
# ------------------------------------------------------------------------------------------

platform() {
    local arch libc=
    case "$(uname -m)" in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) die "Unsupported CPU architecture $(uname -m). Proton ships the CLI for x86_64 and arm64 only." ;;
    esac
    if ls /lib/ld-musl-* >/dev/null 2>&1 || (ldd --version 2>&1 | grep -qi musl); then libc=-musl; fi
    echo "linux/$arch$libc"
}

download_cli() {
    local plat tmp row url sum got
    plat=$(platform)
    tmp=$(mktemp -d -p "$TMPROOT")
    info "Fetching the latest Proton Drive CLI release list"
    fetch "$INDEX_URL" "$tmp/index.html"
    row=$("$PY" -I - "$tmp/index.html" "$plat" <<'EOF'
import html.parser, sys
class P(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.cell = None; self.row = None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row = []
        elif tag == "td" and self.row is not None: self.cell = ""
    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None: self.row.append(self.cell.strip()); self.cell = None
        elif tag == "tr" and self.row: self.rows.append(self.row); self.row = None
    def handle_data(self, data):
        if self.cell is not None: self.cell += data
p = P(); p.feed(open(sys.argv[1], encoding="utf-8").read())
for r in p.rows:
    if len(r) >= 3 and r[0] == sys.argv[2]:
        print(r[1], r[2]); break
EOF
)
    [ -n "$row" ] || die "Could not find a $plat build on $INDEX_URL"
    url=${row% *}; sum=${row##* }
    case "$url" in "$DOWNLOAD_PREFIX"*) ;; *) die "Unexpected download URL: $url";; esac
    [[ $sum =~ ^[0-9a-f]{128}$ ]] || die "Unexpected checksum format on the release page."
    info "Downloading $url"
    fetch "$url" "$tmp/proton-drive"
    got=$(hash_of sha512 "$tmp/proton-drive")
    [ "$got" = "$sum" ] || die "SHA-512 checksum mismatch for the downloaded CLI. Aborting."
    info "Checksum verified (SHA-512)"
    mkdir -p "$DATA_DIR/bin"
    chmod 700 "$tmp/proton-drive"
    mv -f "$tmp/proton-drive" "$CLI"
    "$CLI" --version 2>/dev/null | head -1 || true
}

download_mfusepy() {
    [ -f "$DATA_DIR/mfusepy.py" ] && [ "$(cat "$DATA_DIR/.mfusepy.sha256" 2>/dev/null)" = "$MFUSEPY_SHA256" ] && return 0
    local tmp; tmp=$(mktemp -d -p "$TMPROOT")
    info "Downloading mfusepy (FUSE binding, ISC licence)"
    fetch "$MFUSEPY_URL" "$tmp/m.whl"
    [ "$(hash_of sha256 "$tmp/m.whl")" = "$MFUSEPY_SHA256" ] || die "Checksum mismatch for mfusepy. Aborting."
    "$PY" -I -c 'import sys, zipfile; open(sys.argv[2], "wb").write(zipfile.ZipFile(sys.argv[1]).read("mfusepy.py"))' \
        "$tmp/m.whl" "$DATA_DIR/mfusepy.py"
    echo "$MFUSEPY_SHA256" > "$DATA_DIR/.mfusepy.sha256"
}

extract_daemon() {
    local self=$1
    mkdir -p "$DATA_DIR"
    sed -n '/^__PDSYNC_PY_BELOW__$/,$p' "$self" | tail -n +2 > "$DATA_DIR/pdsync.py"
    [ -s "$DATA_DIR/pdsync.py" ] || die "Installer is incomplete (embedded daemon missing)."
    if [ "$(readlink -f "$self")" != "$(readlink -f "$BIN_DIR/$APP" 2>/dev/null || true)" ]; then
        mkdir -p "$BIN_DIR"
        cp -f "$self" "$BIN_DIR/$APP"
    fi
    chmod 700 "$BIN_DIR/$APP"
}

# ------------------------------------------------------------------------------------------
# service management
# ------------------------------------------------------------------------------------------

cli_env() { PROTON_DRIVE_CREDENTIALS_STORE="$(cfg_get credentials_store)" PROTON_DRIVE_LOG_LEVEL=WARNING "$CLI" "$@"; }

install_autostart() {
    local mode=$1 local_path=$2
    if has_systemd_user; then
        mkdir -p "$UNIT_DIR"
        {
            echo "[Unit]"
            echo "Description=Proton Drive ($mode mode)"
            echo "After=network-online.target"
            echo ""
            echo "[Service]"
            echo "Type=simple"
            echo "ExecStart=$PY -I $DATA_DIR/pdsync.py run"
            if [ "$mode" = mount ]; then
                echo "ExecStopPost=-$(command -v fusermount3 || command -v fusermount) -u -z \"$local_path\""
            else
                echo "NoNewPrivileges=yes"
            fi
            echo "Restart=always"
            echo "RestartSec=20"
            echo "TimeoutStopSec=120"
            echo "Environment=PYTHONUNBUFFERED=1"
            echo ""
            echo "[Install]"
            echo "WantedBy=default.target"
        } > "$UNIT"
        systemctl --user daemon-reload
        systemctl --user enable "$APP.service" >/dev/null
        systemctl --user restart "$APP.service"
        info "Installed systemd user service ($APP.service); it starts automatically when you log in."
        if [ "$(cfg_get credentials_store)" = unsafe_file ] && command -v loginctl >/dev/null 2>&1; then
            if confirm "Also start it at boot, before you log in (systemd 'linger')?" y; then
                loginctl enable-linger "$(id -un)" 2>/dev/null && info "Linger enabled." \
                    || run_privileged "loginctl enable-linger $(id -un)"
            fi
        fi
    else
        warn "systemd user services are not available; using cron @reboot and desktop autostart instead."
        local start_cmd="$PY -I $DATA_DIR/pdsync.py run >>$LOG_FILE 2>&1"
        if command -v crontab >/dev/null 2>&1; then
            ( crontab -l 2>/dev/null | grep -v "# $APP\$" || true
              echo "@reboot $start_cmd # $APP" ) | crontab -
            info "Added @reboot entry to your crontab."
        fi
        mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
        cat > "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Proton Drive sync
Exec=/bin/sh -c '$start_cmd'
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
        do_start
    fi
    if [ "$mode" = mount ]; then
        exclude_from_indexers "$local_path"
        install_file_manager_menus
    fi
    install_tray
}

# Right-click entries "Always keep on this device" / "Free up space" in Dolphin and Nautilus.
FM_SERVICEMENU="${XDG_DATA_HOME:-$HOME/.local/share}/kio/servicemenus/$APP.desktop"
FM_NAUTILUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus/scripts"
install_file_manager_menus() {
    mkdir -p "$(dirname "$FM_SERVICEMENU")"
    cat > "$FM_SERVICEMENU" <<EOF
[Desktop Entry]
Type=Service
MimeType=all/all;
X-KDE-Submenu=Proton Drive
Actions=keep;free;

[Desktop Action keep]
Name=Always keep on this device
Icon=folder-download
Exec=$BIN_DIR/$APP pin %F

[Desktop Action free]
Name=Free up space
Icon=edit-clear
Exec=$BIN_DIR/$APP free %F
EOF
    chmod 755 "$FM_SERVICEMENU"  # KDE only runs executable service menus
    if [ -d "${XDG_DATA_HOME:-$HOME/.local/share}/nautilus" ] || command -v nautilus >/dev/null 2>&1; then
        mkdir -p "$FM_NAUTILUS_DIR"
        local action label
        for action in pin free; do
            [ $action = pin ] && label="Proton Drive - Always keep on this device" || label="Proton Drive - Free up space"
            printf '#!/bin/sh\nset -f\nIFS="\n"\nexec "%s" %s $NAUTILUS_SCRIPT_SELECTED_FILE_PATHS\n' \
                "$BIN_DIR/$APP" "$action" > "$FM_NAUTILUS_DIR/$label"
            chmod 700 "$FM_NAUTILUS_DIR/$label"
        done
    fi
    info "Added 'Always keep on this device' and 'Free up space' to the file manager's right-click menu."
}

remove_file_manager_menus() {
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/kio/servicemenus/$APP.desktop" \
          "${XDG_DATA_HOME:-$HOME/.local/share}/nautilus/scripts/Proton Drive - Always keep on this device" \
          "${XDG_DATA_HOME:-$HOME/.local/share}/nautilus/scripts/Proton Drive - Free up space"
}

# KDE's Baloo indexer would otherwise crawl the whole mounted drive.
exclude_from_indexers() {
    local b
    for b in balooctl6 balooctl; do
        if command -v "$b" >/dev/null 2>&1; then
            "$b" config add excludeFolders "$1" >/dev/null 2>&1 && info "Excluded $1 from KDE file indexing (Baloo)."
            return 0
        fi
    done
}

# Status icon next to the clock (StatusNotifierItem). Needs a desktop session and PyGObject.
install_tray() {
    if ! "$PY" -I -c 'import gi; gi.require_version("Gio", "2.0"); from gi.repository import Gio' 2>/dev/null; then
        warn "Status icon skipped: it needs PyGObject (package python3-gobject or python3-gi)."
        warn "Install it, then run '$APP update'."
        return 0
    fi
    # Only desktops that actually activate graphical-session.target (GNOME, Plasma 6, ...) get a
    # systemd unit; others (XFCE, Cinnamon, MATE, ...) never start it, so use XDG autostart there.
    if has_systemd_user && systemctl --user is-active --quiet graphical-session.target; then
        mkdir -p "$UNIT_DIR"
        cat > "$TRAY_UNIT" <<EOF
[Unit]
Description=Proton Drive status icon
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=$PY -I $DATA_DIR/pdsync.py tray
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes

[Install]
WantedBy=graphical-session.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable "$APP-tray.service" >/dev/null 2>&1 || true
        systemctl --user restart "$APP-tray.service" || true
        rm -f "$AUTOSTART_DIR/$APP-tray.desktop"
    else
        if [ -f "$TRAY_UNIT" ]; then # left over from an install that picked the unit; it never starts here
            systemctl --user disable --now "$APP-tray.service" >/dev/null 2>&1 || true
            rm -f "$TRAY_UNIT"; systemctl --user daemon-reload 2>/dev/null || true
        fi
        mkdir -p "$AUTOSTART_DIR"
        cat > "$AUTOSTART_DIR/$APP-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Proton Drive status icon
Exec=$PY -I $DATA_DIR/pdsync.py tray
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
        if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
            pkill -u "$(id -u)" -f "$DATA_DIR/pdsync.py tray" 2>/dev/null || true
            nohup setsid "$PY" -I "$DATA_DIR/pdsync.py" tray >/dev/null 2>&1 </dev/null &
        fi
    fi
    info "Status icon installed next to the clock (on GNOME it needs the 'AppIndicator' extension)."
}

do_start() {
    if [ -f "$UNIT" ] && has_systemd_user; then
        systemctl --user start "$APP.service"
    else
        mkdir -p "$STATE_DIR"
        nohup setsid "$PY" -I "$DATA_DIR/pdsync.py" run >>"$LOG_FILE" 2>&1 </dev/null &
        info "Started (log: $LOG_FILE)"
    fi
    start_tray
}

# (Re)shows the status icon, e.g. after "Hide until next login".
start_tray() {
    if [ -f "$TRAY_UNIT" ] && has_systemd_user; then
        systemctl --user start "$APP-tray.service" 2>/dev/null || true
    elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP-tray.desktop" ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] &&
         ! pgrep -u "$(id -u)" -f "$DATA_DIR/pdsync.py tray" >/dev/null; then
        nohup setsid "$PY" -I "$DATA_DIR/pdsync.py" tray >/dev/null 2>&1 </dev/null &
    fi
}

do_stop() {
    if [ -f "$UNIT" ] && has_systemd_user; then systemctl --user stop "$APP.service"; return; fi
    pkill -TERM -u "$(id -u)" -f "$DATA_DIR/pdsync.py run" 2>/dev/null || true
    if [ "$(cfg_get mode 2>/dev/null)" = mount ]; then
        fusermount3 -u -z "$(cfg_get local_path)" 2>/dev/null || fusermount -u -z "$(cfg_get local_path)" 2>/dev/null || true
    fi
}

do_status() {
    [ -f "$CONFIG_FILE" ] || die "Not installed."
    say "Mode:        $(cfg_get mode)"
    say "Local path:  $(cfg_get local_path)"
    say "Remote path: $(cfg_get remote_path)"
    say "Login store: $(cfg_get credentials_store)"
    if [ -f "$UNIT" ] && has_systemd_user; then
        systemctl --user --no-pager status "$APP.service" | head -n 5 || true
    elif pgrep -u "$(id -u)" -f "$DATA_DIR/pdsync.py run" >/dev/null; then say "Daemon:      running"
    else say "Daemon:      stopped"; fi
    if [ "$(cfg_get mode)" = mount ]; then
        "$PY" -I -c 'import json, os, sys
idx = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
d = [p for p, e in idx.items() if e.get("dirty")]
print(f"Cached files: {len(idx)}   waiting to upload: {len(d)}")
for p in d[:20]: print("   ", p)' "$CACHE_DIR/index.json"
    fi
}

do_logs() {
    if [ -f "$UNIT" ] && has_systemd_user; then journalctl --user -u "$APP.service" -n 200 -f
    else tail -n 200 -f "$LOG_FILE"; fi
}

do_uninstall() {
    confirm "Remove $APP (your files on Proton Drive are not touched)?" n || exit 0
    if [ "$(cfg_get mode 2>/dev/null)" = mount ] && [ -f "$CACHE_DIR/index.json" ]; then
        local pending
        pending=$("$PY" -I -c 'import json, sys; print(sum(1 for e in json.load(open(sys.argv[1])).values() if e.get("dirty")))' "$CACHE_DIR/index.json")
        if [ "$pending" != 0 ]; then
            warn "$pending file(s) have not been uploaded yet (see: $APP status)."
            confirm "Uninstall anyway and lose those changes?" n || exit 1
        fi
    fi
    do_stop || true
    if [ -f "$UNIT" ]; then
        systemctl --user disable "$APP.service" >/dev/null 2>&1 || true
        rm -f "$UNIT"; systemctl --user daemon-reload 2>/dev/null || true
    fi
    if command -v crontab >/dev/null 2>&1; then
        ( crontab -l 2>/dev/null | grep -v "# $APP\$" || true ) | crontab - 2>/dev/null || true
    fi
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop"
    if [ -f "$TRAY_UNIT" ]; then
        systemctl --user disable --now "$APP-tray.service" >/dev/null 2>&1 || true
        rm -f "$TRAY_UNIT"; systemctl --user daemon-reload 2>/dev/null || true
    fi
    pkill -u "$(id -u)" -f "$DATA_DIR/pdsync.py tray" 2>/dev/null || true
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP-tray.desktop"
    remove_file_manager_menus
    local local_path; local_path=$(cfg_get local_path)
    if confirm "Sign out of Proton Drive (removes the stored session)?" y; then cli_env auth logout || true; fi
    rm -rf "$CACHE_DIR" "$STATE_DIR" "$CONFIG_DIR"
    [ -d "$DATA_DIR/local-trash" ] && say "Kept files removed by sync in: $DATA_DIR/local-trash"
    find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name local-trash -exec rm -rf {} +
    rm -f "$BIN_DIR/$APP"
    info "Uninstalled. Your local folder $local_path was left in place."
}

# ------------------------------------------------------------------------------------------
# install
# ------------------------------------------------------------------------------------------

detect_keyring() {
    command -v dbus-send >/dev/null 2>&1 || return 1
    dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.ListActivatableNames 2>/dev/null | grep -q org.freedesktop.secrets && return 0
    dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.ListNames 2>/dev/null | grep -q org.freedesktop.secrets
}

do_install() {
    local self; self=$(readlink -f "$0")
    [ "$(uname -s)" = Linux ] || die "This installer is for Linux."
    [ "$(id -u)" -ne 0 ] || die "Do not run this as root/sudo. Run it as the user who will use Proton Drive; it asks for admin rights only if needed."

    say "${B}Proton Drive for Linux — installer${N}"
    say "Everything is installed under your home directory."
    say ""
    ensure_python

    if [ -f "$CONFIG_FILE" ]; then
        warn "An existing installation was found ($(cfg_get mode) mode, $(cfg_get local_path))."
        confirm "Reconfigure it? The service will be stopped first" y || exit 0
        do_stop || true
    fi

    say ""
    say "${B}Choose a mode:${N}"
    say "  1) Full / on-demand: your whole Proton Drive appears in one folder. Files download when"
    say "     you open them; anything you add, edit, rename or delete is synced back. (needs FUSE)"
    say "  2) Single folder: one local folder is kept fully in sync (two-way) with one Proton Drive"
    say "     folder. All files are stored locally. (no admin rights ever needed)"
    ask "Mode" 1
    local mode
    case "$REPLY" in 1) mode=mount ;; 2) mode=sync ;; *) die "Please answer 1 or 2." ;; esac
    [ $mode = mount ] && ensure_fuse

    download_cli
    [ $mode = mount ] && download_mfusepy

    # where to store the login session
    say ""
    say "${B}Where should your Proton login session be stored?${N}"
    local def_store=2
    if detect_keyring; then def_store=1; fi
    say "  1) System keyring (GNOME Keyring / KWallet) — most secure; syncing starts after you log in"
    say "  2) Private file readable only by you (~/.cache/proton-drive-cli, mode 600) — needed for"
    say "     servers/headless use or to start at boot before login. Anyone with root or your account"
    say "     can read it; use full-disk encryption."
    say "  3) 'pass' password manager (GPG) — gpg-agent must be unlocked for syncing to work"
    ask "Choice" "$def_store"
    local store
    case "$REPLY" in 1) store=keychain ;; 2) store=unsafe_file ;; 3) store=pass ;; *) die "Invalid choice." ;; esac

    # folders
    say ""
    local local_path remote_path
    if [ $mode = mount ]; then
        ask "Local folder where Proton Drive should appear (must be empty or new)" "$HOME/ProtonDrive"
        local_path=$(abspath "$REPLY")
        if [ -d "$local_path" ] && [ -n "$(ls -A "$local_path" 2>/dev/null)" ]; then
            die "$local_path is not empty. Choose an empty or new folder."
        fi
        remote_path=/my-files
    else
        ask "Local folder to keep in sync" "$HOME/ProtonDrive"
        local_path=$(abspath "$REPLY")
        ask "Proton Drive folder to sync it with (created if missing)" "/my-files/$(basename "$local_path")"
        remote_path=${REPLY%/}
        case "$remote_path" in /my-files/?*) ;; *) die "The remote folder must be inside /my-files/, e.g. /my-files/Work";; esac
        if [ -d "$local_path" ] && [ -n "$(ls -A "$local_path" 2>/dev/null)" ]; then
            warn "$local_path already contains files. They will be merged with $remote_path:"
            warn "nothing is deleted on the first run; files that differ on both sides are kept as 'conflict' copies."
            confirm "Continue?" y || exit 1
        fi
    fi
    case "$local_path" in "$HOME"|/|"$HOME/.cache"*|"$DATA_DIR"*|"$CACHE_DIR"*) die "Refusing to use $local_path.";; esac
    mkdir -p "$local_path"
    chmod 700 "$local_path"

    mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$CACHE_DIR"
    chmod 700 "$CONFIG_DIR" "$STATE_DIR" "$CACHE_DIR"
    "$PY" -I -c 'import json, sys
cfg = {"mode": sys.argv[2], "local_path": sys.argv[3], "remote_path": sys.argv[4],
       "credentials_store": sys.argv[5], "binary": sys.argv[6]}
json.dump(cfg, open(sys.argv[1], "w"), indent=2)' "$CONFIG_FILE" "$mode" "$local_path" "$remote_path" "$store" "$CLI"
    printf '%s\n' "$PY" > "$CONFIG_DIR/python"  # remembered for later commands (matters with PDS_PYTHON)
    if [ -f "$STATE_DIR/sync-state.json" ] && [ $mode = sync ]; then
        rm -f "$STATE_DIR/sync-state.json"  # paths may have changed; a fresh merge is safe
    fi
    extract_daemon "$self"
    # The CLI keeps its own (encrypted) cache and, with option 2, the session file here.
    mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/proton-drive-cli"; chmod 700 "${XDG_CACHE_HOME:-$HOME/.cache}/proton-drive-cli"

    # login
    say ""
    if cli_env filesystem list / >/dev/null 2>&1; then
        info "Already signed in to Proton Drive."
    else
        info "Signing in. A browser link will be shown; you can open it on any device."
        cli_env auth login </dev/tty || die "Login failed. Run '$APP login' to try again."
        cli_env filesystem list / >/dev/null || die "Login did not work. Run '$APP login' to try again."
    fi

    install_autostart "$mode" "$local_path"

    say ""
    info "Done. Your Proton Drive is at: $local_path"
    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not in your PATH; use $BIN_DIR/$APP";; esac
    say "Manage it with:  $APP status | logs | stop | start | restart | login | update | uninstall"
}

# ------------------------------------------------------------------------------------------

main() {
    local cmd=${1:-install}; shift || true
    case "$cmd" in version|--version|-V) say "$APP $VERSION"; return 0 ;; -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; return 0 ;; esac
    if [ "$cmd" != install ]; then
        pick_python || die "Python 3.9+ not found."
        [ -f "$CONFIG_FILE" ] || die "Not installed yet. Run: $0 install"
    fi
    case "$cmd" in
        install) do_install ;;
        start) do_start ;;
        stop) do_stop ;;
        restart) do_stop; do_start ;;
        status) do_status ;;
        logs) do_logs ;;
        login) cli_env auth login </dev/tty; do_stop; do_start ;;
        logout) do_stop; cli_env auth logout ;;
        update) download_cli; extract_daemon "$(readlink -f "$0")"; do_stop; do_start
                [ "$(cfg_get mode)" = mount ] && install_file_manager_menus
                install_tray ;;
        pin|unpin|pins|free|cache|set) exec "$PY" -I "$DATA_DIR/pdsync.py" "$cmd" "$@" ;;
        reset-state)
            [ "$(cfg_get mode)" = sync ] || die "Only used in single-folder mode."
            say "This forgets what was synced before. The next run merges both sides again"
            say "(nothing is deleted; differing files become conflict copies)."
            confirm "Continue?" n || exit 0
            do_stop; rm -f "$STATE_DIR/sync-state.json"; do_start ;;
        cli) export PROTON_DRIVE_CREDENTIALS_STORE; PROTON_DRIVE_CREDENTIALS_STORE=$(cfg_get credentials_store)
             if command -v flock >/dev/null 2>&1; then exec flock "$STATE_DIR/cli.lock" flock "$STATE_DIR/cli-meta.lock" "$CLI" "$@"
             else exec "$CLI" "$@"; fi ;;
        uninstall) do_uninstall ;;
        *) die "Unknown command: $cmd (see --help)" ;;
    esac
}

main "$@"
exit 0
__PDSYNC_PY_BELOW__
