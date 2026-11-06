#!/usr/bin/env python3
"""proton-drive-sync daemon.

Wraps the official Proton Drive CLI (`proton-drive`) and provides either:
  * mount mode: a FUSE filesystem showing the whole drive, downloading files
    on first access ("files on demand") and uploading every change, or
  * sync mode: two-way synchronisation of one local folder with one remote folder.

Only the Python standard library is used, plus the vendored `mfusepy` module
(ISC licence, pure ctypes binding to libfuse) for mount mode and, optionally,
PyGObject for the status icon. No data is sent anywhere except through the
official Proton CLI to Proton's servers.

Author: Soumick Chatterjee, PhD (https://github.com/soumickmj) — MIT licence.
Unofficial; not affiliated with or endorsed by Proton AG.
"""

__version__ = "1.1.2"

import errno
import fcntl
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

APP_NAME = "proton-drive-sync"
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), APP_NAME)
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), APP_NAME)
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), APP_NAME)
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

sys.path.insert(0, DATA_DIR)  # vendored mfusepy lives next to this script

log = logging.getLogger(APP_NAME)

# Files that are never synchronised (editor swap files, partial downloads, ...).
IGNORE_PREFIXES = (".~lock.", ".goutputstream-", ".pdsync-")
IGNORE_SUFFIXES = (".swp", ".swx", ".part", ".crdownload", ".tmp~", "~")
PROTON_NATIVE_TYPES = ("application/vnd.proton.doc", "application/vnd.proton.sheet")
# Processes that must not trigger downloads in mount mode (thumbnailers, indexers).
DEFAULT_NO_DOWNLOAD_PROCS = [
    "tumblerd", "tumbler", "gnome-thumbnail", "gdk-pixbuf-thum", "ffmpegthumbnai",
    "totem-video-thu", "evince-thumbnai", "gnome-desktop-t", "tracker-extract", "tracker-miner-f",
    "localsearch-ext", "localsearch", "baloo_file", "baloo_file_extr", "updatedb", "plocate",
]
# Same, matched anywhere in the caller's command line (KDE runs thumbnailers inside generic kioworkers).
DEFAULT_NO_DOWNLOAD_CMDLINE = ["/thumbnail.so"]
# Threads whose directory listings are background work (Dolphin's folder item counter).
BACKGROUND_THREADS = ("KDirectoryConte",)
# Request priorities for CLI calls: lower runs first.
PRIO_UI, PRIO_LOW, PRIO_BG = 0, 1, 2
_tl = threading.local()


def current_prio():
    return getattr(_tl, "prio", PRIO_UI)


def caller_info(tid):
    """(process name, thread name, command line) of the thread with id `tid`."""
    def read(p):
        try:
            with open(p, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""
    thread = read(f"/proc/{tid}/comm").strip()
    tgid = tid
    for line in read(f"/proc/{tid}/status").splitlines():
        if line.startswith("Tgid:"):
            tgid = line.split()[1]
            break
    return read(f"/proc/{tgid}/comm").strip(), thread, read(f"/proc/{tgid}/cmdline").replace("\0", " ")


def ignored(name):
    return name.startswith(IGNORE_PREFIXES) or name.endswith(IGNORE_SUFFIXES)


def load_config():
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    cfg.setdefault("list_ttl", 30)
    cfg.setdefault("local_interval", 15)
    cfg.setdefault("remote_interval", 120)
    cfg.setdefault("upload_delay", 3)
    cfg.setdefault("no_download_procs", DEFAULT_NO_DOWNLOAD_PROCS)
    cfg.setdefault("no_download_cmdline", DEFAULT_NO_DOWNLOAD_CMDLINE)
    return with_storage_defaults(cfg)


def with_storage_defaults(cfg):
    """Mode-1 storage settings; they can be changed while running (tray menu, CLI)."""
    cfg.setdefault("cache_max_gb", 10)       # 0 = no limit (pinned files never count)
    cfg.setdefault("cache_expire_days", 30)  # 0 = never remove unused downloads by age
    cfg.setdefault("min_free_gb", 2)         # always leave this much free on the disk
    cfg.setdefault("pinned", [])             # mount paths ("/Work/Data") always kept on this device
    return cfg


def update_config(change):
    """Applies change(cfg) to config.json atomically; safe against concurrent writers."""
    with open(CONFIG_FILE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        change(cfg)
        atomic_write_json(CONFIG_FILE, cfg)
        return cfg


def under(path, parent):
    return path == parent or parent == "/" or path.startswith(parent.rstrip("/") + "/")


def request_free(paths):
    """Asks the running daemon to remove the downloaded copies of these mount paths."""
    d = os.path.join(STATE_DIR, "requests")
    os.makedirs(d, mode=0o700, exist_ok=True)
    atomic_write_json(os.path.join(d, uuid.uuid4().hex + ".json"), {"op": "free", "paths": list(paths)})


def parse_time(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def escape_name(name):
    """Escape a single node name for use in a CLI remote path."""
    return name.replace("/", "\\/")


def rjoin(parent, name):
    return parent.rstrip("/") + "/" + escape_name(name)


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def notify(summary, body=""):
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "Proton Drive", summary, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# Proton Drive CLI wrapper
# --------------------------------------------------------------------------------------

class DriveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def not_found(self):
        m = self.message.lower()
        return "not found" in m or "does not exist" in m or "no such" in m

    @property
    def auth(self):
        m = self.message.lower()
        return "login" in m or "unauthori" in m or "session" in m and "expired" in m


class Node:
    __slots__ = ("name", "uid", "is_dir", "size", "mtime", "rev", "sha1")

    def __init__(self, name, uid, is_dir, size, mtime, rev, sha1=None):
        self.name, self.uid, self.is_dir, self.size, self.mtime, self.rev = name, uid, is_dir, size, mtime, rev
        self.sha1 = sha1


class PrioLock:
    """Mutex handing the lock to the waiter with the lowest priority number (FIFO within a priority)."""

    def __init__(self):
        self.cv = threading.Condition()
        self.busy = False
        self.waiting = []
        self.seq = 0

    def acquire(self, prio):
        import heapq
        with self.cv:
            self.seq += 1
            ticket = (prio, self.seq)
            heapq.heappush(self.waiting, ticket)
            while self.busy or self.waiting[0] != ticket:
                self.cv.wait()
            heapq.heappop(self.waiting)
            self.busy = True

    def release(self):
        with self.cv:
            self.busy = False
            self.cv.notify_all()


class Status:
    """Publishes what the daemon is doing to status.json, read by the tray icon."""

    def __init__(self, cfg):
        from collections import deque
        self.path = os.path.join(STATE_DIR, "status.json")
        self.mode, self.root = cfg["mode"], cfg["local_path"]
        self.mu = threading.Lock()
        self.active = {}
        self.recent = deque(maxlen=15)
        self.error = None
        self.pending_fn = lambda: 0
        self.cache = None  # mode 1 storage use, set by the mount
        self.last = None
        self.seq = 0
        threading.Thread(target=self.writer, daemon=True).start()

    def begin(self, op, path, size=None, probe=None):
        with self.mu:
            self.seq += 1
            self.active[self.seq] = {"op": op, "path": path, "size": size, "probe": probe, "started": time.time()}
            return self.seq

    def end(self, key, ok=True, msg=None):
        with self.mu:
            a = self.active.pop(key, None)
            if a:
                self.recent.appendleft({"t": time.time(), "op": a["op"], "path": a["path"], "ok": ok, "msg": msg})

    def event(self, op, path, ok=True, msg=None):
        with self.mu:
            self.recent.appendleft({"t": time.time(), "op": op, "path": path, "ok": ok, "msg": msg})

    def track(self, op, path, size=None, probe=None):
        status = self

        class Tracker:
            def __enter__(self):
                self.key = status.begin(op, path, size, probe)

            def __exit__(self, et, ev, tb):
                msg = getattr(ev, "message", None) or (str(ev) if ev else None)
                status.end(self.key, ok=et is None, msg=msg and msg[-300:])
        return Tracker()

    def set_error(self, msg):
        with self.mu:
            self.error = msg

    @staticmethod
    def probe_size(d):
        total = 0
        try:
            for e in os.scandir(d):
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
        except OSError:
            pass
        return total

    def snapshot(self):
        with self.mu:
            active = [{"op": a["op"], "path": a["path"], "size": a["size"],
                       "done": self.probe_size(a["probe"]) if a["probe"] else None}
                      for a in self.active.values()]
            recent = list(self.recent)
            error = self.error
        try:
            pending = self.pending_fn()
        except Exception:
            pending = 0
        state = "error" if error else "busy" if active or pending else "idle"
        return {"mode": self.mode, "root": self.root, "state": state, "active": active,
                "pending_uploads": pending, "recent": recent, "error": error, "cache": self.cache}

    def writer(self):
        while True:
            try:
                snap = self.snapshot()
                if snap != self.last:
                    atomic_write_json(self.path, snap)
                    self.last = snap
            except Exception as ex:
                log.debug("status write failed: %s", ex)
            time.sleep(1)


class _NoStatus:
    def track(self, *a, **k):
        import contextlib
        return contextlib.nullcontext()

    def __getattr__(self, name):
        return lambda *a, **k: None


def tidy_cli_log():
    """The CLI's own debug log is created world-readable and grows without limit; keep it private and small."""
    d = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "proton-drive-cli")
    p = os.path.join(d, "proton-drive.log")
    try:
        os.chmod(d, 0o700)
        os.chmod(p, 0o600)
        if os.path.getsize(p) > 20 * 1024 ** 2:
            os.truncate(p, 0)
    except OSError:
        pass


class Drive:
    """Access to the official CLI.

    Calls go through two lanes: "meta" (listings, renames, ...) and "xfer" (uploads,
    downloads), each one call at a time, ordered by request priority, so browsing is not
    stuck behind a long transfer. Lanes only overlap while the login session was used
    successfully moments ago, i.e. when no session refresh is due; otherwise (and to retry a
    failed overlapping call) both lanes are taken, as concurrent CLI processes could race on
    refreshing the login session.
    """
    FRESH = 120

    def __init__(self, cfg, status=None):
        self.bin = cfg["binary"]
        self.env = dict(os.environ)
        self.env["PROTON_DRIVE_CREDENTIALS_STORE"] = cfg["credentials_store"]
        self.env.setdefault("PROTON_DRIVE_LOG_LEVEL", "WARNING")
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        self.lanes = {"xfer": (PrioLock(), os.path.join(STATE_DIR, "cli.lock")),
                      "meta": (PrioLock(), os.path.join(STATE_DIR, "cli-meta.lock"))}
        self.last_ok = 0.0
        self.calls = 0
        self.auth_failed = False
        self.status = status or _NoStatus()
        tidy_cli_log()

    def _call(self, args, timeout, lanes):
        prio = current_prio()
        held = []
        try:
            for name in ("xfer", "meta"):  # fixed order: no deadlock
                if name in lanes:
                    lock, path = self.lanes[name]
                    lock.acquire(prio)
                    f = open(path, "w")
                    held.append((lock, f))
                    fcntl.flock(f, fcntl.LOCK_EX)
            return subprocess.run([self.bin, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, env=self.env, timeout=timeout)
        finally:
            for lock, f in reversed(held):
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
                lock.release()

    def run(self, *args, timeout=None, lane="meta"):
        both = {"xfer", "meta"}
        overlap = time.time() - self.last_ok < self.FRESH
        p = self._call(args, timeout, {lane} if overlap else both)
        err = None
        if p.returncode != 0:
            err = DriveError(p.returncode, (p.stderr.strip() or p.stdout.strip() or "unknown error")[-2000:])
            if err.auth and overlap:
                p = self._call(args, timeout, both)
                err = None if p.returncode == 0 else \
                    DriveError(p.returncode, (p.stderr.strip() or p.stdout.strip() or "unknown error")[-2000:])
        self.calls += 1
        if self.calls % 200 == 0:
            tidy_cli_log()
        if err:
            if err.auth and not self.auth_failed:
                self.auth_failed = True
                log.error("Proton session invalid. Run: proton-drive-sync login")
                notify("Proton Drive: login needed", "Run 'proton-drive-sync login' in a terminal.")
                self.status.set_error("Login needed: run 'proton-drive-sync login' in a terminal")
            raise err
        if self.auth_failed:
            self.status.set_error(None)
        self.auth_failed = False
        self.last_ok = time.time()
        return p.stdout

    def list(self, rpath):
        out = self.run("filesystem", "list", "--json", rpath, timeout=300)
        start = out.find("[")
        items = json.loads(out[start:]) if start >= 0 else []
        nodes = {}
        for n in items:
            name = n.get("name") or {}
            if not name.get("ok") or n.get("type") not in ("file", "folder"):
                continue
            if n.get("trashTime") or n.get("mediaType") in PROTON_NATIVE_TYPES:
                continue
            nm = name["value"].replace("/", "∕")  # '/' cannot appear in local names
            rev = n.get("activeRevision") or {}
            nodes[nm] = Node(
                name=nm, uid=n["uid"], is_dir=n["type"] == "folder",
                size=int(rev.get("claimedSize") or rev.get("storageSize") or 0),
                mtime=parse_time(rev.get("claimedModificationTime") or n.get("modificationTime")),
                rev=rev.get("uid"),
                sha1=(rev.get("claimedDigests") or {}).get("sha1"),
            )
        return nodes

    def download(self, rpath, local_dir):
        self.run("filesystem", "download", "-f", "remove", "-d", "merge", rpath, local_dir, lane="xfer")

    def upload(self, local_path, rparent, strategy="create-new-revision"):
        self.run("filesystem", "upload", "-f", strategy, "-d", "merge", local_path, rparent, lane="xfer")

    def mkdir(self, rparent, name):
        self.run("filesystem", "create-folder", rparent, name)

    def trash(self, rpath):
        self.run("filesystem", "trash", rpath)

    def rename(self, rpath, new_name):
        self.run("filesystem", "rename", rpath, new_name)

    def move(self, rpath, rparent):
        self.run("filesystem", "move", rpath, rparent)


def local_to_remote_name(name):
    return name.replace("∕", "/")


def conflict_name(name):
    base, ext = os.path.splitext(name)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    return f"{base} (conflict {socket.gethostname()} {stamp}){ext}"


def stage_for_upload(src, name):
    """Hard-link (or copy) `src` into a private staging dir under the remote `name`."""
    stage = tempfile.mkdtemp(prefix="up-", dir=os.path.join(CACHE_DIR, "staging"))
    dst = os.path.join(stage, local_to_remote_name(name))
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return stage, dst


# --------------------------------------------------------------------------------------
# Mount mode (files on demand)
# --------------------------------------------------------------------------------------

def run_mount(cfg):
    import mfusepy as fuse  # vendored

    from collections import deque
    status = Status(cfg)
    drive = Drive(cfg, status)
    remote_root = cfg["remote_path"].rstrip("/") or "/my-files"
    mountpoint = cfg["local_path"]
    files_dir = os.path.join(CACHE_DIR, "files")
    index_path = os.path.join(CACHE_DIR, "index.json")
    dirs_path = os.path.join(CACHE_DIR, "dirs.json")
    for d in (files_dir, os.path.join(CACHE_DIR, "staging")):
        os.makedirs(d, mode=0o700, exist_ok=True)
    shutil.rmtree(os.path.join(CACHE_DIR, "staging"), ignore_errors=True)
    os.makedirs(os.path.join(CACHE_DIR, "staging"), mode=0o700, exist_ok=True)
    blocked_procs = tuple(cfg["no_download_procs"])
    blocked_cmdline = tuple(cfg.get("no_download_cmdline", DEFAULT_NO_DOWNLOAD_CMDLINE))
    uid, gid = os.getuid(), os.getgid()

    class Ops(fuse.Operations):
        use_ns = False

        def __init__(self):
            self.mu = threading.RLock()
            self.dirs = {}         # local path ("/a/b") -> (fetched_at, {name: Node})
            # Listings from the last run are shown at once (as stale) and refreshed in the background.
            try:
                with open(dirs_path) as f:
                    for p, nodes in json.load(f).items():
                        self.dirs[p] = (0.0, {n[0]: Node(*n) for n in nodes})
            except (OSError, ValueError, TypeError):
                self.dirs = {}
            self.dirs_dirty = False
            self.refreshing = set()
            self.bgq = deque()
            self.bg_cv = threading.Condition(self.mu)
            self.inflight = {}     # path -> {"ev": Event, "nodes"/"err"}
            # index: local path -> {"cid", "rev", "dirty", "base_rev", "atime"}
            try:
                with open(index_path) as f:
                    self.index = json.load(f)
            except (OSError, ValueError):
                self.index = {}
            self.handles = {}      # fh -> [fd, path, written]
            self.open_count = {}   # path -> number of open handles
            self.path_locks = {}
            self.upload_due = {}   # path -> time
            self.cv = threading.Condition(self.mu)
            for p, e in self.index.items():
                if e.get("dirty"):
                    self.upload_due[p] = time.time()
            threading.Thread(target=self.upload_worker, daemon=True).start()
            threading.Thread(target=self.bg_worker, daemon=True).start()
            threading.Thread(target=self.dirs_saver, daemon=True).start()
            status.pending_fn = lambda: len(self.upload_due)
            self.settings = with_storage_defaults(dict(cfg))
            self.own_revs = {}  # path -> revision we last uploaded there
            self.cfg_stamp = self.config_stamp()
            self.pin_wake = threading.Event()
            self.pin_problem = None
            threading.Thread(target=self.maintenance, daemon=True).start()
            threading.Thread(target=self.pin_worker, daemon=True).start()

        # ---------- helpers ----------
        def rpath(self, path):
            if path == "/":
                return remote_root
            parts = [escape_name(local_to_remote_name(p)) for p in path.strip("/").split("/")]
            return remote_root + "/" + "/".join(parts)

        def cfile(self, entry):
            return os.path.join(files_dir, entry["cid"])

        def save_index(self):
            atomic_write_json(index_path, self.index)

        def path_lock(self, path):
            with self.mu:
                return self.path_locks.setdefault(path, threading.Lock())

        def caller(self):
            try:
                return caller_info(fuse.fuse_get_context()[2])
            except Exception:
                return "", "", ""

        def caller_blocked(self, who=None):
            proc, thread, cmdline = who or self.caller()
            return proc.startswith(blocked_procs) or thread.startswith(blocked_procs) or \
                any(s in cmdline for s in blocked_cmdline)

        def caller_prio(self):
            who = self.caller()
            if self.caller_blocked(who):
                return PRIO_BG  # indexers crawling the tree
            if who[1].startswith(BACKGROUND_THREADS):
                return PRIO_LOW
            return PRIO_UI

        def fetch(self, path):
            nodes = drive.list(self.rpath(path))
            with self.mu:
                self.dirs[path] = (time.time(), nodes)
                self.dirs_dirty = True
                # drop clean cached copies whose remote revision changed
                prefix = path.rstrip("/") + "/"
                for name, node in nodes.items():
                    e = self.index.get(prefix + name)
                    if e and not e.get("dirty") and e.get("rev") and e["rev"] != node.rev \
                            and not self.open_count.get(prefix + name):
                        try:
                            os.unlink(self.cfile(e))
                        except OSError:
                            pass
                        del self.index[prefix + name]
            return nodes

        def bg_worker(self):
            """Refreshes stale folders; when idle, walks the whole drive once so every folder opens instantly."""
            _tl.prio = PRIO_BG
            crawl, crawled = deque(["/"]), set()
            while True:
                with self.mu:
                    while not self.bgq and not crawl:
                        self.bg_cv.wait()
                    if self.bgq:
                        path, crawling = self.bgq.popleft(), False
                    else:
                        path, crawling = crawl.popleft(), True
                        if path in crawled:
                            continue
                        crawled.add(path)
                        cached = self.dirs.get(path)
                        fresh = cached is not None and cached[0] > 0  # already listed during this run
                if crawling:
                    try:
                        nodes = cached[1] if fresh else self.fetch(path)
                    except Exception as ex:
                        log.debug("crawl of %s failed: %s", path, ex)
                        continue
                    prefix = path.rstrip("/") + "/"
                    crawl.extend(prefix + n.name for n in nodes.values() if n.is_dir)
                    if not fresh:
                        time.sleep(0.2)  # leave gaps for interactive requests
                    continue
                try:
                    self.fetch(path)
                except DriveError as ex:
                    if ex.not_found:
                        self.invalidate(path)
                    else:
                        log.warning("refresh of %s failed: %s", path, ex.message)
                except Exception as ex:
                    log.warning("refresh of %s failed: %s", path, ex)
                finally:
                    with self.mu:
                        self.refreshing.discard(path)

        def dirs_saver(self):
            while True:
                time.sleep(15)
                self.save_dirs()

        def save_dirs(self):
            with self.mu:
                if not self.dirs_dirty:
                    return
                data = {p: [[n.name, n.uid, n.is_dir, n.size, n.mtime, n.rev, n.sha1] for n in nodes.values()]
                        for p, (_, nodes) in self.dirs.items()}
                self.dirs_dirty = False
            try:
                atomic_write_json(dirs_path, data)
            except OSError as ex:
                log.warning("saving folder cache failed: %s", ex)

        def listing(self, path):
            with self.mu:
                cached = self.dirs.get(path)
                if cached and time.time() - cached[0] > cfg["list_ttl"] and path not in self.refreshing:
                    self.refreshing.add(path)
                    self.bgq.append(path)
                    self.bg_cv.notify()
                if cached:
                    return cached[1]
                # one CLI call per folder, however many callers ask at the same time
                job = self.inflight.get(path)
                owner = job is None
                if owner:
                    job = self.inflight[path] = {"ev": threading.Event()}
            if owner:
                try:
                    _tl.prio = self.caller_prio()
                    job["nodes"] = self.fetch(path)
                except Exception as ex:
                    job["err"] = ex if isinstance(ex, DriveError) else DriveError(1, f"internal error: {ex!r}")
                finally:
                    _tl.prio = PRIO_UI
                    with self.mu:
                        self.inflight.pop(path, None)
                    job["ev"].set()
            else:
                job["ev"].wait()
            ex = job.get("err")
            if ex is None:
                return job["nodes"]
            if ex.not_found:
                raise fuse.FuseOSError(errno.ENOENT)
            log.error("list %s: %s", path, ex.message)
            raise fuse.FuseOSError(errno.EIO)

        def lookup(self, path):
            """Returns (node or None, index entry or None)."""
            if path == "/":
                return Node("", "", True, 0, time.time(), None), None
            parent, name = os.path.split(path)
            nodes = self.listing(parent)
            with self.mu:
                return nodes.get(name), self.index.get(path)

        def invalidate(self, path):
            with self.mu:
                for p in [p for p in self.dirs if p == path or p.startswith(path.rstrip("/") + "/")]:
                    del self.dirs[p]
                self.dirs_dirty = True

        def is_local_only(self, entry):
            return entry is not None and entry.get("pending")

        # ---------- metadata ----------
        def getattr(self, path, fh=None):
            node, entry = self.lookup(path)
            if entry is not None and (entry.get("dirty") or entry.get("pending")):
                st = os.lstat(self.cfile(entry))
                return dict(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=st.st_size, st_uid=uid,
                            st_gid=gid, st_mtime=st.st_mtime, st_atime=st.st_mtime, st_ctime=st.st_mtime,
                            st_blocks=st.st_blocks)
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            if node.is_dir:
                return dict(st_mode=stat.S_IFDIR | 0o700, st_nlink=2, st_uid=uid, st_gid=gid,
                            st_mtime=node.mtime, st_atime=node.mtime, st_ctime=node.mtime, st_size=4096)
            # not-yet-downloaded files report 0 blocks, so du and file managers count only cached data
            size, blocks = node.size, 0
            if entry is not None:
                try:
                    st = os.lstat(self.cfile(entry))
                    size, blocks = st.st_size, st.st_blocks
                except OSError:
                    pass
            return dict(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=size, st_uid=uid, st_gid=gid,
                        st_mtime=node.mtime, st_atime=node.mtime, st_ctime=node.mtime, st_blocks=blocks)

        def readdir(self, path, fh=0):
            nodes = self.listing(path)
            names = set(nodes)
            prefix = path.rstrip("/") + "/"
            with self.mu:
                for p, e in self.index.items():
                    if e.get("pending") and p.startswith(prefix) and "/" not in p[len(prefix):]:
                        names.add(p[len(prefix):])
            return [".", ".."] + sorted(names)

        def statfs(self, path):
            bs = 4096
            total = 1 << 40
            return dict(f_bsize=bs, f_frsize=bs, f_blocks=total // bs, f_bfree=total // bs,
                        f_bavail=total // bs, f_files=1 << 20, f_ffree=1 << 20, f_namemax=255)

        def chmod(self, path, mode):
            return 0

        def chown(self, path, uid_, gid_):
            return 0

        def utimens(self, path, times=None):
            node, entry = self.lookup(path)
            if entry is not None and (entry.get("dirty") or entry.get("pending")):
                os.utime(self.cfile(entry), times)
            return 0

        # ---------- directories ----------
        def mkdir(self, path, mode):
            parent, name = os.path.split(path)
            if ignored(name):
                raise fuse.FuseOSError(errno.EPERM)
            try:
                drive.mkdir(self.rpath(parent), local_to_remote_name(name))
            except DriveError as ex:
                log.error("mkdir %s: %s", path, ex.message)
                raise fuse.FuseOSError(errno.EIO)
            self.fetch(parent)
            with self.mu:
                self.dirs[path] = (time.time(), {})
            status.event("mkdir", path)
            return 0

        def rmdir(self, path):
            if self.readdir(path) != [".", ".."]:
                raise fuse.FuseOSError(errno.ENOTEMPTY)
            try:
                drive.trash(self.rpath(path))
            except DriveError as ex:
                log.error("rmdir %s: %s", path, ex.message)
                raise fuse.FuseOSError(errno.EIO)
            self.invalidate(path)
            self.fetch(os.path.dirname(path))
            status.event("trash", path)
            self.update_pins(path)
            return 0

        # ---------- files ----------
        def ensure_local(self, path, node, entry, need_content=True, who=None):
            """Make sure a cache file exists for `path`; download when needed."""
            if entry is not None and os.path.exists(self.cfile(entry)):
                if entry.get("dirty") or entry.get("pending") or node is None or entry.get("rev") == node.rev:
                    return entry
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            cid = uuid.uuid4().hex
            target = os.path.join(files_dir, cid)
            if need_content:
                who = who or self.caller()
                if self.caller_blocked(who):
                    raise fuse.FuseOSError(errno.EACCES)
                if not self.make_room(node.size):
                    log.error("not enough disk space to download %s (%s)", path, human_size(node.size))
                    status.event("download", path, ok=False, msg="not enough disk space")
                    raise fuse.FuseOSError(errno.ENOSPC)
                tmpdir = tempfile.mkdtemp(prefix="dl-", dir=os.path.join(CACHE_DIR, "staging"))
                try:
                    log.info("downloading %s (opened by %s)", path, who[0] or "?")
                    with status.track("download", path, node.size, tmpdir):
                        drive.download(self.rpath(path), tmpdir)
                    got = os.listdir(tmpdir)
                    if len(got) != 1:
                        raise DriveError(1, f"unexpected download result {got}")
                    os.replace(os.path.join(tmpdir, got[0]), target)
                except DriveError as ex:
                    log.error("download %s: %s", path, ex.message)
                    raise fuse.FuseOSError(errno.EIO)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                os.utime(target, (node.mtime, node.mtime))
            else:
                open(target, "wb").close()
            os.chmod(target, 0o600)
            with self.mu:
                old = self.index.get(path)
                if old:
                    try:
                        os.unlink(self.cfile(old))
                    except OSError:
                        pass
                entry = {"cid": cid, "rev": node.rev, "base_rev": node.rev, "dirty": False, "atime": time.time()}
                self.index[path] = entry
                self.save_index()
            self.evict()
            return entry

        # ---------- storage: limit, expiry, free disk space, pinned files ----------
        def is_pinned(self, path):
            return any(under(path, p) for p in self.settings["pinned"])

        def disk_free(self):
            st = os.statvfs(files_dir)
            return st.f_bavail * st.f_frsize

        def evict(self, need=0, free_paths=()):
            """Removes downloaded copies that are safe to remove (uploaded, closed, not pinned):
            those under `free_paths`, those unused for longer than the expiry, then least recently
            used ones while over the size limit or while the disk is short of space.
            Returns True when `need` more bytes fit on the disk."""
            st = self.settings
            limit = int(float(st["cache_max_gb"]) * 1024 ** 3)
            expire = float(st["cache_expire_days"]) * 86400
            min_free = int(float(st["min_free_gb"]) * 1024 ** 3)
            now = time.time()
            with self.mu:
                items, used, pinned = [], 0, 0
                for p, e in self.index.items():
                    try:
                        size = os.lstat(self.cfile(e)).st_blocks * 512
                    except OSError:
                        continue
                    if self.is_pinned(p):
                        pinned += size
                        continue
                    used += size
                    if not e.get("dirty") and not e.get("pending") and not self.open_count.get(p):
                        items.append((e.get("atime", 0), p, size))
                items.sort()
                free = self.disk_free()
                changed = False
                for atime, p, size in items:
                    if not (any(under(p, f) for f in free_paths) or (expire and now - atime > expire)
                            or (limit and used > limit) or free - need < min_free):
                        if not free_paths:
                            break  # sorted by last use: nothing older is left to remove
                        continue
                    try:
                        os.unlink(self.cfile(self.index[p]))
                    except OSError:
                        pass
                    del self.index[p]
                    used -= size
                    free += size
                    changed = True
                if changed:
                    self.save_index()
                status.cache = {"used": used, "pinned": pinned, "limit": limit, "free": free,
                                "expire_days": st["cache_expire_days"], "pins": len(st["pinned"]),
                                "problem": self.pin_problem}
                return free - need >= min_free

        def make_room(self, size):
            return self.evict(need=size or 0)

        def config_stamp(self):
            # mtime alone misses a second write in the same clock tick (a few ms on ext4)
            try:
                st = os.stat(CONFIG_FILE)
                return st.st_mtime_ns, st.st_ino, st.st_size
            except OSError:
                return None

        def reload_settings(self):
            try:
                with open(CONFIG_FILE) as f:
                    new = with_storage_defaults(json.load(f))
            except (OSError, ValueError):
                return
            pins_changed = new["pinned"] != self.settings["pinned"]
            self.settings = {k: new[k] for k in ("cache_max_gb", "cache_expire_days", "min_free_gb", "pinned")}
            log.info("storage settings: limit %s GB, remove unused after %s days, %d kept on this device",
                     new["cache_max_gb"] or "no", new["cache_expire_days"] or "never", len(new["pinned"]))
            if pins_changed:
                self.pin_wake.set()
            self.evict()

        def handle_requests(self):
            d = os.path.join(STATE_DIR, "requests")
            try:
                names = sorted(os.listdir(d))
            except OSError:
                return
            for n in names:
                p = os.path.join(d, n)
                try:
                    with open(p) as f:
                        req = json.load(f)
                except (OSError, ValueError):
                    req = {}
                try:
                    os.unlink(p)
                except OSError:
                    pass
                if req.get("op") == "free":
                    before = (status.cache or {}).get("used", 0)
                    self.evict(free_paths=tuple(req.get("paths") or ()))
                    freed = before - (status.cache or {}).get("used", 0)
                    log.info("freed %s for %s", human_size(max(0, freed)), ", ".join(req.get("paths") or []))
                    status.event("free", ", ".join(req.get("paths") or []), msg=human_size(max(0, freed)))

        def maintenance(self):
            last_sweep = 0
            while True:
                time.sleep(2)
                try:
                    stamp = self.config_stamp()
                    if stamp != self.cfg_stamp:
                        self.cfg_stamp = stamp
                        self.reload_settings()
                    self.handle_requests()
                    if time.time() - last_sweep > 60:
                        last_sweep = time.time()
                        self.evict()
                except Exception:
                    log.exception("storage maintenance failed")

        def pin_worker(self):
            """Downloads everything that is pinned and keeps it up to date."""
            _tl.prio = PRIO_BG
            who = (APP_NAME, "keep-on-device", "")
            while True:
                self.pin_wake.wait(timeout=300)
                self.pin_wake.clear()
                problem = None
                for pin in list(self.settings["pinned"]):
                    todo = [pin]
                    while todo:
                        if self.pin_wake.is_set():
                            break  # pins changed: start over
                        path = todo.pop()
                        parent, name = os.path.split(path)
                        try:
                            if path == "/":
                                node = Node("", "", True, 0, 0, None)
                            else:
                                with self.mu:
                                    cached = self.dirs.get(parent)
                                nodes = cached[1] if cached and time.time() - cached[0] < cfg["list_ttl"] \
                                    else self.fetch(parent)
                                node = nodes.get(name)
                            if node is None:
                                continue
                            if node.is_dir:
                                prefix = path.rstrip("/") + "/"
                                todo.extend(prefix + n for n in self.fetch(path))
                                continue
                            with self.path_lock(path):
                                with self.mu:
                                    entry = self.index.get(path)
                                    busy = self.open_count.get(path) or (entry and entry.get("dirty"))
                                if not busy:
                                    self.ensure_local(path, node, entry, who=who)
                        except fuse.FuseOSError as ex:
                            if ex.errno == errno.ENOSPC:
                                problem = "Not enough disk space to keep all chosen files on this device"
                                todo.clear()
                        except Exception as ex:
                            log.warning("keeping %s on this device failed: %s", path, getattr(ex, "message", ex))
                if problem != self.pin_problem:
                    if problem:
                        status.set_error(problem)
                    elif status.error == self.pin_problem:
                        status.set_error(None)
                    self.pin_problem = problem
                self.evict()

        def update_pins(self, old, new=None):
            """A pinned item (or its parent) was renamed to `new`, or deleted when `new` is None."""
            if not any(under(p, old) for p in self.settings["pinned"]):
                return
            def change(c):
                pins = []
                for p in c.get("pinned", []):
                    if under(p, old):
                        if new is None:
                            continue
                        p = new + p[len(old):]
                    pins.append(p)
                c["pinned"] = pins
            try:
                self.settings["pinned"] = update_config(change)["pinned"]
            except (OSError, ValueError) as ex:
                log.warning("could not update kept-on-device list: %s", ex)

        def _open(self, path, flags, entry):
            fd = os.open(self.cfile(entry), flags & ~(os.O_CREAT | os.O_EXCL))
            with self.mu:
                fh = fd
                self.handles[fh] = [fd, path, False]
                self.open_count[path] = self.open_count.get(path, 0) + 1
                entry["atime"] = time.time()
            return fh

        def open(self, path, flags):
            node, entry = self.lookup(path)
            if node is not None and node.is_dir:
                raise fuse.FuseOSError(errno.EISDIR)
            truncating = bool(flags & os.O_TRUNC) and (flags & os.O_ACCMODE) != os.O_RDONLY
            with self.path_lock(path):
                entry = self.ensure_local(path, node, entry, need_content=not truncating)
                fh = self._open(path, flags, entry)
            if truncating:
                os.ftruncate(fh, 0)
                self.mark_written(fh)
            return fh

        def create(self, path, mode, fi=None):
            parent, name = os.path.split(path)
            self.listing(parent)
            cid = uuid.uuid4().hex
            fd = os.open(os.path.join(files_dir, cid), os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
            os.close(fd)
            with self.mu:
                node = self.dirs.get(parent, (0, {}))[1].get(name)
                self.index[path] = {"cid": cid, "rev": None, "base_rev": node.rev if node else None,
                                    "dirty": True, "pending": node is None, "atime": time.time()}
                self.save_index()
                entry = self.index[path]
            fh = self._open(path, os.O_RDWR, entry)
            self.mark_written(fh)
            return fh

        def mark_written(self, fh):
            with self.mu:
                h = self.handles.get(fh)
                if h and not h[2]:
                    h[2] = True
                    e = self.index.get(h[1])
                    if e is not None and not e.get("dirty"):
                        e["dirty"] = True
                        self.save_index()

        def read(self, path, size, offset, fh):
            return os.pread(fh, size, offset)

        def write(self, path, data, offset, fh):
            self.mark_written(fh)
            return os.pwrite(fh, data, offset)

        def truncate(self, path, length, fh=None):
            if fh is not None and fh in self.handles:
                os.ftruncate(fh, length)
                self.mark_written(fh)
                return 0
            node, entry = self.lookup(path)
            with self.path_lock(path):
                entry = self.ensure_local(path, node, entry, need_content=length > 0)
                os.truncate(self.cfile(entry), length)
                with self.mu:
                    entry["dirty"] = True
                    self.save_index()
                    self.schedule_upload(path)
            return 0

        def flush(self, path, fh):
            return 0

        def fsync(self, path, datasync, fh):
            os.fsync(fh)
            return 0

        def release(self, path, fh):
            with self.mu:
                h = self.handles.pop(fh, None)
                if h is None:
                    return 0
                os.close(h[0])
                p = h[1]
                self.open_count[p] -= 1
                if not self.open_count[p]:
                    del self.open_count[p]
                if h[2]:
                    self.schedule_upload(p)
            return 0

        def schedule_upload(self, path):
            with self.mu:
                self.upload_due[path] = time.time() + cfg["upload_delay"]
                self.cv.notify()

        def unlink(self, path):
            node, entry = self.lookup(path)
            if node is None and not self.is_local_only(entry):
                raise fuse.FuseOSError(errno.ENOENT)
            if node is not None:
                try:
                    drive.trash(self.rpath(path))
                except DriveError as ex:
                    log.error("trash %s: %s", path, ex.message)
                    raise fuse.FuseOSError(errno.EIO)
            with self.mu:
                e = self.index.pop(path, None)
                self.upload_due.pop(path, None)
                if e:
                    try:
                        os.unlink(self.cfile(e))
                    except OSError:
                        pass
                self.save_index()
                parent, name = os.path.split(path)
                if parent in self.dirs:
                    self.dirs[parent][1].pop(name, None)
                    self.dirs_dirty = True
            if node is not None:
                status.event("trash", path)
                self.update_pins(path)
            return 0

        def rename(self, old, new, flags=0):
            onode, oentry = self.lookup(old)
            nnode, nentry = self.lookup(new)
            if onode is None and not self.is_local_only(oentry):
                raise fuse.FuseOSError(errno.ENOENT)
            oparent, oname = os.path.split(old)
            nparent, nname = os.path.split(new)

            # Local-only file (e.g. an editor's temp file): just move it in the index.
            # Renaming it over an existing remote file turns it into a new revision of that file.
            if onode is None:
                with self.mu:
                    if nentry is not None and nentry is not oentry:
                        try:
                            os.unlink(self.cfile(nentry))
                        except OSError:
                            pass
                    e = self.index.pop(old)
                    self.upload_due.pop(old, None)
                    e["pending"] = nnode is None
                    e["base_rev"] = nnode.rev if nnode else None
                    e["dirty"] = True
                    self.index[new] = e
                    for h in self.handles.values():
                        if h[1] == old:
                            h[1] = new
                    if old in self.open_count:
                        self.open_count[new] = self.open_count.pop(old)
                    self.save_index()
                    self.schedule_upload(new)
                return 0

            try:
                if nnode is not None:
                    if nnode.is_dir and self.readdir(new) != [".", ".."]:
                        raise fuse.FuseOSError(errno.ENOTEMPTY)
                    drive.trash(self.rpath(new))
                    self.fetch(nparent)
                rold = self.rpath(old)
                if oparent != nparent:
                    drive.move(rold, self.rpath(nparent))
                    rold = rjoin(self.rpath(nparent), local_to_remote_name(oname))
                if oname != nname:
                    drive.rename(rold, local_to_remote_name(nname))
            except DriveError as ex:
                log.error("rename %s -> %s: %s", old, new, ex.message)
                self.invalidate(oparent)
                self.invalidate(nparent)
                raise fuse.FuseOSError(errno.EIO)
            with self.mu:
                moved = {}
                for p in list(self.index):
                    if p == old or p.startswith(old + "/"):
                        moved[new + p[len(old):]] = self.index.pop(p)
                if nentry is not None and new not in moved:
                    try:
                        os.unlink(self.cfile(nentry))
                    except OSError:
                        pass
                    self.index.pop(new, None)
                self.index.update(moved)
                for p in list(self.upload_due):
                    if p == old or p.startswith(old + "/"):
                        self.upload_due[new + p[len(old):]] = self.upload_due.pop(p)
                if old in self.open_count:
                    self.open_count[new] = self.open_count.pop(old)
                for p in list(self.dirs):
                    if p == old or p.startswith(old + "/"):
                        del self.dirs[p]
                self.dirs_dirty = True
                for h in self.handles.values():
                    if h[1] == old:
                        h[1] = new
                self.save_index()
            self.fetch(oparent)
            if nparent != oparent:
                self.fetch(nparent)
            status.event("move" if oparent != nparent else "rename", new)
            self.update_pins(old, new)
            return 0

        def symlink(self, target, source):
            raise fuse.FuseOSError(errno.ENOTSUP)

        def link(self, target, source):
            raise fuse.FuseOSError(errno.ENOTSUP)

        # ---------- uploads ----------
        def upload_worker(self):
            _tl.prio = PRIO_LOW
            while True:
                with self.mu:
                    now = time.time()
                    ready = [p for p, t in self.upload_due.items() if t <= now and not self.open_count.get(p)]
                    if not ready:
                        waiting = [t for p, t in self.upload_due.items() if not self.open_count.get(p)]
                        wait = min(waiting + [now + 30]) - now
                        if len(waiting) < len(self.upload_due):
                            wait = min(wait, 2)  # some files are still open; check again soon
                        self.cv.wait(timeout=max(0.5, wait))
                        continue
                    path = ready[0]
                    entry = self.index.get(path)
                    del self.upload_due[path]
                if entry is None or not entry.get("dirty"):
                    continue
                parent, name = os.path.split(path)
                if ignored(name):
                    continue
                try:
                    self.do_upload(path, parent, name, entry)
                except Exception as ex:
                    log.error("upload %s failed, retrying in 60s: %s", path, getattr(ex, "message", ex))
                    with self.mu:
                        if path in self.index:
                            self.upload_due[path] = time.time() + 60

        def do_upload(self, path, parent, name, entry):
            with self.path_lock(path):
                nodes = self.fetch(parent)
                remote = nodes.get(name)
                target_name = name
                strategy = "create-new-revision"
                if remote is not None and remote.is_dir:
                    target_name, strategy = conflict_name(name), "rename"
                elif remote is not None and remote.rev not in (entry.get("base_rev"), self.own_revs.get(path)):
                    target_name, strategy = conflict_name(name), "rename"
                    log.warning("conflict on %s: remote changed meanwhile, uploading as %s", path, target_name)
                    notify("Proton Drive: conflict", f"Your copy was saved as {target_name}")
                stage, staged = stage_for_upload(self.cfile(entry), target_name)
                mtime_before = os.lstat(self.cfile(entry)).st_mtime_ns
                try:
                    log.info("uploading %s", path)
                    with status.track("upload", os.path.join(parent, target_name), os.lstat(staged).st_size):
                        drive.upload(staged, self.rpath(parent), strategy)
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
                nodes = self.fetch(parent)
                with self.mu:
                    if target_name == name and name in nodes:
                        # a save during this upload may have noted the older revision as its base
                        self.own_revs[path] = nodes[name].rev
                    cur = self.index.get(path)
                    if cur is not entry:
                        return
                    if target_name != name:
                        # Our copy now lives under the conflict name; the original path follows the remote.
                        new_path = os.path.join(parent, target_name)
                        self.index[new_path] = entry
                        del self.index[path]
                        path = new_path
                        name = target_name
                    node = nodes.get(name)
                    entry["pending"] = False
                    entry["rev"] = entry["base_rev"] = node.rev if node else None
                    if os.lstat(self.cfile(entry)).st_mtime_ns == mtime_before:
                        entry["dirty"] = False
                    else:
                        self.upload_due[path] = time.time() + cfg["upload_delay"]
                    self.save_index()

        def destroy(self, path):
            with self.mu:
                self.save_index()
            self.save_dirs()

    # Clean up a stale mount left by a crash ("Transport endpoint is not connected").
    with open("/proc/self/mounts") as f:
        stale = any(line.split()[1].replace("\\040", " ") == mountpoint and line.split()[2] == "fuse." + APP_NAME for line in f)
    if stale:
        fusermount = shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount3"
        subprocess.run([fusermount, "-u", "-z", mountpoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(mountpoint, exist_ok=True)
    if os.listdir(mountpoint):
        log.error("mount point %s is not empty; refusing to mount over existing files", mountpoint)
        sys.exit(1)
    log.info("mounting %s at %s", remote_root, mountpoint)
    fuse.FUSE(Ops(), mountpoint, foreground=True, nothreads=False, fsname="protondrive",
              subtype=APP_NAME, attr_timeout=5, entry_timeout=5, noatime=True)


# --------------------------------------------------------------------------------------
# Sync mode (one folder, always in sync)
# --------------------------------------------------------------------------------------

def sha1_of(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_sync(cfg):
    status = Status(cfg)
    drive = Drive(cfg, status)
    root = os.path.abspath(cfg["local_path"])
    remote_root = cfg["remote_path"].rstrip("/")
    state_path = os.path.join(STATE_DIR, "sync-state.json")
    trash_root = os.path.join(DATA_DIR, "local-trash")
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(CACHE_DIR, "staging"), mode=0o700, exist_ok=True)
    try:
        with open(state_path) as f:
            state = json.load(f)  # rel path -> {"dir": True} | {"size", "mtime_ns", "rev", ["need_rev"]}
    except (OSError, ValueError):
        state = {}
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    absent = object()

    def rp(rel):
        if not rel:
            return remote_root
        return remote_root + "/" + "/".join(escape_name(p) for p in rel.split("/"))

    def ensure_remote_root():
        parent, name = remote_root.rsplit("/", 1)
        try:
            drive.list(remote_root)
        except DriveError as ex:
            if not ex.not_found:
                raise
            log.info("creating remote folder %s", remote_root)
            drive.mkdir(parent or "/my-files", name)

    def remote_tree():
        tree = {}
        todo = [""]
        while todo:
            rel = todo.pop()
            try:
                children = drive.list(rp(rel))
            except DriveError as ex:
                if not rel or not ex.not_found:
                    raise
                tree.pop(rel, None)  # folder removed while we were listing it
                continue
            for name, n in children.items():
                if "∕" in name or ignored(name):
                    continue
                child = f"{rel}/{name}" if rel else name
                tree[child] = n
                if n.is_dir:
                    todo.append(child)
        return tree

    def local_tree():
        tree = {}
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if not ignored(d) and not os.path.islink(os.path.join(dirpath, d))]
            for d in dirnames:
                tree[f"{rel_dir}/{d}" if rel_dir else d] = None
            for fn in filenames:
                if ignored(fn):
                    continue
                try:
                    st = os.lstat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    tree[f"{rel_dir}/{fn}" if rel_dir else fn] = (st.st_size, st.st_mtime_ns)
        return tree

    def drop_state(rel):
        state.pop(rel, None)
        for k in [k for k in state if k.startswith(rel + "/")]:
            del state[k]

    def move_to_local_trash(rel):
        dst = os.path.join(trash_root, datetime.now().strftime("%Y-%m-%d"), rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            dst += "." + uuid.uuid4().hex[:6]
        shutil.move(os.path.join(root, rel), dst)

    def download(rel, node):
        tmpdir = tempfile.mkdtemp(prefix="dl-", dir=os.path.join(CACHE_DIR, "staging"))
        try:
            with status.track("download", rel, node.size, tmpdir):
                drive.download(rp(rel), tmpdir)
            got = os.listdir(tmpdir)
            if len(got) != 1:
                raise DriveError(1, f"unexpected download result {got}")
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.utime(os.path.join(tmpdir, got[0]), (node.mtime, node.mtime))
            os.replace(os.path.join(tmpdir, got[0]), dst)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        st = os.lstat(dst)
        state[rel] = {"rev": node.rev, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        log.info("downloaded %s", rel)

    def upload(rel):
        stage, staged = stage_for_upload(os.path.join(root, rel), os.path.basename(rel))
        st = os.lstat(os.path.join(root, rel))
        try:
            with status.track("upload", rel, st.st_size):
                drive.upload(staged, rp(os.path.dirname(rel)), "create-new-revision")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        state[rel] = {"rev": None, "need_rev": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        log.info("uploaded %s", rel)

    def same_content(rel, l, r):
        if l[0] != r.size:
            return False
        if r.sha1:
            return sha1_of(os.path.join(root, rel)) == r.sha1
        return abs(r.mtime - l[1] / 1e9) < 2

    def conflict(rel, r):
        cname = conflict_name(os.path.basename(rel))
        crel = os.path.join(os.path.dirname(rel), cname)
        os.replace(os.path.join(root, rel), os.path.join(root, crel))
        upload(crel)
        download(rel, r)
        log.warning("conflict on %s: both sides changed, your local copy is now %s", rel, crel)
        notify("Proton Drive: conflict", f"Your copy was saved as {cname}")

    def sync_dir(rel, l, r, s, fresh, local, remote, skip):
        """Returns True when the remote side was modified."""
        l_dir, r_dir = l is None, r is not None and r.is_dir
        if (l is not absent and not l_dir) or (r is not None and not r_dir):
            log.warning("a file and a folder share the name %s; skipping it", rel)
            skip.append(rel)
            return False
        if l_dir and r_dir:
            state[rel] = {"dir": True}
        elif l_dir:
            if s and fresh:
                # Folder removed remotely; keep it only if it holds local changes not yet uploaded.
                modified = any(k.startswith(rel + "/") and (local[k] is not None and
                               (state.get(k, {}).get("size"), state.get(k, {}).get("mtime_ns")) != local[k]
                               or local[k] is None and k not in state) for k in local)
                if not modified:
                    move_to_local_trash(rel)
                    drop_state(rel)
                    skip.append(rel)
                    log.info("folder removed on Proton Drive, local copy moved to %s: %s", trash_root, rel)
                    return False
            elif s:
                return False  # wait for a fresh remote listing before deciding
            drive.mkdir(rp(os.path.dirname(rel)), os.path.basename(rel))
            state[rel] = {"dir": True}
            log.info("created remote folder %s", rel)
            status.event("mkdir", rel)
            return True
        elif r_dir:
            if s:
                # Folder removed locally; trash it remotely unless it changed remotely meanwhile.
                modified = any(k.startswith(rel + "/") and (n.is_dir and k not in state or
                               not n.is_dir and state.get(k, {}).get("rev") != n.rev
                               and not state.get(k, {}).get("need_rev")) for k, n in remote.items())
                if not modified:
                    drive.trash(rp(rel))
                    drop_state(rel)
                    skip.append(rel)
                    log.info("folder deleted locally, moved to Proton Drive trash: %s", rel)
                    status.event("trash", rel)
                    return True
            os.makedirs(os.path.join(root, rel), exist_ok=True)
            state[rel] = {"dir": True}
        else:
            drop_state(rel)
            skip.append(rel)
        return False

    def recheck(rel, l, r, s, now_ns):
        """Before uploading over a stale view of the remote side, look at the file's current remote
        version, so an edit made on Proton Drive since the last listing becomes a conflict copy."""
        try:
            cur = drive.list(rp(os.path.dirname(rel))).get(os.path.basename(rel))
        except DriveError as ex:
            if not ex.not_found:
                raise
            cur = None
        if cur is not None and cur.is_dir:
            log.warning("a file and a folder share the name %s; skipping it", rel)
            return False, False
        return sync_file(rel, l, cur, s, True, now_ns)

    def sync_file(rel, l, r, s, fresh, now_ns):
        """Returns (remote_modified, still_being_written)."""
        if l is not absent and now_ns - l[1] < 3_000_000_000:
            return False, True
        if s and s.get("need_rev"):
            if not fresh:
                return False, False  # our own upload is not visible yet
            if r is not None:
                s["rev"] = r.rev
            s.pop("need_rev")
        l_changed = l is not absent and (not s or (s.get("size"), s.get("mtime_ns")) != tuple(l))
        r_changed = r is not None and (not s or s.get("rev") != r.rev)
        if l is not absent and r is not None:
            if not l_changed and not r_changed:
                return False, False
            if (not s or (l_changed and r_changed)) and same_content(rel, l, r):
                state[rel] = {"rev": r.rev, "size": l[0], "mtime_ns": l[1]}
                return False, False
            if not s or (l_changed and r_changed):
                conflict(rel, r)
                return True, False
            if l_changed:
                if not fresh:
                    return recheck(rel, l, r, s, now_ns)
                upload(rel)
                return True, False
            download(rel, r)
            return False, False
        if l is not absent:
            if s and not l_changed:
                if fresh:
                    move_to_local_trash(rel)
                    state.pop(rel)
                    log.info("removed on Proton Drive, local copy moved to %s: %s", trash_root, rel)
                return False, False
            if not fresh:
                return recheck(rel, l, r, s, now_ns)
            upload(rel)
            return True, False
        if r is not None:
            if s and not r_changed:
                drive.trash(rp(rel))
                state.pop(rel)
                log.info("deleted locally, moved to Proton Drive trash: %s", rel)
                status.event("trash", rel)
                return True, False
            download(rel, r)
            return False, False
        state.pop(rel, None)
        return False, False

    def cycle(local, remote, fresh):
        known = [k for k in state]
        if known and fresh:
            gone_local = sum(1 for k in known if k not in local)
            gone_remote = sum(1 for k in known if k not in remote)
            limit = max(50, len(known) // 2)
            if gone_local > limit or gone_remote > limit:
                log.error("sync paused: %d local / %d remote deletions out of %d known items look like an "
                          "accident (unmounted disk, wrong folder?). If intended, run: proton-drive-sync "
                          "reset-state", gone_local, gone_remote, len(known))
                notify("Proton Drive sync paused", "Too many deletions detected, see 'proton-drive-sync logs'.")
                status.set_error("Sync paused: too many deletions (see 'proton-drive-sync logs')")
                return False, False
        if fresh and status.error and status.error.startswith("Sync paused"):
            status.set_error(None)
        now_ns = time.time_ns()
        changed = pending = False
        skip = []
        for rel in sorted(set(local) | set(remote) | set(state), key=lambda k: (k.count("/"), k)):
            if stop.is_set():
                break
            if any(rel.startswith(p + "/") or rel == p for p in skip):
                continue
            l, r, s = local.get(rel, absent), remote.get(rel), state.get(rel)
            try:
                if l is None or (r is not None and r.is_dir) or (s and s.get("dir")):
                    changed |= sync_dir(rel, l, r, s, fresh, local, remote, skip)
                else:
                    c, p = sync_file(rel, l, r, s, fresh, now_ns)
                    changed |= c
                    pending |= p
            except DriveError as ex:
                log.error("sync of %s failed: %s", rel, ex.message)
                if ex.auth:
                    break
            except OSError as ex:
                log.error("sync of %s failed: %s", rel, ex)
        atomic_write_json(state_path, state)
        return changed, pending

    ensure_remote_root()
    log.info("syncing %s <-> %s", root, remote_root)
    remote, last_remote, remote_dirty, last_sig = None, 0.0, True, None
    while not stop.is_set():
        try:
            local = local_tree()
            sig = hash(frozenset(local.items()))
            fresh = (remote is None or remote_dirty or time.time() - last_remote >= cfg["remote_interval"]
                     or any(k not in local for k in state))
            if fresh or sig != last_sig:
                if fresh:
                    remote = remote_tree()
                    last_remote = time.time()
                remote_dirty, pending = cycle(local, remote, fresh)
                last_sig = None if pending else hash(frozenset(local_tree().items()))
        except DriveError as ex:
            log.error("sync cycle failed: %s", ex.message)
            stop.wait(30)
        except Exception:
            log.exception("sync cycle crashed")
            stop.wait(60)
        stop.wait(cfg["local_interval"])


# --------------------------------------------------------------------------------------
# Tray icon (StatusNotifierItem over D-Bus; shown natively by KDE Plasma, XFCE, Cinnamon,
# and by GNOME with the AppIndicator extension). Reads status.json written by the daemon.
# --------------------------------------------------------------------------------------

# Tray icons: a cloud with a state glyph, written to disk at start and served via IconThemePath,
# so the icon looks the same on every desktop and icon theme.
TRAY_CLOUD = "M9.5 26a6.5 6.5 0 0 1-.9-12.94A8.5 8.5 0 0 1 25 12.6 6.75 6.75 0 0 1 24.25 26z"
TRAY_GLYPHS = {
    "idle": '<path d="M11.5 19l3 3 6-6.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "download": '<path d="M16 13.5v9m-4-4 4 4 4-4" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "upload": '<path d="M16 23v-9m-4 4 4-4 4 4" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "busy": '<g fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M20.6 16.6a4.8 4.8 0 0 0-8.6-1"/><path d="M11.4 20.4a4.8 4.8 0 0 0 8.6 1"/><path d="M11.6 12.6v3h3"/><path d="M20.4 24.4v-3h-3"/></g>',
    "error": '<path d="M16 13.5v5.5" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><circle cx="16" cy="22.6" r="1.6" fill="#fff"/>',
    "offline": '<path d="M5 6l22 22" stroke="#2b2b2b" stroke-width="4.5" stroke-linecap="round"/><path d="M5 6l22 22" stroke="#9a9a9a" stroke-width="2.2" stroke-linecap="round"/>',
}


def tray_icon_svg(state):
    top, bottom = {"error": ("#ff6b6b", "#d93a3a"), "offline": ("#a8a8a8", "#7a7a7a")}.get(state, ("#9b7bff", "#6a46e6"))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" '
            f'x2="0" y2="1"><stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
            f'</linearGradient></defs><path d="{TRAY_CLOUD}" fill="url(#g)"/>{TRAY_GLYPHS[state]}</svg>')


SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
 </interface>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
   <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
 </interface>
</node>"""

OP_TEXT = {"download": ("Downloading", "Downloaded", "↓"), "upload": ("Uploading", "Uploaded", "↑"),
           "mkdir": (None, "Created folder", "+"), "trash": (None, "Moved to trash", "🗑"),
           "rename": (None, "Renamed", "✎"), "move": (None, "Moved", "→"),
           "free": (None, "Freed space in", "♻")}


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def run_tray(cfg):
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        log.error("The status icon needs PyGObject (package python3-gobject or python3-gi); not starting it.")
        return
    import html
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "tray.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("status icon already running")
        return
    status_path = os.path.join(STATE_DIR, "status.json")
    daemon_lock = os.path.join(STATE_DIR, "daemon.lock")
    root = cfg["local_path"]
    path = "/StatusNotifierItem"
    menu_path = "/MenuBar"
    node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
    sni_iface, menu_iface = node.interfaces[0], node.interfaces[1]
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    icon_dir = os.path.join(STATE_DIR, "icons")
    apps_dir = os.path.join(icon_dir, "hicolor", "scalable", "apps")
    os.makedirs(apps_dir, exist_ok=True)
    ICONS = {k: f"{APP_NAME}-{k}" for k in TRAY_GLYPHS}
    for k, name in ICONS.items():
        for d in (icon_dir, apps_dir):  # flat for GTK's unthemed lookup, hicolor for KDE
            with open(os.path.join(d, name + ".svg"), "w") as f:
                f.write(tray_icon_svg(k))

    st = {"snap": {}, "running": False, "mtime": None, "rev": 1, "icon": None, "status": None, "tip": None,
          "items": {}}

    def daemon_running():
        try:
            with open(daemon_lock, "a") as f:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except OSError:
            return True

    def short(p, n=60):
        p = p.lstrip("/")
        return p if len(p) <= n else "…" + p[-(n - 1):]

    def describe():
        """-> (icon, headline, [active lines], [recent lines])"""
        s = st["snap"]
        if not st["running"]:
            return ICONS["offline"], "Proton Drive is not running", [], []
        lines = []
        for a in s.get("active", []):
            verb, _, arrow = OP_TEXT.get(a["op"], (a["op"], a["op"], "•"))
            size, done = a.get("size"), a.get("done")
            prog = f" — {min(99, int(done * 100 / size))}% of {human_size(size)}" if size and done is not None \
                else f" — {human_size(size)}" if size else ""
            lines.append(f"{arrow} {verb} {short(a['path'])}{prog}")
        pending = s.get("pending_uploads") or 0
        if pending:
            lines.append(f"{pending} change(s) waiting to upload")
        recent = []
        for r in s.get("recent", [])[:8]:
            _, done_verb, _ = OP_TEXT.get(r["op"], (r["op"], r["op"], "•"))
            when = datetime.fromtimestamp(r["t"]).strftime("%H:%M")
            what = f"Freed {r.get('msg') or 'space'}" if r["op"] == "free" else f"{done_verb} {short(r['path'], 50)}"
            recent.append(f"{when}  {'✓' if r['ok'] else '✗ Failed:'} {what}")
        if s.get("error"):
            return ICONS["error"], s["error"], lines, recent
        ops = {a["op"] for a in s.get("active", [])}
        if "download" in ops:
            ic = ICONS["download"]
        elif "upload" in ops:
            ic = ICONS["upload"]
        elif ops or pending:
            ic = ICONS["busy"]
        else:
            return ICONS["idle"], "Proton Drive is up to date", lines, recent
        n = len(s.get("active", []))
        return ic, f"Syncing {n} item(s)…" if n else "Waiting to upload…", lines, recent

    def read_settings():
        try:
            with open(CONFIG_FILE) as f:
                return with_storage_defaults(json.load(f))
        except (OSError, ValueError):
            return with_storage_defaults({})

    def set_setting(key, value):
        update_config(lambda c: c.__setitem__(key, value))

    def unpin(p):
        update_config(lambda c: c.__setitem__("pinned", [q for q in c.get("pinned", []) if q != p]))

    def pick_folder():
        """Asks for a folder inside the drive with whatever dialog tool the desktop has."""
        title = "Choose a folder to always keep on this device"
        for tool in (["kdialog", "--title", title, "--getexistingdirectory", root],
                     ["zenity", "--file-selection", "--directory", "--title", title, f"--filename={root}/"],
                     ["qarma", "--file-selection", "--directory", "--title", title, f"--filename={root}/"],
                     ["yad", "--file", "--directory", "--title", title, f"--filename={root}/"]):
            if shutil.which(tool[0]):
                break
        else:
            notify("Proton Drive", "Right-click a folder in your file manager and choose Proton Drive > "
                                   "Always keep on this device, or run: proton-drive-sync pin FOLDER")
            return

        def run():
            r = subprocess.run(tool, capture_output=True, text=True)
            chosen = r.stdout.strip()
            if r.returncode == 0 and chosen:
                storage_command(dict(cfg), "pin", [chosen])
        threading.Thread(target=run, daemon=True).start()

    def build_menu():
        """Menu tree of (id, props, children); ids are stable hashes so clicks survive rebuilds."""
        import zlib
        flat, actions = {}, {}

        def mk(key, label=None, enabled=True, icon=None, sep=False, radio=None, children=None, action=None):
            i = (zlib.crc32(key.encode()) & 0x7fffffff) or 1
            props = {"type": GLib.Variant("s", "separator")} if sep else \
                {"label": GLib.Variant("s", label.replace("_", "__"))}
            if not enabled:
                props["enabled"] = GLib.Variant("b", False)
            if icon:
                props["icon-name"] = GLib.Variant("s", icon)
            if radio is not None:
                props["toggle-type"] = GLib.Variant("s", "radio")
                props["toggle-state"] = GLib.Variant("i", 1 if radio else 0)
            if children is not None:
                props["children-display"] = GLib.Variant("s", "submenu")
            flat[i], actions[i] = props, action
            return (i, props, children or [])

        ic, head, lines, recent = describe()
        items = [mk("head", head, enabled=False)]
        items += [mk(f"line{n}", ln, enabled=False) for n, ln in enumerate(lines)]
        items.append(mk("sep1", sep=True))
        if recent:
            items.append(mk("recent", "Recent activity", enabled=False))
            items += [mk(f"recent{n}", ln, enabled=False) for n, ln in enumerate(recent)]
            items.append(mk("sep2", sep=True))
        if cfg["mode"] == "mount":
            conf = read_settings()
            c = st["snap"].get("cache") or {}
            limit = conf["cache_max_gb"]
            items.append(mk("usage", f"On this device: {human_size(c.get('used', 0))} downloaded"
                            f"{f' of {limit} GB' if limit else ''}, {human_size(c.get('pinned', 0))} always kept",
                            enabled=False))
            if c.get("problem"):
                items.append(mk("problem", "⚠ " + c["problem"], enabled=False))
            items.append(mk("limit", "Space for downloaded files", icon="drive-harddisk", children=[
                mk(f"limit{v}", f"{v} GB" if v else "No limit", radio=limit == v,
                   action=lambda v=v: set_setting("cache_max_gb", v)) for v in (5, 10, 20, 50, 100, 250, 0)]))
            exp = conf["cache_expire_days"]
            items.append(mk("expire", "Remove downloads not opened for", icon="chronometer", children=[
                mk(f"expire{v}", f"{v} days" if v else "Never", radio=exp == v,
                   action=lambda v=v: set_setting("cache_expire_days", v)) for v in (7, 14, 30, 90, 0)]))
            pins = [mk(f"pin:{p}", f"✓ {short(p) or 'Everything'}  (click to stop keeping)",
                       action=lambda p=p: unpin(p)) for p in conf["pinned"]]
            if not pins:
                pins = [mk("nopins", "Nothing yet", enabled=False)]
            items.append(mk("pins", "Always keep on this device", icon="folder-download", children=pins + [
                mk("pinsep", sep=True),
                mk("pinadd", "Add a folder…", icon="list-add", action=pick_folder)]))
            items.append(mk("free", "Free up space now", icon="edit-clear",
                            action=lambda: request_free(["/"])))
            items.append(mk("sep3", sep=True))
        items.append(mk("open", "Open Proton Drive folder", icon="folder-open", action=open_folder))
        items.append(mk("web", "Open Proton Drive in browser", icon="internet-web-browser",
                        action=lambda: open_uri("https://drive.proton.me/")))
        items.append(mk("sep4", sep=True))
        items.append(mk("quit", "Hide until next login", icon="window-close", action=hide))
        st["items"], st["actions"] = flat, actions
        return items

    def layout_variant(parent_id=0, depth=-1):
        """The menu below `parent_id` (0 = top level), `depth` levels deep (-1 = all): a submenu
        must get its own items, not the whole menu again."""
        def v(node, d):
            i, props, children = node
            kids = [] if d == 0 else [v(c, d - 1) for c in children]
            return GLib.Variant("(ia{sv}av)", (i, props, kids))

        def find(nodes, i):
            for n in nodes:
                if n[0] == i:
                    return n
                hit = find(n[2], i)
                if hit:
                    return hit
            return None
        top = (0, {"children-display": GLib.Variant("s", "submenu")}, build_menu())
        node = top if parent_id == 0 else find(top[2], parent_id) or (parent_id, {}, [])
        i, props, children = node
        kids = [] if depth == 0 else [v(c, depth - 1) for c in children]
        return GLib.Variant("(u(ia{sv}av))", (st["rev"], (i, props, kids)))

    def open_uri(uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as ex:
            log.warning("could not open %s (%s); trying xdg-open", uri, ex.message)
            if shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_folder():
        """Ask the desktop's file manager first: the default app for folders can be something else
        (Catfish, the XFCE search tool, registers itself for them and wins on Manjaro XFCE)."""
        uri = Gio.File.new_for_path(root).get_uri()
        try:
            bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                          "org.freedesktop.FileManager1", "ShowFolders", GLib.Variant("(ass)", ([uri], "")),
                          None, Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as ex:
            log.info("no file manager service (%s); using the default app for folders", ex.message)
            open_uri(uri)

    def hide():
        notify("Proton Drive", "The status icon is hidden until you log in again. Syncing continues. "
                               "To show it now, run: proton-drive-sync start")
        loop.quit()

    def on_menu_event(item_id, event_id):
        if event_id != "clicked":
            return
        action = st.get("actions", {}).get(item_id)
        label = st.get("items", {}).get(item_id, {}).get("label")
        log.info("menu: %s", label.unpack() if label else f"item {item_id} (no action)")
        if action:
            try:
                action()
            except Exception as ex:
                log.warning("menu action failed: %s", ex)
                notify("Proton Drive", f"That did not work: {ex}")

    def tooltip():
        ic, head, lines, recent = describe()
        # newline-separated, no <br/>: XFCE feeds this to Pango markup, which rejects <br/> and
        # then silently shows the previous tooltip (e.g. a neighbouring icon's)
        body = "\n".join(html.escape(x, quote=False) for x in [head] + (lines[:6] or [root]))
        return GLib.Variant("(sa(iiay)ss)", (ic, [], "Proton Drive", body))

    def sni_method(conn, sender, obj, iface, method, params, inv):
        if iface == "org.kde.StatusNotifierItem":
            if method in ("Activate", "SecondaryActivate"):
                open_folder()
            inv.return_value(None)
            return
        if method == "GetLayout":
            parent_id, depth, _ = params.unpack()
            inv.return_value(layout_variant(parent_id, depth))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            if not st["items"]:
                build_menu()
            out = [(i, p) for i, p in st["items"].items() if not ids or i in ids]
            inv.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            inv.return_value(GLib.Variant("(v)", (st["items"].get(i, {}).get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, ev, _, _ = params.unpack()
            on_menu_event(i, ev)
            inv.return_value(None)
        elif method == "EventGroup":
            for i, ev, _, _ in params.unpack()[0]:
                on_menu_event(i, ev)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_value(None)

    def sni_prop(conn, sender, obj, iface, prop):
        if iface == "com.canonical.dbusmenu":
            return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                    "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)
        ic = describe()[0]
        return {"Category": GLib.Variant("s", "ApplicationStatus"), "Id": GLib.Variant("s", APP_NAME),
                "Title": GLib.Variant("s", "Proton Drive"),
                "Status": GLib.Variant("s", "NeedsAttention" if ic == ICONS["error"] else "Active"),
                "IconName": GLib.Variant("s", ic), "AttentionIconName": GLib.Variant("s", ICONS["error"]),
                "IconThemePath": GLib.Variant("s", icon_dir),
                "ToolTip": tooltip(), "ItemIsMenu": GLib.Variant("b", False),
                "Menu": GLib.Variant("o", menu_path)}.get(prop)

    bus.register_object(path, sni_iface, sni_method, sni_prop, None)
    bus.register_object(menu_path, menu_iface, sni_method, sni_prop, None)

    def emit(obj, iface, name, params=None):
        bus.emit_signal(None, obj, iface, name, params)

    def refresh():
        running = daemon_running()
        try:
            mtime = os.stat(status_path).st_mtime_ns
        except OSError:
            mtime = None
        try:
            cmtime = os.stat(CONFIG_FILE).st_mtime_ns
        except OSError:
            cmtime = None
        if running == st["running"] and mtime == st["mtime"] and cmtime == st.get("cmtime"):
            return True
        st["running"], st["mtime"], st["cmtime"] = running, mtime, cmtime
        try:
            with open(status_path) as f:
                st["snap"] = json.load(f)
        except (OSError, ValueError):
            st["snap"] = {}
        ic, head, lines, _ = describe()
        sni = "org.kde.StatusNotifierItem"
        if ic != st["icon"]:
            st["icon"] = ic
            emit(path, sni, "NewIcon")
            stat_ = "NeedsAttention" if ic == ICONS["error"] else "Active"
            if stat_ != st["status"]:
                st["status"] = stat_
                emit(path, sni, "NewStatus", GLib.Variant("(s)", (stat_,)))
        tip = (head, tuple(lines))
        if tip != st["tip"]:
            st["tip"] = tip
            emit(path, sni, "NewToolTip")
        st["rev"] += 1
        emit(menu_path, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (st["rev"], 0)))
        return True

    def register(*_):
        bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                 "RegisterStatusNotifierItem", GLib.Variant("(s)", (bus_name,)), None,
                 Gio.DBusCallFlags.NONE, -1, None, None)
        log.info("status icon registered")

    def owned(*_):
        Gio.bus_watch_name_on_connection(bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                                         register, None)

    Gio.bus_own_name_on_connection(bus, bus_name, Gio.BusNameOwnerFlags.NONE, owned, None)
    refresh()
    GLib.timeout_add(1000, refresh)
    loop = GLib.MainLoop()
    signal.signal(signal.SIGTERM, lambda *a: loop.quit())
    loop.run()


# --------------------------------------------------------------------------------------
# Storage commands (mode 1): used from the terminal, file-manager menus and the tray icon
# --------------------------------------------------------------------------------------

STORAGE_COMMANDS = ("pin", "unpin", "pins", "free", "cache", "set")


def mount_path(arg, root):
    """Local path inside the mount -> mount path ("/Work/a.txt"), or None if outside."""
    a, root = os.path.abspath(os.path.expanduser(arg)), os.path.abspath(root)
    if a == root:
        return "/"
    return a[len(root):] if a.startswith(root + "/") else None


def storage_command(cfg, cmd, args):
    def say(msg, ok=True):
        print(msg, file=sys.stdout if ok else sys.stderr)
        if not sys.stdout.isatty():  # started from a file manager: show a notification instead
            notify("Proton Drive", msg)
        return 0 if ok else 1

    if cfg["mode"] != "mount":
        return say("Keeping files on this device and freeing space only apply to mode 1 "
                   "(in mode 2 every file is always on this device).", ok=False)
    root = cfg["local_path"]
    cfg = with_storage_defaults(cfg)
    if cmd in ("pin", "unpin", "free"):
        if not args:
            return say(f"Usage: proton-drive-sync {cmd} PATH...", ok=False)
        paths = []
        for a in args:
            p = mount_path(a, root)
            if p is None:
                return say(f"{a} is not inside your Proton Drive folder ({root}).", ok=False)
            paths.append(p)
        if cmd == "pin":
            def change(c):
                pins = c.setdefault("pinned", [])
                for p in paths:
                    if not any(under(p, q) for q in pins):
                        pins[:] = [q for q in pins if not under(q, p)] + [p]
            update_config(change)
            names = ", ".join(os.path.basename(p) or "Proton Drive" for p in paths)
            return say(f"{names}: will always be kept on this device (downloading in the background).")
        if cmd in ("unpin", "free"):
            pins = cfg["pinned"]
            blocked = [(p, q) for p in paths for q in pins if under(p, q) and p != q]
            update_config(lambda c: c.__setitem__("pinned", [q for q in c.get("pinned", [])
                                                             if not any(under(q, p) for p in paths)]))
            if cmd == "free":
                request_free(paths)
            msg = ("Freeing up space: downloaded copies are removed; files stay visible and download again "
                   "when opened." if cmd == "free" else "No longer always kept on this device.")
            if blocked:
                msg += f" Note: {blocked[0][0]} is inside {blocked[0][1]}, which is still always kept."
            return say(msg)
    if cmd == "pins":
        for p in cfg["pinned"]:
            print(os.path.join(root, p.lstrip("/")))
        if not cfg["pinned"]:
            print("Nothing is pinned. Use: proton-drive-sync pin PATH")
        return 0
    if cmd == "cache":
        try:
            with open(os.path.join(STATE_DIR, "status.json")) as f:
                c = json.load(f).get("cache") or {}
        except (OSError, ValueError):
            c = {}
        print(f"Downloaded files:      {human_size(c.get('used', 0))}"
              f" (limit: {str(cfg['cache_max_gb']) + ' GB' if cfg['cache_max_gb'] else 'none'})")
        print(f"Always kept on device: {human_size(c.get('pinned', 0))} in {len(cfg['pinned'])} item(s)")
        print(f"Remove unused after:   {str(cfg['cache_expire_days']) + ' days' if cfg['cache_expire_days'] else 'never'}")
        print(f"Keep free on disk:     {cfg['min_free_gb']} GB (now free: {human_size(c.get('free'))})")
        if c.get("problem"):
            print(f"Problem:               {c['problem']}")
        return 0
    if cmd == "set":
        keys = {"cache-limit": "cache_max_gb", "expire-days": "cache_expire_days", "min-free": "min_free_gb"}
        if len(args) != 2 or args[0] not in keys:
            return say("Usage: proton-drive-sync set cache-limit GB|none | expire-days DAYS|never | "
                       "min-free GB", ok=False)
        v = args[1].lower()
        try:
            value = 0 if v in ("none", "never", "off", "0") else float(v)
            if value < 0:
                raise ValueError
        except ValueError:
            return say(f"Not a number: {args[1]}", ok=False)
        value = int(value) if value == int(value) else value
        update_config(lambda c: c.__setitem__(keys[args[0]], value))
        return say(f"{args[0]} set to {value or args[1]}.")
    return 2


def thunar_menu(action, cli=None):
    """Adds or removes our Thunar custom actions. Edits uca.xml as text: the file holds the user's
    own actions and must stay as it was (it often isn't strict XML either, for example a reordered
    <?xml?> declaration, which Thunar's parser accepts). Thunar can't limit an action to a folder,
    so these show everywhere; the commands refuse paths outside the drive, with a notification."""
    import re
    import shlex
    from xml.sax.saxutils import escape
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    path = os.path.realpath(os.path.join(home, "Thunar", "uca.xml"))
    source = path
    if action == "add" and not os.path.exists(path):
        # Thunar reads the user's file *instead of* the system one, so start from a copy of it
        dirs = (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
        source = next((p for p in (os.path.join(d, "Thunar", "uca.xml") for d in dirs) if os.path.exists(p)), path)
    try:
        with open(source, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        if action == "remove":
            return 0
        text = '<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n</actions>\n'
    new = re.sub(r"<action>.*?</action>\s*",
                 lambda m: "" if f"<unique-id>{APP_NAME}-" in m[0] else m[0], text, flags=re.S)
    if action == "add":
        if new.count("</actions>") != 1:
            print(f"{path} doesn't look right; not changing it.", file=sys.stderr)
            return 1
        items = "".join(f"""<action>
\t<icon>{icon}</icon>
\t<name>{name}</name>
\t<submenu>Proton Drive</submenu>
\t<unique-id>{APP_NAME}-{cmd}</unique-id>
\t<command>{escape(shlex.quote(cli))} {cmd} %F</command>
\t<description></description>
\t<patterns>*</patterns>
\t<directories/>
\t<audio-files/>
\t<image-files/>
\t<other-files/>
\t<text-files/>
\t<video-files/>
</action>
""" for cmd, name, icon in (("pin", "Always keep on this device", "folder-download"),
                            ("free", "Free up space", "edit-clear")))
        new = new.replace("</actions>", items + "</actions>")
    if new != text or source != path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        shutil.copymode(source, tmp) if os.path.exists(source) else os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    return 0


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = load_config()
    if cmd == "run":
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        lock = open(os.path.join(STATE_DIR, "daemon.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another instance is already running")
            return
        if cfg["mode"] == "mount":
            run_mount(cfg)
        else:
            run_sync(cfg)
    elif cmd == "tray":
        run_tray(cfg)
    elif cmd in STORAGE_COMMANDS:
        sys.exit(storage_command(cfg, cmd, sys.argv[2:]))
    elif cmd == "thunar-menu":
        sys.exit(thunar_menu(*sys.argv[2:4]))
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
