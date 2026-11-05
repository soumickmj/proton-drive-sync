#!/usr/bin/env python3
"""Logic test of mount mode: drives the FUSE operations directly (no kernel mount) against
the fake CLI, using a stub in place of mfusepy."""
import errno
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
W = tempfile.mkdtemp()
os.environ.update(HOME=f"{W}/home", XDG_CONFIG_HOME=f"{W}/home/.config", XDG_DATA_HOME=f"{W}/home/.local/share",
                  XDG_CACHE_HOME=f"{W}/home/.cache", XDG_STATE_HOME=f"{W}/home/.local/state",
                  FAKE_REMOTE=f"{W}/remote")
DATA = f"{W}/home/.local/share/proton-drive-sync"
R = f"{W}/remote/my-files"
os.makedirs(DATA)
os.makedirs(f"{W}/home/.config/proton-drive-sync")
os.makedirs(f"{R}/docs")
shutil.copy(f"{HERE}/../src/pdsync.py", DATA)
with open(f"{DATA}/mfusepy.py", "w") as f:
    f.write('''import os
class FuseOSError(OSError):
    def __init__(self, e): super().__init__(e, os.strerror(e))
class Operations: pass
def fuse_get_context(): return (os.getuid(), os.getgid(), os.getpid())
INSTANCE = None
class FUSE:
    def __init__(self, ops, mnt, **kw):
        global INSTANCE; INSTANCE = ops
''')
with open(f"{W}/pd", "w") as f:
    f.write(f'#!/bin/sh\nexec /usr/bin/python3 -I {HERE}/fake_proton_drive.py "$@"\n')
os.chmod(f"{W}/pd", 0o755)
with open(f"{R}/hello.txt", "w") as f:
    f.write("hello remote\n")
with open(f"{R}/docs/big.bin", "wb") as f:
    f.write(os.urandom(300_000))

cfg = {"mode": "mount", "local_path": f"{W}/mnt", "remote_path": "/my-files", "credentials_store": "unsafe_file",
       "binary": f"{W}/pd", "list_ttl": 0.5, "upload_delay": 0.2, "cache_max_gb": 0.0005,
       "no_download_procs": ["tumblerd"]}
sys.argv = ["pdsync"]
sys.path.insert(0, DATA)
import pdsync  # noqa: E402
import mfusepy  # noqa: E402

pdsync.run_mount(cfg)
ops = mfusepy.INSTANCE
calls = f"{W}/remote/.calls"
fails = 0


def check(desc, cond_fn, timeout=10):
    global fails
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond_fn():
                print("PASS", desc)
                return
        except Exception:
            pass
        time.sleep(0.2)
    print("FAIL", desc)
    fails += 1


def ncalls(word):
    with open(calls) as f:
        return sum(1 for line in f if line.startswith("filesystem " + word))


def read_all(path):
    fh = ops.open(path, os.O_RDONLY)
    data = ops.read(path, 10_000_000, 0, fh)
    ops.release(path, fh)
    return data


def write_file(path, data, create=False, flags=os.O_RDWR):
    fh = ops.create(path, 0o644) if create else ops.open(path, flags)
    ops.truncate(path, 0, fh)
    ops.write(path, data, 0, fh)
    ops.release(path, fh)


def errno_of(fn):
    try:
        fn()
    except OSError as e:
        return e.errno
    return 0


def rtext(p):
    with open(f"{R}/{p}", "rb") as f:
        return f.read()


FUSE_OPS = {"lock", "flock", "ioctl", "poll", "init", "destroy", "access", "bmap", "fallocate", "lseek",
            "copy_file_range", "read_buf", "write_buf", "getxattr", "setxattr", "listxattr", "removexattr"}
check("no instance attribute shadows a FUSE op", lambda: not (FUSE_OPS & set(vars(ops))))
check("listing shows remote items", lambda: {"hello.txt", "docs"} <= set(ops.readdir("/")))
check("size known before download", lambda: ops.getattr("/hello.txt")["st_size"] == 13)
check("nothing downloaded yet", lambda: ncalls("download") == 0)
check("placeholder uses 0 disk blocks", lambda: ops.getattr("/docs/big.bin")["st_blocks"] == 0)
check("open downloads content", lambda: read_all("/hello.txt") == b"hello remote\n")
check("second open served from cache", lambda: read_all("/hello.txt") and ncalls("download") == 1)

write_file("/new.txt", b"new local\n", create=True)
check("new file visible immediately", lambda: "new.txt" in ops.readdir("/"))
check("new file uploaded", lambda: rtext("new.txt") == b"new local\n")

write_file("/hello.txt", b"edited\n")
check("edit uploaded as new revision", lambda: rtext("hello.txt") == b"edited\n")

# editor-style atomic save: write temp file, rename over original
write_file("/.hello.txt.swp~tmp", b"atomic save\n", create=True)
ops.rename("/.hello.txt.swp~tmp", "/hello.txt")
check("atomic save uploads to original name", lambda: rtext("hello.txt") == b"atomic save\n")
check("temp file never uploaded", lambda: not any(n.startswith(".hello") for n in os.listdir(R)))

ops.mkdir("/newdir", 0o755)
check("mkdir creates remote folder", lambda: os.path.isdir(f"{R}/newdir"))
ops.rename("/new.txt", "/newdir/moved.txt")
check("move into folder", lambda: rtext("newdir/moved.txt") == b"new local\n" and not os.path.exists(f"{R}/new.txt"))
ops.rename("/newdir/moved.txt", "/newdir/renamed.txt")
check("rename", lambda: os.path.exists(f"{R}/newdir/renamed.txt"))
check("renamed file readable", lambda: read_all("/newdir/renamed.txt") == b"new local\n")
ops.unlink("/newdir/renamed.txt")
check("delete goes to Proton trash", lambda: not os.listdir(f"{R}/newdir") and
      any(n.startswith("renamed.txt") for n in os.listdir(f"{W}/remote/.trash")))
ops.rmdir("/newdir")
check("rmdir", lambda: not os.path.exists(f"{R}/newdir"))
check("rmdir on non-empty refused", lambda: errno_of(lambda: ops.rmdir("/docs")) == errno.ENOTEMPTY)

before = ncalls("download")
fh = ops.open("/docs/big.bin", os.O_WRONLY | os.O_TRUNC)
ops.write("/docs/big.bin", b"replaced", 0, fh)
ops.release("/docs/big.bin", fh)
check("O_TRUNC open skips download", lambda: ncalls("download") == before)
check("truncated content uploaded", lambda: rtext("docs/big.bin") == b"replaced")

# conflict: remote changes while we hold a modified copy
read_all("/hello.txt")
fh = ops.open("/hello.txt", os.O_RDWR)
time.sleep(1.1)
with open(f"{R}/hello.txt", "wb") as f:
    f.write(b"changed elsewhere\n")
ops.write("/hello.txt", b"MINE", 0, fh)
ops.release("/hello.txt", fh)
check("conflict keeps remote version", lambda: rtext("hello.txt") == b"changed elsewhere\n")
check("conflict copy uploaded", lambda: any(n.startswith("hello (conflict") and rtext(n).startswith(b"MINE")
                                            for n in os.listdir(R)))

# thumbnailers/indexers must not trigger downloads
with open(f"{R}/photo.jpg", "wb") as f:
    f.write(b"jpeg")
ops.dirs.clear()
pdsync_ops_blocked = type(ops).caller_blocked
type(ops).caller_blocked = lambda self, who=None: True
check("thumbnailer gets EACCES", lambda: errno_of(lambda: ops.open("/photo.jpg", os.O_RDONLY)) == errno.EACCES)
type(ops).caller_blocked = pdsync_ops_blocked

check("remote change detected after TTL",
      lambda: (open(f"{R}/docs/remote_new.txt", "w").write("x"), ops.readdir("/docs"))[1].count("remote_new.txt") == 1
      or "remote_new.txt" in ops.readdir("/docs"))

# cache eviction: limit is ~500 KB
with open(f"{R}/docs/e1.bin", "wb") as f:
    f.write(os.urandom(400_000))
with open(f"{R}/docs/e2.bin", "wb") as f:
    f.write(os.urandom(400_000))
ops.dirs.clear()
read_all("/docs/e1.bin")
read_all("/docs/e2.bin")
check("LRU eviction keeps cache under limit", lambda: "/docs/e1.bin" not in ops.index and "/docs/e2.bin" in ops.index)
check("evicted file re-downloads on access", lambda: len(read_all("/docs/e1.bin")) == 400_000)

check("index has no dirty leftovers", lambda: not any(e.get("dirty") for e in ops.index.values()))

# a long download must not block browsing
with open(f"{R}/slow.bin", "wb") as f:
    f.write(b"s" * 1000)
os.makedirs(f"{R}/fresh/sub")
ops.dirs.pop("/", None)
ops.readdir("/")
t = threading.Thread(target=read_all, args=("/slow.bin",))
t.start()
time.sleep(0.5)
t0 = time.time()
names = ops.readdir("/fresh")
check("listing during a long download is not blocked", lambda: "sub" in names and time.time() - t0 < 2)
t.join()

# status file for the tray icon
STATUS = f"{W}/home/.local/state/proton-drive-sync/status.json"


def status():
    import json
    with open(STATUS) as f:
        return json.load(f)


check("status.json reports recent downloads and uploads",
      lambda: {"download", "upload", "mkdir", "trash"} <= {r["op"] for r in status()["recent"]} or
      {"download", "upload"} <= {r["op"] for r in status()["recent"]})
check("status.json is idle at the end", lambda: status()["state"] == "idle")

# folder listings survive a restart
ops.dirs_dirty = True
ops.save_dirs()
check("folder cache saved", lambda: os.path.exists(f"{W}/home/.cache/proton-drive-sync/dirs.json"))

# priority lock: higher priority waiters go first
pl, order = pdsync.PrioLock(), []
pl.acquire(0)
ths = [threading.Thread(target=lambda p=p: (pl.acquire(p), order.append(p), pl.release())) for p in (2, 1, 0)]
for th in ths:
    th.start()
    time.sleep(0.1)
pl.release()
for th in ths:
    th.join()
check("priority lock serves UI before background", lambda: order == [0, 1, 2])
shutil.rmtree(W, ignore_errors=True)
print("failures:", fails)
sys.exit(fails)
