"""The head's own known states: the site config's history, and checkpoints of Noctyx itself to roll back to.

- Every rewrite of the site config (noctyx *-set, the console's Configure page) first keeps a copy of the file as
  it was, in <state>/config-history (the last KEEP_CONFIGS). noctyx config-history lists them and shows what
  changed; noctyx config-restore puts one back (keeping the current one first).
- A self-checkpoint records which Noctyx runs (its version, and its git commit when it runs from a checkout or was
  installed from one) with a copy of the site config, in <state>/self-checkpoints (the last KEEP_SELF). The
  console takes one when it starts on code it has not recorded yet, so the checkpoint before an upgrade is the
  version that ran until then. noctyx self-rollback goes back to one: the code (git checkout of its commit, and
  pip install for an installed Noctyx), optionally its site config, the head's units rendered again, and the
  long-running services restarted.
"""
import datetime
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

from noctyx import __version__
from noctyx.paths import CONFIG, P

STATE = pathlib.Path(P["state"])
CONFIG_HISTORY = STATE / "config-history"
SELF_DIR = STATE / "self-checkpoints"
KEEP_CONFIGS = 50
KEEP_SELF = 10
PKG = pathlib.Path(__file__).resolve().parent
LONG_RUNNING = ("console", "directory", "hf-mirror")


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _audit(event, **kw):
    try:
        from noctyx import orchestrator as cm
        cm.audit(event, **kw)
    except Exception:
        pass


def _copy_like(src, dst, like):
    """Copy src to dst with like's owner and mode (the config holds no secrets, but its group matters)."""
    shutil.copyfile(src, dst)
    st = os.stat(like)
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.chmod(dst, st.st_mode & 0o7777)


# ── the site config's history ───────────────────────────────────────────────
def save_config(why, path=None):
    """Keep a copy of the site config as it is now (before a rewrite). -> the copy's name, or None."""
    path = pathlib.Path(path or CONFIG)
    if not path.exists():
        return None
    CONFIG_HISTORY.mkdir(parents=True, exist_ok=True)
    name = f"{_stamp()}-{re.sub(r'[^a-z0-9_-]', '', why.lower())[:40] or 'change'}.json"
    _copy_like(path, CONFIG_HISTORY / name, path)
    for old in sorted(CONFIG_HISTORY.glob("*.json"))[:-KEEP_CONFIGS]:
        old.unlink(missing_ok=True)
    return name


def config_history():
    """[{name, ts, why, bytes}] newest first: the site config as it was before each rewrite."""
    out = []
    for f in sorted(CONFIG_HISTORY.glob("*.json"), reverse=True):
        m = re.match(r"(\d{8}T\d{6})\d*Z-(.*)\.json$", f.name)
        if not m:
            continue
        ts = datetime.datetime.strptime(m[1], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
        out.append({"name": f.name, "ts": ts.isoformat(timespec="seconds"), "ts_epoch": ts.timestamp(),
                    "why": m[2], "bytes": f.stat().st_size})
    return out


def _named(name):
    if not re.fullmatch(r"\d{8}T\d{6}\d*Z-[a-z0-9_-]*\.json", name or ""):
        raise ValueError(f"no such copy: {name}")
    f = CONFIG_HISTORY / name
    if not f.exists():
        raise ValueError(f"no such copy: {name}")
    return f


def config_diff(name):
    """What restoring that copy would change: a unified diff from the current file to it."""
    f = _named(name)
    cur = pathlib.Path(CONFIG).read_text().splitlines(keepends=True)
    return "".join(difflib.unified_diff(cur, f.read_text().splitlines(keepends=True), "site config now",
                                        f"site config {name}"))


def config_restore(name, by=None):
    """Put a kept copy back (the current file is kept first). Refuses a copy that is not valid JSON."""
    f = _named(name)
    json.loads(f.read_text())
    kept = save_config("before-restore")
    tmp = pathlib.Path(f"{CONFIG}.tmp")
    _copy_like(f, tmp, CONFIG)
    os.replace(tmp, CONFIG)
    _audit("config-restore", copy=name, kept=kept, by=by)
    return kept


# ── Noctyx itself ───────────────────────────────────────────────────────────
def _git(root, *args, timeout=60):
    r = subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root), *args], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _git_root(path):
    rc, out = _git(path, "rev-parse", "--show-toplevel")
    return pathlib.Path(out) if rc == 0 and out else None


def identity():
    """Which Noctyx runs: {version, mode: "checkout" | "installed", code, git: {root, sha, branch, dirty}?, src?}.
    checkout: the package runs from a git work tree (PYTHONPATH=src). installed: from site-packages; its source
    checkout is NOCTYX_SRC (default /opt/noctyx-src) when that is a git work tree."""
    root = _git_root(PKG)
    out = {"version": __version__, "code": str(PKG), "python": sys.executable}
    if root:
        out["mode"], src = "checkout", root
    else:
        out["mode"] = "installed"
        cand = pathlib.Path(os.environ.get("NOCTYX_SRC", "/opt/noctyx-src"))
        src = _git_root(cand) if cand.is_dir() else None
    if src:
        rc, sha = _git(src, "rev-parse", "HEAD")
        _, branch = _git(src, "rev-parse", "--abbrev-ref", "HEAD")
        _, dirty = _git(src, "status", "--porcelain", "--untracked-files=no")
        if rc == 0:
            out["git"] = {"root": str(src), "sha": sha, "branch": branch, "dirty": bool(dirty)}
    return out


def _same_code(a, b):
    ga, gb = a.get("git") or {}, b.get("git") or {}
    return (ga.get("sha"), a.get("version")) == (gb.get("sha"), b.get("version"))


def self_checkpoints():
    """[{id, ts, note, identity}] newest first."""
    out = []
    for d in sorted((x for x in SELF_DIR.glob("*") if x.is_dir()), reverse=True):
        try:
            m = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        out.append({**m, "id": d.name})
    return out


def self_checkpoint(note="", by=None):
    """Record the Noctyx that runs now, with a copy of the site config. -> its id."""
    SELF_DIR.mkdir(parents=True, exist_ok=True)
    cid = _stamp()
    d = SELF_DIR / cid
    d.mkdir()
    meta = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "note": note,
            "identity": identity(), "by": by}
    if pathlib.Path(CONFIG).exists():
        _copy_like(CONFIG, d / "site.json", CONFIG)
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    for old in sorted(x for x in SELF_DIR.glob("*") if x.is_dir())[:-KEEP_SELF]:
        shutil.rmtree(old, ignore_errors=True)
    _audit("self-checkpoint", id=cid, note=note, version=__version__, sha=(meta["identity"].get("git") or {}).get("sha"))
    return cid


def checkpoint_if_new(note="started on code not recorded before"):
    """The console's start: a checkpoint when the code differs from the newest one. -> its id or None."""
    try:
        last = (self_checkpoints() or [None])[0]
        if last and _same_code(last.get("identity") or {}, identity()):
            return None
        return self_checkpoint(note)
    except OSError:
        return None


def rollback_plan(to=None, config=False, units=True, restart=True):
    """-> {target, steps: [text], problems: [text]}: what self_rollback would do."""
    cur = identity()
    cps = self_checkpoints()
    if to:
        target = next((x for x in cps if x["id"] == to), None)
        if not target:
            return {"target": None, "steps": [], "problems": [f"no self-checkpoint {to} (noctyx self-checkpoint --list)"]}
    else:
        target = next((x for x in cps if not _same_code(x.get("identity") or {}, cur)), None)
        if not target:
            return {"target": None, "steps": [], "problems": ["every self-checkpoint is the code that runs now: nothing "
                                                              "to go back to"]}
    ti, problems, steps = target.get("identity") or {}, [], []
    tg, cg = ti.get("git") or {}, cur.get("git") or {}
    if not tg.get("sha"):
        problems.append("that checkpoint has no git commit (Noctyx was not run or installed from a checkout then): "
                        "go back by hand (the upgrade guide's Roll back)")
    elif not cg.get("root"):
        problems.append("Noctyx does not run from a git checkout here, and NOCTYX_SRC names none: go back by hand")
    else:
        if cg.get("dirty"):
            problems.append(f"{cg['root']} has uncommitted changes: commit or stash them first")
        steps.append(f"git checkout --detach {tg['sha'][:12]} in {cg['root']} (now {cg.get('sha', '?')[:12]} on "
                     f"{cg.get('branch')}; back with: git checkout {cg.get('branch')})")
        if cur.get("mode") == "installed":
            steps.append(f"{sys.executable} -m pip install {cg['root']}")
    if config:
        if (SELF_DIR / target["id"] / "site.json").exists():
            steps.append(f"the site config as it was then (the current one kept in {CONFIG_HISTORY})")
        else:
            problems.append("that checkpoint has no copy of the site config")
    if units:
        steps.append("noctyx install-head --apply (the head's units, rendered by that code)")
    if restart:
        steps.append("restart " + ", ".join(LONG_RUNNING))
    return {"target": target, "current": cur, "steps": steps, "problems": problems}


def self_rollback(to=None, config=False, units=True, restart=True, by=None, log=print):
    """Go back to a self-checkpoint (rollback_plan says what). -> 0 or an exit code."""
    plan = rollback_plan(to, config, units, restart)
    if plan["problems"]:
        for p in plan["problems"]:
            log(f"✗ {p}")
        return 2
    target, cur = plan["target"], plan["current"]
    root = cur["git"]["root"]
    sha = target["identity"]["git"]["sha"]
    self_checkpoint(f"before self-rollback to {target['id']}", by=by)
    log(f"· git checkout --detach {sha[:12]} in {root}")
    rc, out = _git(root, "checkout", "--detach", sha)
    if rc:
        log(f"✗ {out[-300:]}")
        return 1
    if cur.get("mode") == "installed":
        log(f"· pip install {root}")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", root], capture_output=True, text=True)
        if r.returncode:
            log(f"✗ pip: {(r.stdout + r.stderr)[-300:]}")
            return 1
    if config:
        kept = save_config("before-self-rollback")
        tmp = pathlib.Path(f"{CONFIG}.tmp")
        _copy_like(SELF_DIR / target["id"] / "site.json", tmp, CONFIG)
        os.replace(tmp, CONFIG)
        log(f"· the site config of {target['id']} is back (the one before it: {kept})")
    env = {**os.environ}
    if cur.get("mode") == "checkout":
        env["PYTHONPATH"] = str(pathlib.Path(root) / "src")
    if units:
        log("· noctyx install-head --apply")
        r = subprocess.run([sys.executable, "-m", "noctyx.cli", "install-head", "--apply"], env=env,
                           capture_output=True, text=True, timeout=600)
        if r.returncode:
            log(f"✗ install-head: {(r.stdout + r.stderr)[-300:]}")
            return 1
    _audit("self-rollback", to=target["id"], sha=sha, from_sha=cur["git"].get("sha"), config=config, by=by)
    if restart:
        from noctyx import hostos
        for n in LONG_RUNNING:
            if hostos.IS_MAC:
                argv = ["launchctl", "kickstart", "-k", f"system/{P['launchd_prefix']}.{n}"]
            else:
                if subprocess.run(["systemctl", "is-active", "--quiet", f"{P['unit_prefix']}-{n}"]).returncode:
                    continue
                argv = ["systemctl", "restart", f"{P['unit_prefix']}-{n}"]
            log(f"· {' '.join(argv)}")
            subprocess.run(argv, capture_output=True, timeout=120)
    log(f"✓ Noctyx is back at {target['id']} ({target['identity'].get('version')}, {sha[:12]})")
    return 0
