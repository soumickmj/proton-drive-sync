# proton-drive-sync

**Proton Drive on Linux with files on demand.** Your whole Proton Drive appears as a normal folder: files download when you open them, and every change you make is uploaded automatically. A simpler mode instead keeps one folder fully in sync. A status icon next to the clock shows what is being downloaded or uploaded.

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

| | **1. Full / on-demand** | **2. Single folder** |
|---|---|---|
| What you see | your entire Proton Drive | one folder, synced both ways with one Proton Drive folder |
| Disk use | files you opened, added or chose to keep (see [Storage](#storage-on-this-device-mode-1)) | everything in the folder |
| Needs | FUSE 3 | nothing beyond Python |
| Admin rights | only if FUSE is not installed yet | never |

In both modes, new files, edits, new folders, renames, moves and deletes are synced automatically, and it starts again by itself after a reboot.

## What the installer does
1. Downloads the latest Proton Drive CLI for your platform (x64 or arm64, glibc or musl) from https://proton.me/download/drive/cli/ and **checks its SHA-512 checksum**. If the checksum doesn't match, it stops.
2. Asks for the mode, where to keep your login (system keyring, a private file, or `pass`) and the local folder.
3. Signs you in with `proton-drive auth login`, which shows a browser link you can open on any device.
4. Installs a systemd user service so syncing starts by itself. Without systemd it uses cron `@reboot` plus desktop autostart.
5. Adds the **status icon** if a desktop and PyGObject are present.
6. In mode 1, excludes the folder from KDE's Baloo file indexer and adds **Proton Drive → Always keep on this device / Free up space** to the right-click menu of Dolphin, Nautilus and Thunar.

Everything goes into `~/.local/bin`, `~/.local/share`, `~/.config`, `~/.cache` and `~/.local/state`, under `proton-drive-sync`.

## Storage on this device (mode 1)

In mode 1, every file you open, copy in, or create is stored in a local cache (`~/.cache/proton-drive-sync`), and its changes are uploaded from there. What stays on disk is controlled by these rules:

| Rule | Default | What it does |
|---|---|---|
| **Space for downloaded files** | 10 GB | When downloads use more, those not opened for the longest time are removed first. |
| **Remove downloads not opened for** | 30 days | Downloads you haven't opened for this long are removed, even below the limit. |
| **Always keep on this device** | nothing | Chosen files and folders are downloaded in the background and kept up to date. They never count against the limit, never expire, and new files you add inside them are kept too. |
| **Keep free on disk** | 2 GB | Downloads are removed (oldest first) to keep this much space free on the disk. If that isn't enough, opening a not-yet-downloaded file fails with "No space left on device" instead of filling the disk. |

A removed download only frees disk space: the file stays visible, stays on Proton Drive, and downloads again when you open it.

**What is never removed:**
- files that are open;
- files with changes that haven't been uploaded yet, so copying in more than the limit is fine;
- anything you chose to always keep.

**Where to change it:**
- **Status icon menu:** *Space for downloaded files*, *Remove downloads not opened for*, *Always keep on this device*, *Free up space now*.
- **File manager:** right-click → *Proton Drive* → *Always keep on this device* or *Free up space*.
- **Terminal:**
  ```
  proton-drive-sync pin ~/ProtonDrive/Work          # always keep on this device
  proton-drive-sync unpin ~/ProtonDrive/Work        # back to normal (removed when unused)
  proton-drive-sync free ~/ProtonDrive/Photos       # remove downloaded copies now
  proton-drive-sync pins                            # list what is always kept
  proton-drive-sync cache                           # disk use and settings
  proton-drive-sync set cache-limit 50              # GB, or "none"
  proton-drive-sync set expire-days 90              # days, or "never"
  proton-drive-sync set min-free 5                  # GB
  ```

Changes take effect within seconds, without a restart. In mode 2 every file is always on the device, so these rules don't apply.

## Status icon
- **Icon:** its own purple cloud, the same with any icon theme: a check mark when everything is up to date, a down or up arrow during transfers, circling arrows while syncing, red with **!** on errors (for example when you need to log in again) and grey when the service isn't running.
- **Tooltip and right-click menu:** current transfers with progress, how many changes are waiting to upload, and recent activity.
- **Storage settings in the right-click menu** (mode 1): see [Storage on this device](#storage-on-this-device-mode-1).
- **Click:** opens the folder.
- **Hiding it:** *Hide until next login* in its menu hides it; syncing continues. `proton-drive-sync start` shows it again.
- **Desktops:** works natively on KDE Plasma, XFCE, Cinnamon, LXQt and Budgie, and on Ubuntu's GNOME. Other GNOME setups need the *AppIndicator and KStatusNotifierItem Support* extension.

## Managing it
```
proton-drive-sync status | logs | start | stop | restart | login | logout
proton-drive-sync update | reset-state | uninstall | version
proton-drive-sync pin | unpin | free PATH... | pins | cache | set NAME VALUE   # mode 1 storage
proton-drive-sync cli <args>    # the official CLI, using the same login
```

Settings are in `~/.config/proton-drive-sync/config.json`. The storage settings apply within seconds; for the others, run `proton-drive-sync restart` after changing them.

| Setting | Default | Meaning |
|---|---|---|
| `cache_max_gb` | 10 | mode 1: space for downloaded files, 0 = no limit |
| `cache_expire_days` | 30 | mode 1: remove downloads not opened for this many days, 0 = never |
| `min_free_gb` | 2 | mode 1: always leave this much free on the disk |
| `pinned` | `[]` | mode 1: paths always kept on this device, such as `"/Work"` |
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
- There are no per-file status marks (such as "downloaded" or "kept") inside the file manager. Use `proton-drive-sync cache` and `pins`, or the status icon.
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
