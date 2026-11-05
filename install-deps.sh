#!/usr/bin/env bash
# install-deps.sh — installs the system packages proton-drive-sync needs. This is the only
# step that may need administrator rights; the tool itself always installs and runs as a
# normal user. Only missing packages are installed.
#
#   ./install-deps.sh               full setup: mode 1 (on-demand) + mode 2 + status icon
#   ./install-deps.sh --sync-only   mode 2 only (no FUSE)
#   ./install-deps.sh --no-tray     skip the status-icon packages
#   ./install-deps.sh --check       only report what is missing (never needs admin rights)
#   ./install-deps.sh --print       print the install command instead of running it
#
# Admin rights can come from your own sudo, from another local account that has sudo
# (you type that account's password straight into su/sudo), or from an administrator
# who runs the printed command.
#
# Author: Soumick Chatterjee, PhD (https://github.com/soumickmj) — MIT licence.
set -euo pipefail

want_fuse=y want_tray=y action=install
for a in "$@"; do
    case "$a" in
        --sync-only) want_fuse=n ;;
        --no-tray) want_tray=n ;;
        --check) action=check ;;
        --print) action=print ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
miss() { printf '  %s✗%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
ask()  { local d=${2-}; read -r -p "$1${d:+ [$d]}: " REPLY </dev/tty || true; REPLY=${REPLY:-$d}; }

[ "$(uname -s)" = Linux ] || die "This tool is for Linux."

# --- package manager and per-distribution package names ------------------------------------
PM=
for m in apt-get dnf yum zypper pacman apk xbps-install; do
    if command -v "$m" >/dev/null 2>&1; then PM=$m; break; fi
done

pkgs_for() { # pkgs_for python|fuse|tray|notify
    case "$PM:$1" in
        apt-get:python) echo python3 ;;            apt-get:fuse) echo fuse3 ;;
        apt-get:tray) echo python3-gi gir1.2-glib-2.0 ;;  apt-get:notify) echo libnotify-bin ;;
        dnf:python|yum:python) echo python3 ;;      dnf:fuse|yum:fuse) echo fuse3 fuse3-libs ;;
        dnf:tray|yum:tray) echo python3-gobject-base ;;   dnf:notify|yum:notify) echo libnotify ;;
        zypper:python) echo python3 ;;              zypper:fuse) echo fuse3 libfuse3-3 ;;
        zypper:tray) echo python3-gobject ;;        zypper:notify) echo libnotify-tools ;;
        pacman:python) echo python ;;               pacman:fuse) echo fuse3 ;;
        pacman:tray) echo python-gobject ;;         pacman:notify) echo libnotify ;;
        apk:python) echo python3 ;;                 apk:fuse) echo fuse3 ;;
        apk:tray) echo py3-gobject3 ;;              apk:notify) echo libnotify ;;
        xbps-install:python) echo python3 ;;        xbps-install:fuse) echo fuse3 ;;
        xbps-install:tray) echo python3-gobject ;;  xbps-install:notify) echo libnotify ;;
    esac
}

install_cmd() { # install_cmd PKG...
    case "$PM" in
        apt-get) echo "apt-get update && apt-get install -y $*" ;;
        dnf|yum) echo "$PM install -y $*" ;;
        zypper) echo "zypper --non-interactive install $*" ;;
        pacman) echo "pacman -S --needed --noconfirm $*" ;;
        apk) echo "apk add $*" ;;
        xbps-install) echo "xbps-install -y $*" ;;
    esac
}

# --- what is missing --------------------------------------------------------------------
PY=
for p in "${PDS_PYTHON:-}" /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    if "$p" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY=$p; break; fi
done

missing=() notes=()
echo "${B}Checking dependencies${N}"
if [ -n "$PY" ]; then ok "Python $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])') ($PY)"
else miss "Python 3.9+ (required)"; missing+=(python); fi

if [ $want_fuse = y ]; then
    fm=$(command -v fusermount3 || command -v fusermount || true)
    lib=n
    if [ -n "$PY" ] && "$PY" -I -c 'import ctypes.util, sys
sys.exit(0 if ctypes.util.find_library("fuse3") or ctypes.util.find_library("fuse") else 1)' 2>/dev/null; then lib=y; fi
    if [ -n "$fm" ] && { [ $lib = y ] || [ -z "$PY" ]; }; then ok "FUSE ($fm) — for mode 1"
    else miss "FUSE 3 (needed for mode 1, on-demand files)"; missing+=(fuse); fi
    if [ ! -e /dev/fuse ]; then
        notes+=("/dev/fuse does not exist here (container or kernel without FUSE): mode 1 cannot work; use mode 2.")
    elif [ ! -r /dev/fuse ] || [ ! -w /dev/fuse ]; then
        if getent group fuse >/dev/null 2>&1; then missing+=(fusegroup); miss "access to /dev/fuse (join the 'fuse' group)"
        else notes+=("/dev/fuse is not accessible to you; ask an administrator.")
        fi
    fi
fi

if [ $want_tray = y ]; then
    if [ -n "$PY" ] && "$PY" -I -c 'import gi; gi.require_version("Gio", "2.0"); from gi.repository import Gio' 2>/dev/null
    then ok "PyGObject — for the status icon"
    else miss "PyGObject (optional: status icon next to the clock)"; missing+=(tray); fi
    if command -v notify-send >/dev/null 2>&1; then ok "notify-send — desktop notifications"
    else miss "notify-send (optional: desktop notifications)"; missing+=(notify); fi
fi
for n in ${notes[@]+"${notes[@]}"}; do printf '  %s!%s %s\n' "$Y" "$N" "$n"; done

if [ ${#missing[@]} -eq 0 ]; then
    echo "${G}All dependencies are present.${N} Next: ./proton-drive-sync"
    exit 0
fi
[ "$action" = check ] && { echo "Run ./install-deps.sh to install what is missing."; exit 1; }

# --- build the commands -----------------------------------------------------------------
pkgs=() cmds=()
for m in "${missing[@]}"; do
    [ "$m" = fusegroup ] && continue
    [ -n "$PM" ] || die "Unknown package manager. Please install: Python 3.9+, FUSE 3, PyGObject, libnotify."
    read -r -a p <<<"$(pkgs_for "$m")"
    pkgs+=("${p[@]}")
done
[ ${#pkgs[@]} -gt 0 ] && cmds+=("$(install_cmd "${pkgs[@]}")")
[[ " ${missing[*]} " == *" fusegroup "* ]] && cmds+=("usermod -aG fuse $(id -un)")
cmd=$(printf '%s && ' "${cmds[@]}"); cmd=${cmd% && }

echo ""
echo "${B}This needs administrator rights once:${N}"
echo "    $cmd"
if [ "$action" = print ]; then exit 0; fi

if [ "$(id -u)" -eq 0 ]; then
    sh -c "$cmd"
else
    can_sudo=n
    if command -v sudo >/dev/null 2>&1 && sudo -n -l >/dev/null 2>&1; then can_sudo=y; fi
    echo "  1) Use sudo with my account$([ $can_sudo = y ] && echo ' (looks available)')"
    echo "  2) Another account on this computer has sudo (you will type ITS password)"
    echo "  3) Just show the command; I will ask an administrator"
    ask "Choose" "$([ $can_sudo = y ] && echo 1 || echo 2)"
    case "$REPLY" in
        1) sudo sh -c "$cmd" ;;
        2) ask "Name of the account with sudo rights"
           admin=$REPLY
           id "$admin" >/dev/null 2>&1 || die "No such user: $admin"
           echo "Enter the password of '$admin' when 'su' asks, then again if 'sudo' asks."
           su - "$admin" -c "sudo sh -c '$cmd'" ;;
        *) echo "Ask an administrator to run, as root:"; echo "    $cmd"; exit 0 ;;
    esac
fi

echo ""
echo "${G}Done.${N}"
[[ " ${missing[*]} " == *" fusegroup "* ]] && echo "You were added to the 'fuse' group: log out and back in before installing."
echo "Next, as your normal user (not root): ./proton-drive-sync"
