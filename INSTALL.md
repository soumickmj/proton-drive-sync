# Installing proton-drive-sync

proton-drive-sync installs and runs as your normal user, inside your home folder. The only thing that can ever need administrator rights is installing a few common **system packages**, and most desktop Linux systems already have them.

- [Requirements](#requirements)
- [A. Standard install](#a-standard-install)
- [B. Without any admin rights](#b-without-any-admin-rights)
- [C. Servers, SSH and terminal-only systems](#c-servers-ssh-and-terminal-only-systems)
- [D. Building from source](#d-building-from-source)
- [Updating and uninstalling](#updating-and-uninstalling)

## Requirements

| Needed for | Dependency | Usually already present on | Package names (apt · dnf · pacman · zypper · apk) |
|---|---|---|---|
| everything | Linux, bash, Python ≥ 3.9 | all mainstream distributions | `python3` · `python3` · `python` · `python3` · `python3` |
| mode 1 (on-demand) | FUSE 3 and `/dev/fuse` | Ubuntu, Fedora, Manjaro, openSUSE, Mint desktops | `fuse3` · `fuse3 fuse3-libs` · `fuse3` · `fuse3 libfuse3-3` · `fuse3` |
| status icon (optional) | PyGObject | most desktop installs | `python3-gi` · `python3-gobject-base` · `python-gobject` · `python3-gobject` · `py3-gobject3` |
| notifications (optional) | `notify-send` | most desktop installs | `libnotify-bin` · `libnotify` · `libnotify` · `libnotify-tools` · `libnotify` |
| downloading | `curl` or `wget` (Python is used as a fallback) | everywhere | |

Supported:
- **CPUs:** x86-64 and arm64, both glibc and musl (Alpine).
- **Desktops:** all of them. The status icon needs a tray that supports StatusNotifierItem; see the README.
- **Init systems:** systemd, or any system with cron.

To see what's missing without changing anything:

```bash
./install-deps.sh --check
```

## A. Standard install

1. Download the files from the [latest release](https://github.com/soumickmj/proton-drive-sync/releases/latest) and check them:
   ```bash
   curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/proton-drive-sync
   curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/install-deps.sh
   curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/SHA256SUMS
   sha256sum -c SHA256SUMS
   chmod +x proton-drive-sync install-deps.sh
   ```
2. Install the missing system packages, if any:
   ```bash
   ./install-deps.sh               # everything: mode 1, mode 2 and the status icon
   ./install-deps.sh --sync-only   # only what mode 2 needs (no FUSE)
   ./install-deps.sh --no-tray     # skip the status icon
   ```
   It lists what's missing, then offers three ways to get admin rights:
   - **Your own sudo.**
   - **Another account on this computer that has sudo.** You type *that* account's password straight into `su`/`sudo`; the script never sees it. Use this when someone else is the admin but you are the one using Proton Drive.
   - **Print the command** so you can give it to an administrator.
3. Run the installer **as yourself, not with sudo**:
   ```bash
   ./proton-drive-sync
   ```
   It asks for:
   - the mode;
   - where to keep your login: system keyring (recommended on desktops), a private file, or `pass`;
   - the local folder.

   It then shows a Proton sign-in link, which you can open on any device.

That's all. Syncing starts now and after every reboot. Check it with `proton-drive-sync status`.

## B. Without any admin rights

Everything proton-drive-sync installs goes into your home folder. Whether you need an administrator depends only on which system packages are already there. Run `./install-deps.sh --check` and follow the matching case:

**Python and FUSE are present** (typical desktop): run `./proton-drive-sync`. Both modes work, and no admin rights are needed.

**Python is present, FUSE is not:** choose **mode 2** (single folder); it never needs FUSE or admin rights. Mode 1 can't work without FUSE, because only an administrator can install the setuid helper `fusermount3`. Get it later with `./install-deps.sh --print` and an administrator, then re-run `./proton-drive-sync` to switch modes.

**No Python 3.9+ at all:** install a private Python into your home folder, then point the installer at it. One way is with [uv](https://docs.astral.sh/uv/), a standalone tool that needs no admin rights and leaves the system untouched:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs uv into ~/.local/bin
~/.local/bin/uv python install 3.12
PDS_PYTHON="$(~/.local/bin/uv python find 3.12)" ./proton-drive-sync
```

The installer remembers this Python for later commands and for the service. Note that uv and its Python builds are downloaded from GitHub. If you'd rather not use uv, any Python ≥ 3.9 you can run works: set `PDS_PYTHON=/path/to/python3`.

**No status icon without admin rights:** PyGObject comes from system packages and can't sensibly be installed per user. Everything else works without it; `proton-drive-sync status` shows the same information in the terminal.

**Starting at boot before you log in** (optional) uses systemd "linger": `loginctl enable-linger $USER`. Most systems allow this for your own account; if not, an administrator has to run it. Without it, syncing starts when you log in.

## C. Servers, SSH and terminal-only systems

- **Install** exactly as in A or B; nothing needs a desktop.
- **Login storage:** choose option **2, a private file**. A system keyring usually isn't available without a desktop session.
- **Sign-in:** the installer prints a link. Open it in a browser on any other device, such as your laptop or phone.
- **Start at boot:** the installer offers linger, so syncing starts at boot without anyone logging in.
- **Status icon:** skipped automatically. Use `proton-drive-sync status` and `proton-drive-sync logs`.
- **Containers:**
  - Mode 1 needs `/dev/fuse` passed in (for example `--device /dev/fuse --cap-add SYS_ADMIN`). Otherwise use mode 2.
  - Without systemd, cron `@reboot` is used. If there's no cron either, start it from your container entry point with `proton-drive-sync start`.

## D. Building from source

```bash
git clone https://github.com/soumickmj/proton-drive-sync.git
cd proton-drive-sync
bash tests/run.sh        # optional: all tests, using a fake CLI (no account or network needed)
./build.sh               # creates dist/proton-drive-sync, dist/install-deps.sh, dist/SHA256SUMS
./install-deps.sh        # if needed
./dist/proton-drive-sync
```

## Updating and uninstalling

- **Update the tool:** download the new release, check it, and run `./proton-drive-sync update`. This replaces the installed copy and also fetches the latest Proton CLI.
- **Update only Proton's CLI:** `proton-drive-sync update`.
- **Uninstall:** `proton-drive-sync uninstall`.
  - If any files haven't been uploaded yet, it warns you and asks before continuing.
  - It can sign you out.
  - It removes everything it installed. Your files on Proton Drive are never touched, and a mode 2 folder stays where it is.
- **Packages from `install-deps.sh`:** these are ordinary distribution packages that other software also uses. Remove them with your package manager if you want.
