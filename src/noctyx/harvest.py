"""Hugging Face mirror harvest: copies model files from every node's HF caches into the shared mirror store,
verifying content hashes (sha256 for LFS, git sha1 otherwise) and merging snapshots and refs."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HF_DEFAULTS = {"store": "/srv/noctyx/hf-mirror", "port": 20995, "harvest": True, "write_through": False,
               "scan_roots": ["/"], "exclude_hosts": [], "bwlimit_mb": 0}
HF_UPSTREAM = "https://huggingface.co"
HF_LOCAL_TRIES = 8               # search results asked for a --local-dir download's commit, per search
HF_LOCAL_BUDGET = 24             # commit checks per --local-dir download and harvest
HF_LOCAL_RETRY = 7 * 86400       # a --local-dir download whose repo was not found is asked again after this


def _cm():
    from noctyx import orchestrator as cm
    return cm


def hf_cfg(c):
    return {**HF_DEFAULTS, **(c.get("hf_mirror") or {})}


def hf_skip(c):
    """Never scan the NAS mount (the store and the repositories live there) nor configured extras."""
    cm = _cm()
    hc = hf_cfg(c)
    roots = [str(pathlib.Path(hc["store"]))] + ([cm.repo_root(c)] if c.get("server", {}).get("repo_root") else [])
    try:
        common = os.path.commonpath(roots)
    except ValueError:
        common = "/"
    return ([common] if common not in ("/", "") else roots) + list(hc.get("skip") or [])


def hf_sources(c):
    ex = set(hf_cfg(c)["exclude_hosts"])
    return [h for h in c["hosts"] if h.get("enabled", True) is not False and h.get("address")
            and h["name"] not in ex]


def _hf_hash_ok(path, name):
    """True/False against the blob's own name; None when the name is not a hash we know."""
    if re.fullmatch(r"[0-9a-f]{64}", name):
        h = hashlib.sha256()
    elif re.fullmatch(r"[0-9a-f]{40}", name):
        h = hashlib.sha1(b"blob %d\0" % os.path.getsize(path))   # git blob id
    else:
        return None
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest() == name


def _hf_wait(c, a):
    """Yield to backups: never start a copy inside the backup window (from 15 min
    before it) or while anything holds the NAS spindle."""
    cm = _cm()
    if a.ignore_window:
        return
    win = ((c.get("schedule") or {}).get("defaults") or {}).get("system", {}).get("window")
    said = False
    while True:
        now = datetime.datetime.now()
        busy = cm.spindle_holder(cm.nas_name(c))
        inwin = win and (cm.in_window(now, win) or cm.in_window(now + datetime.timedelta(minutes=15), win))
        if not busy and not inwin:
            return
        if not said:
            print(f"  paused: {'backup window ' + win if inwin else cm.nas_name(c) + ' busy: ' + str(busy.get('what'))}"
                  " — resuming when clear", flush=True)
            said = True
        time.sleep(60)


def _hf_link(hub_repo, commit, rel, blob):
    p = hub_repo / "snapshots" / commit / rel
    if p.is_symlink() or p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(hub_repo / "blobs" / blob, p.parent), p)
    return True


def _hf_api(path):
    with urllib.request.urlopen(HF_UPSTREAM + path, timeout=20, context=_hf_ssl()) as r:
        return json.load(r)


def _hf_ssl():
    """Default-verify HF's TLS. macOS's urllib ignores the system keychain, so pin certifi's bundle when present."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _hf_local_repo(hub, d, memo):
    """The store's name (models--org--name) for the repo a --local-dir download came from, or None."""
    cm = _cm()
    commit = max(d["files"], key=lambda k: len(d["files"][k]))
    name = d["name"].lower()
    hints = list(d.get("hints") or ())
    have = sorted(p.parent.parent.name for p in hub.glob(f"*/snapshots/{commit}"))
    pick = [r for r in have if r.lower().endswith("--" + name)] or have
    if pick:
        return pick[0]
    key = f"{commit} {name} {' '.join(hints)}"
    if key in memo:
        return memo[key]
    neg = memo.setdefault("neg", {})
    if memo.get("offline") or time.time() - neg.get(key, 0) < HF_LOCAL_RETRY:
        return None
    tried = set()

    def ask(path):
        try:
            return cm._hf_api(path), None
        except urllib.error.HTTPError as e:
            return None, "offline" if e.code == 429 or e.code >= 500 else "no"
        except Exception:
            return None, "offline"

    def has(kind, rid):
        if (kind, rid.lower()) in tried or len(tried) >= HF_LOCAL_BUDGET:
            return ""
        tried.add((kind, rid.lower()))
        info, err = ask(f"/api/{kind}/{rid}/revision/{commit}")
        if err:
            return None if err == "offline" else ""
        rid = (info.get("id") or rid) if isinstance(info, dict) else rid
        return f"{kind}--{rid.replace('/', '--')}"

    def search():
        for h in hints:
            kind, _, rid = h.partition("/")
            if kind in ("models", "datasets") and "/" in rid:
                yield kind, rid
        words = re.split(r"[-_ ]", d["name"])
        names = [d["name"]] + ["-".join(words[:n]) for n in range(len(words) - 1, max(len(words) - 3, 0), -1)]
        for q in dict.fromkeys(names):
            for kind in ("models", "datasets"):
                if len(tried) >= HF_LOCAL_BUDGET:
                    return
                found, err = ask(f"/api/{kind}?search={urllib.parse.quote(q)}&limit=100")
                if err == "offline":
                    yield kind, None
                    return
                ids = [m.get("id", "") for m in found or []]
                for rid in sorted(ids, key=lambda i: i.split("/")[-1].lower() not in (name, q.lower()))[:HF_LOCAL_TRIES]:
                    yield kind, rid

    repo = ""
    for kind, rid in search():
        repo = has(kind, rid) if rid else None
        if repo is None:
            memo["offline"] = True
            return None
        if repo:
            break
    if repo:
        neg.pop(key, None)
    else:
        neg[key] = time.time()
    memo[key] = repo or None
    return memo[key]


def hf_harvest_host(c, h, a, hub):
    cm = _cm()
    hc = hf_cfg(c)
    mroots = h.get("scan_roots") or hc.get("scan_roots")
    starts = "~" if (h.get("os") == "macos" and not mroots) else " ".join(shlex.quote(r) for r in (mroots or hc["scan_roots"]))
    lst = (cm.NX_HOME / "cache" / "lister.py").read_text()
    skip = " ".join(shlex.quote(f"--skip={s}") for s in hf_skip(c))
    r = cm._run_on(c, h, f"python3 - {skip} {starts}", stdin=lst, timeout=900)
    try:
        repos = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f"lister failed: {(r.stderr or r.stdout).strip()[:200]}")
    st = {"repos": 0, "new_blobs": 0, "new_bytes": 0, "bad_hash": 0, "unverified": 0, "skipped": [],
          "unresolved": []}
    s_ = c["server"]["ssh"]
    nf = hub.parent / "unresolved.json"
    try:
        neg = {k: t for k, t in json.loads(nf.read_text()).items() if time.time() - t < HF_LOCAL_RETRY}
    except Exception:
        neg = {}
    memo = {"neg": neg}
    for d in repos:
        if d.get("local"):
            d["repo"] = _hf_local_repo(hub, d, memo)
            if not d["repo"]:
                st["unresolved"].append(d["root"])
                continue
        if a.repo and a.repo not in d["repo"]:
            continue
        st["repos"] += 1
        hr = hub / d["repo"]
        want = {b for files in d["files"].values() for b in files.values()}
        have = {p.name for p in (hr / "blobs").glob("*")} if (hr / "blobs").is_dir() else set()
        rel = {b: d["paths"][b] if d.get("local") else b for b in want}
        need = sorted(b for b in want - have if ".." not in rel[b].split("/"))
        if need:
            if any(ch in d["root"] for ch in " '\"\\$`"):
                st["skipped"].append(d["root"])
                continue
            _hf_wait(c, a)
            inc = hub.parent / ".incoming" / h["name"] / d["repo"]
            inc.mkdir(parents=True, exist_ok=True)
            src = f"{d['root']}/" if d.get("local") else f"{d['root']}/{d['repo']}/blobs/"
            cmd = ["rsync", "-t", "--copy-links", "--partial", "--files-from=-"]
            if hc["bwlimit_mb"]:
                cmd.append(f"--bwlimit={int(hc['bwlimit_mb'] * 1000)}")
            if cm.is_head(c, h):
                cmd += [src, str(inc) + "/"]
            else:
                cmd += ["-e", f"ssh -i {s_['key']} -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
                              " -o ServerAliveInterval=60",
                        "--rsync-path=sudo -n rsync", f"{h.get('ssh_user') or s_['user']}@{cm.addr(h)}:{src}", str(inc) + "/"]
            gb = sum(d["blobs"][b] for b in need) / 1e9
            print(f"  {h['name']}: {d['repo']} — {len(need)} file(s), {gb:.1f} GB", flush=True)
            rr = subprocess.run(cmd, input="\n".join(rel[b] for b in need) + "\n", capture_output=True, text=True)
            if rr.returncode not in (0, 23, 24):
                print(f"    rsync rc={rr.returncode}: {rr.stderr.strip()[-200:]}", flush=True)
            (hr / "blobs").mkdir(parents=True, exist_ok=True)
            for b in need:
                f = inc / rel[b]
                if not f.is_file() or f.stat().st_size != d["blobs"][b]:
                    continue
                ok = _hf_hash_ok(f, b)
                if ok is False:
                    st["bad_hash"] += 1
                    print(f"    hash MISMATCH, dropped: {d['repo']}/{b}", flush=True)
                    f.unlink()
                    continue
                st["unverified"] += ok is None
                os.replace(f, hr / "blobs" / b)
                st["new_blobs"] += 1
                st["new_bytes"] += d["blobs"][b]
            shutil.rmtree(inc, ignore_errors=True)
        have = {p.name for p in (hr / "blobs").glob("*")} if (hr / "blobs").is_dir() else set()
        for commit, files in d["files"].items():
            for rel_p, b in files.items():
                if b in have and ".." not in rel_p.split("/"):
                    _hf_link(hr, commit, rel_p, b)
        sf_ = hr / ".nx-seen.json"
        try:
            seen = json.loads(sf_.read_text())
        except Exception:
            seen = {}
        seen[h["name"]] = round(time.time())
        if hr.is_dir():
            sf_.write_text(json.dumps(seen))
        mf, old = hr / ".nx-refs.json", hr / ".cm-refs.json"
        try:
            meta = json.loads((mf if mf.exists() else old).read_text())
        except Exception:
            meta = {}
        changed = old.exists() and not mf.exists()
        for name, (commit, mtime) in d["refs"].items():
            if not (hr / "snapshots" / commit).is_dir() or ".." in name.split("/"):
                continue
            if name not in meta or mtime > meta[name]["mtime"]:
                meta[name] = {"commit": commit, "mtime": mtime, "host": h["name"]}
                (hr / "refs" / name).parent.mkdir(parents=True, exist_ok=True)
                (hr / "refs" / name).write_text(commit)
                changed = True
        if changed:
            mf.write_text(json.dumps(meta))
            old.unlink(missing_ok=True)
    nf.write_text(json.dumps(memo["neg"]))
    st["hub_offline"] = bool(memo.get("offline"))
    return st


def hf_totals(hub):
    repos = blobs = size = 0
    for r in hub.iterdir() if hub.is_dir() else []:
        if not r.name.startswith(("models--", "datasets--", "spaces--")):
            continue
        repos += 1
        for b in (r / "blobs").glob("*") if (r / "blobs").is_dir() else []:
            blobs += 1
            size += b.stat().st_size
    return {"repos": repos, "blobs": blobs, "bytes": size}


def cmd_hf_harvest(a):
    cm = _cm()
    c = cm.cfg()
    hc = hf_cfg(c)
    if not hc["harvest"] and not a.host:
        print("hf_mirror.harvest is off in the site config")
        return
    hub = pathlib.Path(hc["store"]) / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    sf = hub.parent / "status.json"
    try:
        status = json.loads(sf.read_text())
    except Exception:
        status = {"hosts": {}}
    try:
        with cm.spindle_lock("hf-harvest", what="hf-harvest"):
            bad = 0
            for h in hf_sources(c):
                if a.host and h["name"] != a.host:
                    continue
                t0 = time.time()
                try:
                    st = hf_harvest_host(c, h, a, hub)
                    st.update(ts=time.time(), seconds=round(time.time() - t0))
                    print(f"{h['name']}: {st['repos']} repos, {st['new_blobs']} new files "
                          f"({st['new_bytes'] / 1e9:.1f} GB){', ' + str(st['bad_hash']) + ' bad hash' if st['bad_hash'] else ''}"
                          f"{', skipped roots ' + ', '.join(st['skipped']) if st['skipped'] else ''}"
                          f"{', repo not found for ' + ', '.join(st['unresolved']) if st['unresolved'] else ''}"
                          f"{' (huggingface.co refused or unreachable: the rest are asked next time)' if st['hub_offline'] else ''}",
                          flush=True)
                except Exception as e:
                    bad += 1
                    st = {"ts": time.time(), "error": str(e)[:300]}
                    print(f"{h['name']}: FAILED {e}", flush=True)
                status["hosts"][h["name"]] = st
                status.update(ts=time.time(), totals=hf_totals(hub))
                sf.write_text(json.dumps(status))
            t = status.get("totals") or hf_totals(hub)
            print(f"store: {t['repos']} repos, {t['blobs']} files, {t['bytes'] / 1e12:.2f} TB")
            cm.audit("hf-harvest", host=a.host or "*", failed=bad, totals=t)
            sys.exit(1 if bad else 0)
    except cm.SpindleBusy:
        print(f"hf-harvest already running ({cm.spindle_holder('hf-harvest').get('since')}) — skipped")
