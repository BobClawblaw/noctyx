"""What the Hugging Face mirror holds, as a tree: kind (models, datasets, spaces) › owner › repo › revision ›
files, with sizes at every level, read from the store itself (the hub cache layout the harvest writes:
<kind>s--<owner>--<name>/{blobs,snapshots/<commit>/<path> -> ../../blobs/<blob>,refs/<name>}).

A repo's size is its blobs (each stored once, however many revisions share it). A revision lists the files
it has, and the weight files it should have but does not: from its *.index.json weight map, or the
"-00003-of-00016" numbering. The mirror serves what it lacks from huggingface.co, so a gap is not an error,
but a model half on the NAS is not a model the cluster can load while huggingface.co is unreachable.
"""
import json
import os
import pathlib
import re
import time

KINDS = {"models--": "models", "datasets--": "datasets", "spaces--": "spaces"}
REFS_FILES = (".nx-refs.json", ".cm-refs.json")     # the second: written before the rename, still read
SEEN_FILE = ".nx-seen.json"
_SHARD = re.compile(r"^(?P<stem>.*?)-(?P<i>\d{5})-of-(?P<n>\d{5})(?P<ext>\.[A-Za-z0-9.]+)$")


def _refs_meta(rd):
    for n in REFS_FILES:
        try:
            return json.loads((rd / n).read_text())
        except (OSError, ValueError):
            continue
    return {}


def _expected(files, read, kind="models"):
    """Weight files a revision should have: every index's weight map, and each shard series' full count."""
    want = set()
    for rel in files:
        if rel.endswith(".index.json"):
            try:
                wm = json.loads(read(rel)).get("weight_map") or {}
            except (OSError, ValueError, AttributeError):
                continue
            base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
            want |= {base + v for v in wm.values() if isinstance(v, str)}
    series = {}
    for rel in files:
        m = _SHARD.match(rel)
        if m and int(m["n"]) <= 10000:
            series.setdefault((m["stem"], m["n"], m["ext"]), set()).add(int(m["i"]))
    for (stem, n, ext), have in series.items():
        # model shards count 1..N ("-00001-of-00016"), dataset parquet 0..N-1 ("-00000-of-00001"): the ends
        # present tell which; a series showing neither end follows its kind
        zero = 0 in have or (int(n) not in have and kind == "datasets")
        rng = range(0, int(n)) if zero else range(1, int(n) + 1)
        want |= {f"{stem}-{i:05d}-of-{n}{ext}" for i in rng}
    return want


def scan_repo(rd):
    kind = next((k for p, k in KINDS.items() if rd.name.startswith(p)), None)
    owner, _, name = rd.name.split("--", 1)[1].partition("--")
    if not name:                                   # a repo without an owner ("models--gpt2")
        owner, name = "", owner
    blobs = {}
    try:
        for e in os.scandir(rd / "blobs"):
            if e.is_file(follow_symlinks=False) and not e.name.endswith(".incomplete"):
                blobs[e.name] = e.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    refs = {}
    rdir = rd / "refs"
    for dp, _, fn in os.walk(rdir):
        for f in fn:
            try:
                refs[os.path.relpath(os.path.join(dp, f), rdir)] = (pathlib.Path(dp) / f).read_text().strip()
            except OSError:
                pass
    meta = _refs_meta(rd)
    try:
        seen = json.loads((rd / SEEN_FILE).read_text())
    except (OSError, ValueError):
        seen = {}
    snaps, sdir, newest = [], rd / "snapshots", 0
    for snap in sorted(os.scandir(sdir), key=lambda e: e.name) if sdir.is_dir() else []:
        files, missing_blob = {}, []
        for dp, _, fn in os.walk(snap.path):
            for f in fn:
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, snap.path)
                try:
                    tgt = os.readlink(p) if os.path.islink(p) else None
                except OSError:
                    tgt = None
                b = os.path.basename(tgt) if tgt else None
                if b and b in blobs:
                    files[rel] = blobs[b]
                elif b:
                    missing_blob.append(rel)             # the link is there, its blob is not
                else:
                    try:
                        files[rel] = os.stat(p).st_size
                    except OSError:
                        pass
        try:
            newest = max(newest, snap.stat(follow_symlinks=False).st_mtime)
        except OSError:
            pass

        def read(rel, base=snap.path):
            return open(os.path.join(base, rel)).read(4 << 20)
        want = _expected(files, read, kind)
        missing = sorted((want - set(files)) | set(missing_blob))
        snaps.append({"commit": snap.name, "refs": sorted(k for k, v in refs.items() if v == snap.name),
                      "bytes": sum(files.values()), "files": sorted(files.items()),
                      "missing": missing, "expected": len(want)})
    complete = all(not s["missing"] for s in snaps) and bool(snaps)
    return {"id": f"{owner}/{name}" if owner else name, "kind": kind, "owner": owner, "name": name, "dir": rd.name,
            "bytes": sum(blobs.values()), "blobs": len(blobs), "refs": refs,
            "from": sorted({m.get("host") for m in meta.values() if isinstance(m, dict) and m.get("host")}),
            "seen_on": seen, "updated": newest, "snapshots": snaps,
            "state": "complete" if complete else ("empty" if not snaps else "incomplete")}


def scan(store):
    hub = pathlib.Path(store) / "hub"
    t0 = time.time()
    repos = []
    for rd in sorted(hub.iterdir()) if hub.is_dir() else []:
        if rd.is_dir() and rd.name.startswith(tuple(KINDS)):
            try:
                repos.append(scan_repo(rd))
            except OSError:
                continue
    return {"ts": time.time(), "seconds": round(time.time() - t0, 1), "store": str(store),
            "bytes": sum(r["bytes"] for r in repos), "blobs": sum(r["blobs"] for r in repos), "repos": repos}


def cached(store, cache_file, max_age=600):
    """scan(), kept until a harvest has run since or max_age has passed (an NFS walk takes seconds)."""
    cache_file = pathlib.Path(cache_file)
    try:
        harvested = (pathlib.Path(store) / "status.json").stat().st_mtime
    except OSError:
        harvested = 0
    try:
        v = json.loads(cache_file.read_text())
        if time.time() - v["ts"] < max_age and v["ts"] > harvested:
            return v
    except (OSError, ValueError, KeyError):
        pass
    v = scan(store)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(v))
        tmp.replace(cache_file)
    except OSError:
        pass
    return v
