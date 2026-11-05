#!/usr/bin/env python3
"""Test double for the official `proton-drive` CLI.

Emulates the subset of commands used by pdsync over a local directory ($FAKE_REMOTE),
producing JSON shaped like the SDK's NodeEntity.
"""
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.environ["FAKE_REMOTE"]
TRASH = os.path.join(ROOT, ".trash")


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def split(path):
    segs, cur, i = [], "", 0
    while i < len(path):
        c = path[i]
        if c == "\\" and i + 1 < len(path) and path[i + 1] == "/":
            cur += "/"
            i += 2
            continue
        if c == "/":
            segs.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    segs.append(cur)
    return [s for s in segs if s]


def local(path):
    segs = split(path)
    if not segs or segs[0] != "my-files":
        fail(f"Path {path} not supported")
    return os.path.join(ROOT, *[s.replace("/", "%2F") for s in segs])


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def node(p):
    st = os.stat(p)
    name = os.path.basename(p).replace("%2F", "/")
    n = {"uid": f"vol~{st.st_ino}", "name": {"ok": True, "value": name},
         "type": "folder" if os.path.isdir(p) else "file",
         "creationTime": iso(st.st_ctime), "modificationTime": iso(st.st_mtime), "isShared": False}
    if not os.path.isdir(p):
        with open(p, "rb") as f:
            sha = hashlib.sha1(f.read()).hexdigest()
        n["activeRevision"] = {"uid": f"rev~{st.st_ino}~{st.st_mtime_ns}~{st.st_size}", "state": "active",
                               "storageSize": st.st_size + 100, "claimedSize": st.st_size,
                               "claimedModificationTime": iso(st.st_mtime),
                               "claimedDigests": {"sha1": sha, "sha1Verified": True}}
    return n


def opts(args, *names):
    out, rest, i = {}, [], 0
    while i < len(args):
        if args[i] in names:
            out[args[i]] = args[i + 1]
            i += 2
        elif args[i] in ("-j", "--json", "-t", "--skip-thumbnails", "-v"):
            i += 1
        else:
            rest.append(args[i])
            i += 1
    return out, rest


def main():
    a = sys.argv[1:]
    if os.environ.get("FAKE_LOGGED_OUT"):
        fail("You need to login first")
    with open(os.path.join(ROOT, ".calls"), "a") as f:
        f.write(" ".join(a) + "\n")
    if a[:1] == ["--version"]:
        print("fake")
        return
    grp, cmd, rest = a[0], a[1], a[2:]
    if grp == "auth":
        return
    o, r = opts(rest, "-f", "-d")
    if cmd == "list":
        p = local(r[0])
        if not os.path.isdir(p):
            fail(f"Node not found: {r[0]}")
        items = [node(os.path.join(p, n)) for n in sorted(os.listdir(p))]
        print("[\n" + ",\n".join(json.dumps(i) for i in items) + "\n]")
    elif cmd == "download":
        src, dst = local(r[0]), r[1]
        if not os.path.exists(src):
            fail(f"Node not found: {r[0]}")
        target = os.path.join(dst, os.path.basename(src).replace("%2F", "/"))
        if os.path.exists(target):
            if o.get("-f") != "remove":
                fail("conflict")
            os.unlink(target)
        if os.path.basename(src).startswith("slow"):
            import time
            time.sleep(3)  # lets tests check that browsing is not blocked by a long download
        shutil.copyfile(src, target)
    elif cmd == "upload":
        src, parent = r[0], local(r[1])
        if not os.path.isdir(parent):
            fail(f"Node not found: {r[1]}")
        target = os.path.join(parent, os.path.basename(src))
        strat = o.get("-f")
        if os.path.exists(target):
            if strat == "rename":
                base, ext = os.path.splitext(target)
                k = 1
                while os.path.exists(f"{base} ({k}){ext}"):
                    k += 1
                target = f"{base} ({k}){ext}"
            elif strat != "create-new-revision":
                fail("conflict")
        shutil.copyfile(src, target + ".up")
        os.replace(target + ".up", target)
        st = os.stat(src)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif cmd == "create-folder":
        p = os.path.join(local(r[0]), r[1].replace("/", "%2F"))
        if os.path.exists(p):
            fail("A file or folder with that name already exists")
        os.mkdir(p)
    elif cmd == "trash":
        p = local(r[0])
        if not os.path.exists(p):
            fail(f"Node not found: {r[0]}")
        os.makedirs(TRASH, exist_ok=True)
        os.replace(p, os.path.join(TRASH, f"{os.path.basename(p)}.{os.stat(p).st_ino}"))
    elif cmd == "rename":
        p = local(r[0])
        os.replace(p, os.path.join(os.path.dirname(p), r[1].replace("/", "%2F")))
    elif cmd == "move":
        p, parent = local(r[0]), local(r[1])
        os.replace(p, os.path.join(parent, os.path.basename(p)))
    else:
        fail(f"unsupported {a}")


main()
