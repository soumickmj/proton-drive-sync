# proton-drive-sync

**Proton Drive on Linux, OneDrive-style.** Your whole Proton Drive appears as a normal folder: files download when you open them, and every change you make is uploaded automatically. A simpler mode instead keeps one folder fully in sync. A status icon next to the clock shows what is being downloaded or uploaded.

It is built on the [official Proton Drive CLI](https://proton.me/support/drive-cli). It installs and runs as your normal user, inside your home folder.

> Unofficial project, not affiliated with or endorsed by Proton AG.
> Author: **Soumick Chatterjee, PhD** ([@soumickmj](https://github.com/soumickmj)) · MIT licence

## Quick start

```bash
curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/proton-drive-sync
curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/install-deps.sh
curl -fLO https://github.com/soumickmj/proton-drive-sync/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS            # must print OK twice
chmod +x proton-drive-sync install-deps.sh
./install-deps.sh                  # only if something is missing; may ask for admin rights
./proton-drive-sync                # the installer: run as your normal user, never with sudo
```

For other setups (no admin rights, servers or terminal only, building from source), see **[INSTALL.md](INSTALL.md)**.

## Two modes

| | **1. Full / on-demand** (like OneDrive) | **2. Single folder** |
|---|---|---|
| What you see | your entire Proton Drive | one folder, synced both ways with one Proton Drive folder |
| Disk use | only files you have opened (cache, 10 GB by default) | everything in the folder |
| Needs | FUSE 3 | nothing beyond Python |
| Admin rights | only if FUSE is not installed yet | never |

In both modes, new files, edits, new folders, renames, moves and deletes are synced automatically, and it starts again by itself after a reboot.

## What the installer does
1. Downloads the latest Proton Drive CLI for your platform (x64 or arm64, glibc or musl) from https://proton.me/download/drive/cli/ and **checks its SHA-512 checksum**. If the checksum doesn't match, it stops.
2. Asks for the mode, where to keep your login (system keyring, a private file, or `pass`) and the local folder.
3. Signs you in with `proton-drive auth login`, which shows a browser link you can open on any device.
4. Installs a systemd user service so syncing starts by itself. Without systemd it uses cron `@reboot` plus desktop autostart.
5. Adds the **status icon** if a desktop and PyGObject are present.
6. In mode 1, excludes the folder from KDE's Baloo file indexer.

Everything goes into `~/.local/bin`, `~/.local/share`, `~/.config`, `~/.cache` and `~/.local/state`, under `proton-drive-sync`.

## Status icon
- **Icon:** a cloud when everything is up to date, a download or upload arrow during transfers, and a warning icon on errors (for example when you need to log in again).
- **Tooltip and right-click menu:** current transfers with progress, how many changes are waiting to upload, and recent activity.
- **Click:** opens the folder.
- **Desktops:** works natively on KDE Plasma, XFCE, Cinnamon, LXQt and Budgie, and on Ubuntu's GNOME. Other GNOME setups need the *AppIndicator and KStatusNotifierItem Support* extension.

## Managing it
```
proton-drive-sync status | logs | start | stop | restart | login | logout
proton-drive-sync update | reset-state | uninstall | version
proton-drive-sync cli <args>    # the official CLI, using the same login
```

Settings are in `~/.config/proton-drive-sync/config.json`; run `proton-drive-sync restart` after changing them.

| Setting | Default | Meaning |
|---|---|---|
| `cache_max_gb` | 10 | mode 1: disk space for downloaded files (least recently used are removed first) |
| `list_ttl` | 30 | mode 1: seconds before a folder listing is refreshed in the background |
| `upload_delay` | 3 | mode 1: seconds to wait after a file is closed before uploading it |
| `local_interval` | 15 | mode 2: seconds between local checks |
| `remote_interval` | 120 | mode 2: seconds between Proton Drive checks |
| `no_download_procs` | thumbnailers and indexers | programs that must not trigger downloads |

## Safety and privacy
- **Deletes are recoverable.** Anything deleted on Proton Drive goes to the Proton trash. Files that mode 2 removes locally are moved to `~/.local/share/proton-drive-sync/local-trash/`.
- **Conflicts never lose data.** When a file changed on both sides, both versions are kept and yours is named `name (conflict <host> <date>).ext`.
- **Mode 2 guards against mass deletion.** It pauses if more than half of the known files suddenly disappear on one side.
- **Only Proton's official CLI talks to the network,** and only to Proton. There is no telemetry from this project; the CLI's telemetry follows the setting in your Proton account.
- **Only two third-party components,** both checked against a fixed hash:
  - Proton's CLI;
  - [mfusepy](https://github.com/mxmlnkn/mfusepy), a small libfuse binding with no network code (ISC licence).
- **Everything stays private to your account.**
  - The mount is visible only to you.
  - Caches and state are `0700`, cached files `0600`.
  - The CLI's debug log is made private and kept under 20 MB.
- **Login sessions can't clash.** Calls to the CLI are coordinated so two refreshes of the login session never run at the same time.

## Limitations
- Changes made elsewhere show up after a short polling delay (about 30 s in mode 1, 2 min in mode 2), not instantly.
- There are no per-file status marks inside the file manager and no right-click "free up space" or "always keep" options.
- Proton Docs and Sheets, and shared-with-me items, are not shown.
- Symlinks, hard links and permissions are not synced.
- Files that haven't been downloaded yet have no thumbnails.
- Opening a large file waits until the whole file has downloaded.
- With the keyring option, syncing starts after you log in. To start at boot before you log in, choose the file option.

## Development
```
src/installer.sh      installer and manager (bash)
src/pdsync.py         daemon: mount mode, sync mode and status icon (Python stdlib)
install-deps.sh       optional system-package installer (the only part that may need root)
build.sh              builds dist/proton-drive-sync (installer + embedded daemon) and SHA256SUMS
tests/run.sh          all tests, using a fake CLI (no account, network or FUSE needed)
```

To publish a release, set the version in `src/installer.sh` and `src/pdsync.py`, then push a tag such as `v1.0.0`. CI tests it and attaches the built files to a GitHub Release.

## Licence
MIT © 2026 Soumick Chatterjee, PhD. See [LICENSE](LICENSE).
