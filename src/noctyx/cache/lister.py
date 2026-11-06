#!/usr/bin/env python3
"""Runs ON a cluster host (piped over ssh by `noctyx hf-harvest`): finds every
Hugging Face hub cache (any directory holding models--* / datasets--* repos) and
prints one JSON document describing it. Read-only. Python 3.8+, Linux and macOS.
A repo's blob may be a link into the hub's shared-blob store; its size is the file's, and the harvest's
rsync copies what the link points to.

  [{"root": "/root/.cache/huggingface/hub", "repo": "models--org--name",
    "refs": {"main": ["<commit>", <mtime>]},
    "files": {"<commit>": {"path/in/repo": "<blob>"}},
    "blobs": {"<blob>": <size>}}, ...]

It also finds downloads made with `--local-dir`: a directory holding .cache/huggingface/download/, where
huggingface_hub keeps each file's commit and etag. Those print as {"root": "<dir>", "local": true,
"name": "<dir's name>", "files": ..., "blobs": ..., "paths": {"<blob>": "path/in/repo"}}; the metadata does
not name the repo, so the harvest works that out on the head.
"""
import json
import os
import sys

MAXDEPTH = 7
SKIP = {"/proc", "/sys", "/dev", "/run", "/snap", "/tmp", "/var/lib/docker", "/var/lib/containerd",
        "/System", "/Library", "/private", "/Volumes"}
SKIPNAMES = {"node_modules", ".git", "site-packages", ".Trash", "Library", "__pycache__", "blobs", "snapshots"}
PREFIXES = ("models--", "datasets--", "spaces--")


def roots(starts):
    """The hub caches under starts, and the --local-dir download directories."""
    found, local = set(), set()
    for s in starts:
        try:
            dev = os.stat(s).st_dev
        except OSError:
            continue
        stack = [(s, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                ents = list(os.scandir(d))
            except OSError:
                continue
            for e in ents:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if e.name.startswith(PREFIXES):
                        found.add(d)
                        continue
                    if e.name == ".cache" and os.path.isdir(os.path.join(e.path, "huggingface", "download")):
                        local.add(d)
                    p = e.path
                    if p in SKIP or e.name in SKIPNAMES or depth + 1 >= MAXDEPTH or e.stat(follow_symlinks=False).st_dev != dev:
                        continue
                    stack.append((p, depth + 1))
                except OSError:
                    continue
    return sorted(found), sorted(local)


def describe(root, repo):
    rd = os.path.join(root, repo)
    out = {"root": root, "repo": repo, "refs": {}, "files": {}, "blobs": {}}
    bd = os.path.join(rd, "blobs")
    top = os.path.realpath(root) + os.sep
    try:
        for e in os.scandir(bd):
            if e.name.endswith(".incomplete"):
                continue
            # a blob is a file, or (huggingface_hub's shared-blob store, hub/blobs/<xx>/<hash>) a link to one
            # inside the same hub cache; a link out of it is not followed
            if e.is_file(follow_symlinks=False) or (e.is_symlink() and e.is_file()
                                                    and os.path.realpath(e.path).startswith(top)):
                out["blobs"][e.name] = e.stat().st_size
    except OSError:
        pass
    refd = os.path.join(rd, "refs")
    for dp, _, fns in os.walk(refd):
        for f in fns:
            p = os.path.join(dp, f)
            try:
                out["refs"][os.path.relpath(p, refd)] = [open(p).read().strip(), os.path.getmtime(p)]
            except OSError:
                pass
    sd = os.path.join(rd, "snapshots")
    try:
        commits = [e.name for e in os.scandir(sd) if e.is_dir()]
    except OSError:
        commits = []
    for c in commits:
        base = os.path.join(sd, c)
        files = {}
        for dp, _, fns in os.walk(base):
            for f in fns:
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    b = os.path.basename(os.readlink(p))
                    if b in out["blobs"]:            # only files whose blob is really here
                        files[os.path.relpath(p, base)] = b
        if files:
            out["files"][c] = files
    return out


def describe_local(root):
    """A --local-dir download: each file huggingface_hub wrote a metadata file for (commit, etag, time) and
    has not been changed since (its own rule: mtime at most a second past the recorded time). The etag is the
    blob's name, the same hash a hub cache names it by; any other etag is left out, as it cannot be checked."""
    md = os.path.join(root, ".cache", "huggingface", "download")
    out = {"root": root, "local": True, "name": os.path.basename(root), "refs": {}, "files": {}, "blobs": {},
           "paths": {}}
    for dp, _, fns in os.walk(md):
        for f in fns:
            if not f.endswith(".metadata"):
                continue
            rel = os.path.relpath(os.path.join(dp, f[:-len(".metadata")]), md)
            try:
                with open(os.path.join(dp, f)) as fh:
                    commit, etag, ts = (fh.read().split("\n") + ["", "", ""])[:3]
                st = os.stat(os.path.join(root, rel), follow_symlinks=False)
                ts = float(ts)
            except (OSError, ValueError):
                continue
            hexes = set("0123456789abcdef")
            if (len(commit) != 40 or not set(commit) <= hexes or len(etag) not in (40, 64) or not set(etag) <= hexes
                    or not os.path.isfile(os.path.join(root, rel)) or os.path.islink(os.path.join(root, rel))
                    or st.st_mtime - 1 > ts):
                continue
            out["files"].setdefault(commit, {})[rel] = etag
            out["blobs"][etag] = st.st_size
            out["paths"].setdefault(etag, rel)
    return out


def main():
    # --skip=PATH: never descend there (the NAS mount, so the mirror does not scan its own store)
    for a in sys.argv[1:]:
        if a.startswith("--skip="):
            SKIP.add(a[7:].rstrip("/") or "/")
    starts = [a for a in sys.argv[1:] if not a.startswith("--skip=")] or ["/"]
    res = []
    hubs, local = roots(starts)
    for r in hubs:
        for e in sorted(os.listdir(r)):
            if e.startswith(PREFIXES) and os.path.isdir(os.path.join(r, e)):
                d = describe(r, e)
                if d["files"]:
                    res.append(d)
    for r in local:
        d = describe_local(r)
        if d["files"]:
            res.append(d)
    json.dump(res, sys.stdout)


if __name__ == "__main__":
    main()
