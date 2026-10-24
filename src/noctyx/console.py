#!/usr/bin/env python3
"""Noctyx console (head node, stdlib only).

Movable instrument panels: free-positioned
windows with dotted grab grips, native corner resize, layout persisted in
localStorage — and NO page-level refresh: every panel polls its own endpoint on its
own clock and repaints only its own DOM node.

Endpoints (GET read-only and tokenless BY DESIGN — same data the directory service
already publishes; POST whitelisted actions, tokened unless NOCTYX_CONSOLE_AUTH=0):

  GET  /                      the dashboard
  GET  /cluster               systems-only view: every node's live stats + LLM servers, nothing else
  GET  /config                backup coverage settings (docker volumes, database dumps, excludes, retention)
  GET  /api/v1/recipes        recipe slots (what each serves, live), history, the catalog
  GET  /api/v1/policy         backup_policy + per-host effective values + discovered databases
  GET  /api/v1/overview       hosts + facts + snapshots + network + auth mode
  GET  /api/v1/facts/<host>   one machine (?age=<s> max cache age to accept)
  GET  /api/v1/capacity       tier free/used
  GET  /api/v1/audit?n=15     audit.jsonl tail
  GET  /api/v1/probes         last probe run (persisted by the probe action)
  GET  /api/v1/schedule       per host/profile: last, next due, interval, RPO health
  GET  /api/v1/runs           recent detached jobs: running / ok / failed / died
  GET  /api/v1/snapshots/<h>  every snapshot of a host with per-run stats (files new/changed, data added)
  GET  /api/v1/browse/<h>/<snap>?path=/dir   directory listing inside a snapshot
  GET  /api/v1/find/<h>?q=<glob>[&snapshot=] find files across (or in) snapshots
  GET  /api/v1/download/<h>/<snap>?path=/f[&dir=1][&t=ticket]  the file's bytes (or a tar of the dir); operator
  POST /api/v1/download-ticket {host,snapshot,path,dir}  a 60 s signed ticket for one download (token mode)
  GET  /api/v1/growth         used-space trend on the NAS -> GB/day, days until full
  GET  /metrics               Noctyx's own Prometheus metrics (backup health, RAID, endpoint…)
  GET  /api/v1/alerts         active alerts (Alertmanager) + recent alert history (+ assistant triage)
  POST /api/v1/alert-silence  {fingerprint,minutes} -> silence exactly one firing alert
  POST /api/v1/alert-unsilence {id}                -> end a silence early (alert re-fires if true)
  GET  /api/v1/digest         morning brief written by the local LLM (?refresh=1 to regenerate)
  POST /api/v1/explain        {kind:"hygiene",host,id} -> the local LLM explains a finding (cached)
  POST /api/v1/alert-hook     Alertmanager webhook (loopback only) -> audit + history
  GET  /api/v1/metrics        live per-host numbers + 30-min sparklines (Prometheus)
  GET  /api/v1/metrics/<h>?tab=overview|network|storage|gpu|health   one report tab
  GET  /api/v1/job/<id>       one UI job
  GET  /api/v1/versions       every critical component per machine: installed, pinned, latest, upgrade command
  GET  /api/v1/heal           the repair loop: open problems, the last day's repairs, the changes ledger, the policy
  GET  /api/v1/heal/log?n=&host=&check=&all=&q=&run=   its runs, newest first: each check, decision and command
  GET  /api/v1/changes        every change Noctyx made, what is in effect, its checkpoint, whether it can be undone
  GET  /api/v1/checkpoints    the checkpoint snapshots per machine (cached 5 min; ?refresh=1)
  GET  /api/v1/config-history the site config's kept copies (?diff=NAME: what restoring it would change)
  GET  /api/v1/self-checkpoints  the recorded states of Noctyx itself, and what self-rollback would do
  POST /api/v1/action         whitelisted orchestrator argv
  POST /api/v1/ask            {text, sid?, tunit?, tz?} -> Ask Noctyx: a question to the assistant, which looks things up (copilot.py)
  GET  /api/v1/ask/<sid>?since=<rev>      that conversation since a revision

Control model: fixed argv whitelist, host/profile validated
against the site config, audit every trigger, one job at a time + orchestrator locks.
"""
from __future__ import annotations
import base64, json, os, re, secrets, signal, ssl, subprocess, sys, threading, time
import datetime, pathlib, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from noctyx import orchestrator as CM                # noqa: E402  the console never disagrees with `tick`
from noctyx.paths import P, CONFIG                   # noqa: E402
from noctyx import accounts as AC                    # noqa: E402
from noctyx import hostos                            # noqa: E402
from noctyx import copilot as COP                    # noqa: E402

NX_HOME = pathlib.Path(__file__).resolve().parent
CFG_PATH = CONFIG
CFG = json.load(open(CFG_PATH))
S = CFG["server"]
_CFG_MTIME = os.path.getmtime(CFG_PATH)


def _reload_cfg():
    """Pick up an edited site config (noctyx init, policy-set, a hand edit) without a restart. The TLS files
    and the port stay as they were at start: those need one."""
    global CFG, S, _CFG_MTIME
    try:
        m = os.path.getmtime(CFG_PATH)
        if m != _CFG_MTIME:
            CFG, _CFG_MTIME = json.load(open(CFG_PATH)), m
            S = CFG["server"]
    except (OSError, ValueError):
        pass
CERT, KEY, CA = S["tls"]["cert"], S["tls"]["key"], S["tls"]["ca"]
PORT = int(os.environ.get("NOCTYX_CONSOLE_PORT", S.get("ui_port", 20996)))
TOKEN_FILE = P["ui_token"]
AUDIT = pathlib.Path(P["audit"])
ORCH = str(pathlib.Path(CM.__file__).resolve())     # actions run the orchestrator as a file
UI_STATE = pathlib.Path(P["console_state"])
UNIT = P["unit_prefix"]


def _addr(h):
    """Reachable address of a host: a literal IP as-is; a name (the Mac, DHCP + dual-homed)
    as llm-discover last pinned it to its wired interface (addresses.json), else the name."""
    a = h.get("address")
    if not a or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", a):
        return a
    try:
        return json.load(open(UI_STATE / "addresses.json")).get(h["name"], {}).get("ip") or a
    except Exception:
        return a

# Whitelist: UI name -> argv builder. NO user text is ever interpolated into the
# command beyond values validated against the site config itself.
ACTIONS = {
    "probe":      lambda: [ORCH, "probe"],
    "status":     lambda: [ORCH, "status"],
    "capacity":   lambda: [ORCH, "capacity"],
    "snapshots":  lambda: [ORCH, "snapshots"],
    "directory":  lambda: [ORCH, "directory", "--regen"],
    "backup":     lambda: [ORCH, "backup", "--profile", "system"],
    "verify":     lambda: [ORCH, "verify"],
    "copy":       lambda: [ORCH, "copy"],
    "prune":      lambda: [ORCH, "prune"],   # dry-run only; --apply is CLI-side by design
    "schedule":   lambda: [ORCH, "tick", "--dry-run"],   # read-only: what is due, and why
    "hygiene":    lambda: [ORCH, "hygiene"],             # report every host, fix nothing
    # fix specific items on one host; ids are validated against the last report
    "hygieneFix": lambda p: [ORCH, "hygiene", "--host", p["host"], "--fix", ",".join(p["items"])],
    "backupHost": lambda p: [ORCH, "backup", "--host", p["host"], "--profile", p.get("profile", "system")],
    "verifyHost": lambda p: [ORCH, "verify", "--host", p["host"]],
    "nasRest":    lambda: [ORCH, "nas-rest", "restart"],   # the repository endpoint on the NAS
    # the orchestrator re-checks every blocker (backup/restore writing, dpkg, jobs) before it acts
    "reboot":     lambda p: [ORCH, "reboot", "--host", p["host"], "--yes"],
    "rebootFinish": lambda p: [ORCH, "reboot", "--host", p["host"], "--finish"],   # head node, on the new boot
    # restore is NEVER in place from the UI: staging on the NAS, or a fresh dir on the host
    "restore":    lambda p: [ORCH, "restore", "--host", p["host"], "--snapshot", p["snapshot"],
                             "--path", p["path"], "--to", p.get("to", "staging")],
    # schedule edits go through the CLI (which validates again and audits); the
    # console never writes the site config itself
    # coverage policy (UI › Config): validated here AND again by the CLI, which writes
    # the site config, audits, and pushes the rendered files to every host
    "setPolicy":  lambda p: [ORCH, "policy-set", "--json", json.dumps({"policy": p["policy"], "retention": p.get("retention"),
                                                                       "hf_mirror": p.get("hf_mirror")})],
    "hfHarvest":  lambda: [ORCH, "hf-harvest"],
    "setAssistant": lambda p: [ORCH, "assistant-set", "--json", json.dumps(p["assistant"])],
    "setConsoleDefaults": lambda p: [ORCH, "console-set", "--json", json.dumps(p["settings"])],
    # recipe deployment (UI › /recipes): slot/recipe/ref validated against the site config here; the
    # orchestrator plans again, takes the slot + GPU locks, and switches back if the new one fails
    "recipeDeploy": lambda p: [ORCH, "recipe-deploy", "--slot", p["slot"], "--recipe", p["recipe"]]
                              + (["--ref", p["ref"]] if p.get("ref") else [])
                              + (["--prepare-only"] if p.get("prepare_only") else ["--yes"]),
    "recipeDiscover": lambda: [ORCH, "recipe-discover"],     # read-only: GitHub + leaderboard search
    "recipeStop":     lambda p: [ORCH, "recipe-stop", "--slot", p["slot"], "--yes"],
    "recipeStart":    lambda p: [ORCH, "recipe-start", "--slot", p["slot"]],
    "recipeRollback": lambda p: [ORCH, "recipe-rollback", "--slot", p["slot"], "--yes"],
    "recipeCatalog":  lambda p: [ORCH, "recipe-catalog", "--name", p["name"]]
                                + (["--remove"] if p.get("remove") else ["--json", json.dumps(p["entry"])]),
    "policyApply": lambda: [ORCH, "policy-apply"],
    # a component alert's buttons: Noctyx's own upgrade (the orchestrator plans it again from the last versions
    # check, refuses what Noctyx does not install, and checks the host again after), or skipping one release
    "upgradeComponent": lambda p: [ORCH, "upgrade", "--host", p["host"], "--component", p["component"]],
    "upgradeEverywhere": lambda p: [ORCH, "upgrade", "--host", "all", "--component", p["component"]],
    # the repair loop now, not at its next 5-minute turn (noctyx heal)
    "healNow": lambda: [ORCH, "heal", "--trigger", "console"],
    "healUnfreeze": lambda: [ORCH, "heal", "--unfreeze"],
    # going back: undo a change (checked, itself undoable), a checkpoint now, the policy, a kept site config
    "revertChange": lambda p: [ORCH, "revert", p["id"], "--yes"] + (["--keep-data"] if p.get("keep_data") else []),
    "takeCheckpoint": lambda p: [ORCH, "checkpoint", "--host", p["host"]],
    "setCheckpoints": lambda p: [ORCH, "checkpoint-set", "--json", json.dumps(p["checkpoints"])],
    "configRestore": lambda p: [ORCH, "config-restore", p["name"], "--yes"],
    "setHeal": lambda p: [ORCH, "heal-set", "--json", json.dumps(p["heal"])],
    "skipRelease": lambda p: [ORCH, "versions-skip", "--host", p["host"], "--component", p["component"]]
                             + (["--undo"] if p.get("undo") else ["--release", p["release"]]),
    "dumpsNow":   lambda p: [ORCH, "dumps"] + (["--host", p["host"]] if p.get("host") else []),
    "setSchedule": lambda p: [ORCH, "schedule-set", "--profile", p["profile"]]
                             + (["--host", p["host"]] if p.get("host") else [])
                             + ({"disable": ["--disable"], "reset": ["--reset"]}.get(p.get("mode"))
                                or ((["--every", p["every"]] if p.get("every") else [])
                                    + (["--window", p["window"]] if p.get("window") else []))),
}
VALID_HOSTS = {h["name"] for h in CFG["hosts"] if h.get("enabled", True) is not False}
VALID_PROFILES = set(CFG.get("profiles", {}).keys())
# host-REQUIRED actions: their builders index p["host"] directly; missing would KeyError
HOST_REQUIRED = {"backupHost", "verifyHost", "reboot", "rebootFinish"}
RECIPE_ACTIONS = {"recipeDeploy", "recipeStop", "recipeStart", "recipeRollback", "recipeCatalog"}
PARAM_ACTIONS = HOST_REQUIRED | RECIPE_ACTIONS | {"upgradeComponent", "upgradeEverywhere", "skipRelease", "setAssistant", "setHeal", "revertChange", "takeCheckpoint", "setCheckpoints", "configRestore", "setConsoleDefaults", "setSchedule", "restore", "hygieneFix", "setPolicy", "dumpsNow"}
# Only these wait for the one-long-job slot; everything else (status, schedule
# edits, probes) runs at once instead of queueing silently behind a 3-hour seed.
LONG_ACTIONS = {"backup", "verify", "copy", "prune", "backupHost", "verifyHost", "restore", "hygieneFix", "upgradeComponent",
                "upgradeEverywhere", "revertChange", "takeCheckpoint"}
# how long a job may run before the console stops it (SIGTERM first: a change rolls itself back); default 3 days
ACTION_TIMEOUT = {"upgradeComponent": 90 * 60, "upgradeEverywhere": 4 * 3600, "healNow": 90 * 60, "revertChange": 2 * 3600,
                  "takeCheckpoint": 2 * 3600}
# a failed one of these cancels the queued ones for the same component: they would fail the same way
CANCEL_ON_FAIL = {"upgradeComponent", "upgradeEverywhere"}
HYGIENE_FILE = UI_STATE / "hygiene.json"


def policy_summary():
    """UI › Config: the stored policy, what each host ends up with, what the dump
    hook found there (and how its last run went), retention, the baseline lists."""
    c = CM.cfg()
    pol = CM.policy(c)
    names = [h["name"] for h in CM.policy_hosts(c)]
    base, site = NX_HOME / "agent", pathlib.Path(P["etc"])
    rd = lambda n: (base / n).read_text() if (base / n).exists() else ""   # noqa: E731
    extra = {f.name[len("exclusions-"):-len(".conf")]: f.read_text() for f in sorted(site.glob("exclusions-*.conf")) if f.name != "exclusions-common.conf"}
    return {"policy": pol, "stored": "backup_policy" in c, "hosts": names,
            "effective": {n: CM.effective_policy(c, n) for n in names},
            "profiles": {n: sorted(CM.effective_schedule(c, n)) for n in names},   # what actually runs
            "dumps": CM._dumps_state(), "retention": c.get("retention", {}),
            "baseline": {"common": CM.common_exclusions(c), "profiles": extra},
            "not_covered": [h["name"] for h in c["hosts"] if h.get("enabled", True) is not False
                            and h.get("address") and h["name"] not in names],
            "hf": hf_summary(c), "assistant": assistant_summary(), "heal": heal_config(c),
            "checkpoints": checkpoint_config(c)}


def checkpoint_config(c):
    from noctyx import checkpoints, selfstate
    return {"stored": c.get("checkpoints") or {}, "policy": checkpoints.policy(c), "history": selfstate.config_history()}


def changes_view():
    """Every change, newest first, with what is in effect and whether `noctyx revert` would take it now."""
    from noctyx import change
    recs = change.ledger(2000)
    st = change.state_of(recs)
    top = {v[-1]["id"] for v in change.stacks(recs).values() if v}
    return {"changes": [{**{k: v for k, v in r.items() if k != "snap"}, "state": st.get(r["id"]),
                         "revertible": r["id"] in top and not change.undoes(r)} for r in reversed(recs[-200:])]}


_CHECKPOINTS = {}


def checkpoints_view(refresh=False):
    """The checkpoint snapshots per machine: an ssh and a restic listing each, so cached 5 minutes."""
    from noctyx import checkpoints
    if refresh or time.time() - _CHECKPOINTS.get("ts", 0) > 300:
        _CHECKPOINTS.update(ts=time.time(), hosts=checkpoints.listing(CM.cfg()))
    return {"ts": _CHECKPOINTS["ts"], "hosts": _CHECKPOINTS["hosts"]}


def heal_errors(blk):
    from noctyx import heal
    errs = heal.validate_policy(blk)[1]
    slots = set(CM.recipes_cfg(CM.cfg())["slots"])
    return errs + [f"restart_slots: {n} is not a slot" for n in ((blk or {}).get("restart_slots") or [])
                   if isinstance(n, str) and n not in slots]


def heal_config(c):
    """Configure › Self-repair: the stored block, what is in effect, and what each playbook does."""
    from noctyx import heal
    return {"stored": c.get("heal") or {}, "policy": heal.policy(c),
            "playbooks": {k: {"default": m, "does": d} for k, (m, d) in heal.PLAYBOOKS.items()},
            "never_units": list(heal.NEVER_UNITS), "upgradable": list(heal.UPGRADABLE),
            "slots": sorted(CM.recipes_cfg(c)["slots"])}


def hf_summary(c):
    hc = CM.hf_cfg(c)
    try:
        st = json.loads((pathlib.Path(hc["store"]) / "status.json").read_text())
    except Exception:
        st = {}
    run = [u for u in CM._active_job_units() if u.startswith(f"{UNIT}-hf-harvest-")]
    return {**hc, "status": st, "sources": [h["name"] for h in c["hosts"] if h.get("enabled", True) is not False
                                            and h.get("address")],
            "endpoint": f"http://{c['server']['address']}:{hc['port']}",
            "service": "active" if hostos.service_active(f"{P['mirror_unit']}.service", P["launchd_prefix"]) else "inactive",
            "harvesting": bool(run)}


def hygiene_report():
    try:
        return json.loads(HYGIENE_FILE.read_text())
    except Exception:
        return {"ts": 0, "hosts": {}}
_SNAP_RE = re.compile(r"^([0-9a-f]{8,64}|latest)$")


def _ok_path(v):
    return isinstance(v, str) and v.startswith("/") and "\0" not in v and len(v) < 4096
_EVERY_RE = re.compile(r"^\d{1,4}[mhdw]$")
_WINDOW_RE = re.compile(r"^(([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d|any)$")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
# Cross-restart persistence of the job table (frozen-badge fix, 2026-09-26):
# write-through on start/finish; anything still open at boot is reaped.
JOBS_FILE = pathlib.Path(P["console_state"]) / "console-jobs.json"
RUNNING: threading.Semaphore | None = None   # set in main
RUNNING_JOB: dict | None = None              # the long job holding RUNNING
_NOLOCK = __import__("contextlib").nullcontext()
_JOB_THREADS: list[threading.Thread] = []    # worker threads, joined at graceful exit
_GRACEFUL = False


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def auth_on():
    return os.environ.get("NOCTYX_CONSOLE_AUTH", "1") != "0"


def console_mode():
    """open: anyone on the LAN may look and act (sign-in optional: per-user settings, a name in the audit);
    accounts: sign in for everything, roles decide what you may do; token: the old bearer token for actions."""
    if not auth_on():
        return "open"
    return "accounts" if AC.users() else "token"


# what each action needs: everything else that changes something needs "operate"
ADMIN_ACTIONS = {"setPolicy", "setSchedule", "setAssistant", "setHeal", "revertChange", "setCheckpoints", "configRestore",
                 "setConsoleDefaults", "recipeCatalog", "upgradeComponent",
                 "upgradeEverywhere",
                 "reboot", "rebootFinish", "nasRest", "policyApply"}
NEED_RANK = {"view": 0, "operate": 1, "admin": 2}
_LOGIN_FAILS = None          # ip -> [failures, locked_until]; kept on disk so a restart does not reset it
_LOGIN_LOCK = threading.Lock()


def _fails():
    global _LOGIN_FAILS
    if _LOGIN_FAILS is None:
        try:
            raw = json.loads((pathlib.Path(P["console_state"]) / "login-fails.json").read_text())
            _LOGIN_FAILS = {ip: v for ip, v in raw.items() if v[1] > time.time() - 86400}
        except (OSError, ValueError, TypeError, IndexError):
            _LOGIN_FAILS = {}
    return _LOGIN_FAILS


def _fails_save():
    f = pathlib.Path(P["console_state"]) / "login-fails.json"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(_LOGIN_FAILS))
        os.replace(tmp, f)
    except OSError:
        pass


def _login_locked(ip):
    with _LOGIN_LOCK:
        return _fails().get(ip, (0, 0))[1] > time.time()


def _login_failed(ip):
    with _LOGIN_LOCK:
        n = _fails().get(ip, (0, 0))[0] + 1
        _LOGIN_FAILS[ip] = [n, time.time() + (min(3600, 30 * 2 ** (n - 5)) if n >= 5 else 0)]
        _fails_save()


def _login_ok(ip):
    with _LOGIN_LOCK:
        if _fails().pop(ip, None) is not None:
            _fails_save()


def token() -> str:
    try:
        return pathlib.Path(TOKEN_FILE).read_text().strip()
    except Exception:
        return ""


def valid_params(name: str, p: dict) -> bool:
    if name in RECIPE_ACTIONS:
        c = CM.cfg()
        rc = CM.recipes_cfg(c)
        if name == "recipeCatalog":
            if p.get("remove"):
                return p.get("name") in rc["catalog"]
            return not CM.validate_catalog_entry(p.get("name"), p.get("entry"), c)
        if p.get("slot") not in rc["slots"]:
            return False
        if name == "recipeDeploy":
            cat = rc["catalog"].get(p.get("recipe"))
            if not cat or p["slot"] not in (cat.get("slots") or []):
                return False
            if p.get("ref") and not CM._REF.match(str(p["ref"])):
                return False
        return True
    if name in HOST_REQUIRED and not p.get("host"):
        return False
    if name == "hygieneFix":
        if not p.get("host") or not isinstance(p.get("items"), list) or not p["items"]:
            return False
        known = {i["id"] for i in (hygiene_report().get("hosts", {}).get(p["host"]) or {}).get("items", []) if i.get("tier")}
        if not all(isinstance(i, str) and i in known for i in p["items"]):
            return False
    if name == "restore":
        if not p.get("host") or not _SNAP_RE.match(str(p.get("snapshot", ""))) or not _ok_path(p.get("path")):
            return False
        if p.get("to", "staging") not in ("staging", "host"):
            return False
    if name == "setConsoleDefaults":
        return not CM.validate_console(p.get("settings"))[1]
    if name == "upgradeEverywhere":
        return sum(1 for rows in (_versions().get("hosts") or {}).values() for r in rows
                   if r["component"] == p.get("component") and (r.get("fix") or {}).get("change")) > 0
    if name in ("upgradeComponent", "skipRelease"):
        row = next((r for r in (_versions().get("hosts") or {}).get(p.get("host"), [])
                    if r["component"] == p.get("component")), None)
        if not row:
            return False
        if name == "upgradeComponent":
            return bool((row.get("fix") or {}).get("argv"))
        return bool(p.get("undo")) or (row["state"] == "upgrade" and p.get("release") == row.get("latest"))
    if name == "setAssistant":
        a = p.get("assistant")
        if not isinstance(a, dict) or "key_host" in a or CM.validate_assistant(a)[1]:
            return False
    if name == "setHeal":
        return not heal_errors(p.get("heal"))
    if name == "revertChange":
        from noctyx import change
        return (isinstance(p.get("id"), str) and bool(re.fullmatch(r"[0-9a-f]{8}(-undo)?", p["id"]))
                and change.revertible(p["id"])[1] is None)
    if name == "takeCheckpoint":
        return p.get("host") in {h["name"] for h in CM.backup_hosts(CM.cfg())}
    if name == "setCheckpoints":
        from noctyx import checkpoints
        return not checkpoints.validate(p.get("checkpoints"))[1]
    if name == "configRestore":
        from noctyx import selfstate
        return p.get("name") in {x["name"] for x in selfstate.config_history()}
    if name == "setPolicy":
        pol, errs = CM.validate_policy(p.get("policy"))
        r = p.get("retention")
        hf = p.get("hf_mirror")
        if hf is not None and not isinstance(hf, dict):
            return False
        if errs or (r is not None and (not isinstance(r, dict) or any(
                type(r.get(k)) is not int or not 0 <= r[k] <= 999 for k in ("daily", "weekly", "monthly")))):
            return False
    if name == "setSchedule":
        if not p.get("profile") or p.get("mode", "set") not in ("set", "disable", "reset"):
            return False
        if p.get("every") and not _EVERY_RE.match(str(p["every"])):
            return False
        if p.get("window") and not _WINDOW_RE.match(str(p["window"])):
            return False
    if p.get("host") is not None and p["host"] not in VALID_HOSTS:
        return False
    if p.get("profile") is not None and p["profile"] not in VALID_PROFILES:
        return False
    return True


def _parse_probe(output):
    """noctyx probe prints 'name  268 Mbit/s' lines; persist them so the
    Network panel can show a last-known reading instead of 'never probed'."""
    out = {m.group(1): float(m.group(2))
           for m in re.finditer(r"^(\S+)\s+([\d.]+) Mbit/s", output or "", re.M)}
    if out:
        try:
            UI_STATE.mkdir(parents=True, exist_ok=True)
            now = time.time()
            f = UI_STATE / "probe-last.json"
            try:
                cur = json.loads(f.read_text())
            except Exception:
                cur = {}
            # merge: the orchestrator's pre-backup probe writes the same file, one host at a time
            mb = {**(cur.get("mbit") or {}), **out}
            at = {**(cur.get("at") or {}), **{k: now for k in out}}
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": now, "mbit": mb, "at": at}))
            os.replace(tmp, f)
        except Exception:
            pass
    return out


def _reap_orphan_jobs():
    """Persist the UI job table across restarts.

    The job table is in-process, so a restart orphans its jobs: the client's
    adoptRunning() re-shows them as 'running' forever (frozen badges) and
    completed backups land in the repo with no UI closure. Persist a minimal
    record on every start/finish, and on boot mark any still-open record as
    lost (done=True, rc=-2, note). The orchestrator child may still be alive —
    audit.jsonl remains the authoritative trail — but the badge must resolve.
    """
    try:
        if JOBS_FILE.exists():
            stale = json.loads(JOBS_FILE.read_text())
            for j in stale:
                if not j.get("done") and j.get("unit"):
                    # the job runs in its own systemd unit and survived us:
                    # re-attach a follower instead of declaring it lost
                    j["output"] = (j.get("output") or "")[-2000:]
                    JOBS[j["id"]] = j
                    _refollow(j)
                    continue
                if not j.get("done"):
                    # drop the huge live-tail before surfacing in the table
                    j["output"] = (j.get("output") or "")[-2000:]
                    j.update(done=True, running=False, rc=-2,
                             note="UI restarted while this job was in flight;"
                                  " check audit.jsonl / machine report for the real outcome")
                    JOBS[j["id"]] = j
            # keep reaped and re-followed records; everything already done
            # was dropped above (same as before)
            JOBS_FILE.write_text(json.dumps(list(JOBS.values())[-60:]))
    except Exception:
        pass


def _refollow(job):
    """Resume a detached job's live tail + outcome after a UI restart. Runs
    outside the one-job semaphore: the job itself is already running."""
    def worker():
        job["running"] = True
        try:
            p = subprocess.Popen([sys.executable, "-u", ORCH, "follow",
                                  "--unit", job["unit"], "--log", job["log"]],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tail = []
            for line in p.stdout or []:
                tail.append(line)
                if len(tail) > 900:
                    tail = tail[-700:]
                job["output"] = "".join(tail)[-20000:]
            job["rc"] = p.wait()
        except Exception as e:
            job["rc"] = -1
            job["output"] = (job.get("output") or "") + f"\n{e}"
        job["running"] = False
        job["done"] = True
        with JOBS_LOCK:
            _persist_jobs()
    th = threading.Thread(target=worker, daemon=True)
    _JOB_THREADS.append(th)
    th.start()


def _persist_jobs():
    try:
        JOBS_FILE.write_text(json.dumps(list(JOBS.values())[-60:]))
    except Exception:
        pass


_ORPHAN_SWEEP_DONE = False


def run_job(name: str, params: dict, user=None):
    # A previous UI process that was restarted/killed left its in-process jobs
    # "running" with nobody to ever finish them. Mark them dead at startup so
    # adoptRunning()/pollAll cannot repaint frozen "running" badges for ghosts.
    # The underlying orchestrator job may still be running on its own (ssh
    # child survives); the audit trail, not the job table, is the truth there.
    global _ORPHAN_SWEEP_DONE
    if not _ORPHAN_SWEEP_DONE:
        _ORPHAN_SWEEP_DONE = True
        try:
            # best-effort: finish any child still streaming (restart case — the
            # command survives for a grace window) before recording outcomes
            for th in _JOB_THREADS:
                th.join(timeout=15)
        except Exception:
            pass
        _reap_orphan_jobs()
    argv = ACTIONS[name](params) if name in PARAM_ACTIONS else ACTIONS[name]()
    jid = secrets.token_hex(4)
    job = {"id": jid, "action": name, "params": params, "argv": argv,
           "started": _utcnow(),
           "started_ts": time.time(), "running": False,
           "rc": None, "output": "", "done": False}
    with JOBS_LOCK:
        JOBS[jid] = job
    _persist_jobs()
    # record the human trigger in the same audit trail everything else uses
    try:
        with AUDIT.open("a") as f:
            f.write(json.dumps({"ts": job["started"], "event": "ui-trigger", "action": name,
                                **({"user": user} if user else {}), **params}) + "\n")
    except Exception:
        pass

    def worker():
        # streamed output: run the orchestrator with -u and pump its merged
        # stdout+stderr line-by-line into job["output"] so the UI can show a
        # LIVE tail — a job that prints nothing for 40 minutes looks broken
        # even when it isn't (operator, 2026-09-26).
        global RUNNING_JOB
        if name in LONG_ACTIONS:
            with JOBS_LOCK:
                cur = RUNNING_JOB
            if cur and not cur.get("done"):
                # say WHAT it waits for, not a bare "queued" for three hours
                job["queued_behind"] = {"id": cur["id"], "action": cur["action"],
                                        "host": (cur.get("params") or {}).get("host")}
        with (RUNNING if name in LONG_ACTIONS else _NOLOCK):  # one LONG UI job at a time
            job.pop("queued_behind", None)
            if job.get("cancelled"):
                job.update(done=True, rc=-3, output=job["cancelled"] + "\n", note=job["cancelled"])
                with JOBS_LOCK:
                    _persist_jobs()
                return
            job["running"] = True
            if name in LONG_ACTIONS:
                RUNNING_JOB = job
            tail = []
            try:
                p = subprocess.Popen([sys.executable, "-u"] + argv,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, start_new_session=True,
                                     env={**os.environ, **({"NOCTYX_BY": user} if user else {})})
                wd = threading.Timer(ACTION_TIMEOUT.get(name, 3 * 86400), _stop_job, (p, job, ACTION_TIMEOUT.get(name)))
                wd.daemon = True
                wd.start()
                for line in p.stdout or []:
                    if line.startswith("DETACHED unit=") and "unit" not in job:
                        m = re.match(r"DETACHED unit=(\S+) log=(\S+)", line)
                        if m:
                            job["unit"], job["log"] = m.group(1), m.group(2)
                            with JOBS_LOCK:
                                _persist_jobs()
                    tail.append(line)
                    if len(tail) > 900:
                        tail = tail[-700:]
                    job["output"] = "".join(tail)[-20000:]
                job["rc"] = p.wait()
                wd.cancel()
                if name == "probe":
                    _parse_probe(job["output"])
                if job["rc"] and name in CANCEL_ON_FAIL:
                    _cancel_like(job)
            except Exception as e:
                job["rc"] = -1
                job["output"] = (job["output"] or "") + f"\n{e}"
            job["running"] = False
            job["done"] = True
            with JOBS_LOCK:
                _persist_jobs()
    th = threading.Thread(target=worker, daemon=True)
    _JOB_THREADS.append(th)
    th.start()
    return jid


def _stop_job(p, job, limit):
    """The job ran past its time: SIGTERM (a change rolls itself back), SIGKILL two minutes later."""
    if p.poll() is not None:
        return
    job["note"] = f"stopped: ran over {limit // 60 if limit else '?'} min"
    job["output"] = (job.get("output") or "") + f"\n{job['note']}; it is told to stop and roll back\n"
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _cancel_like(failed):
    """A failed upgrade cancels the queued ones for the same component: they would hit the same failure."""
    comp = (failed.get("params") or {}).get("component")
    where = (failed.get("params") or {}).get("host") or "all"
    with JOBS_LOCK:
        for j in JOBS.values():
            if (not j.get("done") and not j.get("running") and j is not failed and j.get("action") in CANCEL_ON_FAIL
                    and (j.get("params") or {}).get("component") == comp):
                j["cancelled"] = (f"cancelled: the {comp} upgrade on {where} before it failed (job {failed['id']}); "
                                  "see its output, then start this one again")


def audit_tail(n=15):
    try:
        lines = AUDIT.read_text().splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []


_REPO_SIZES = {"ts": 0, "repos": {}, "running": False}


def repo_sizes(max_age=1800):
    """Bytes per Noctyx repo on the NAS (`du` of <prefix><repo>), so the Storage
    panel can tell Noctyx's share from everything else on the volume — the
    A fast-growing volume is often something other than the backups.
    du over NFS takes a minute: refreshed in a thread, last result persisted."""
    cache = UI_STATE / "repo_sizes.json"
    if not _REPO_SIZES["ts"]:
        try:
            _REPO_SIZES.update(json.loads(cache.read_text()))
        except Exception:
            pass
    if time.time() - _REPO_SIZES["ts"] > max_age and not _REPO_SIZES["running"]:
        _REPO_SIZES["running"] = True

        def worker():
            try:
                root, sizes = S.get("repo_root", ""), {}
                for r in CFG["repositories"]:
                    d = f"{root}/{P['repo_dir_prefix']}{r['id']}"
                    if not os.path.isdir(d):
                        continue
                    o = subprocess.run(["du", "-sb", d], capture_output=True, text=True, timeout=1800)
                    if o.returncode == 0 and o.stdout.split():
                        sizes[r["id"]] = int(o.stdout.split()[0])
                if sizes:
                    _REPO_SIZES.update(ts=time.time(), repos=sizes)
                    cache.write_text(json.dumps({"ts": _REPO_SIZES["ts"], "repos": sizes}))
            except Exception:
                pass
            finally:
                _REPO_SIZES["running"] = False
        threading.Thread(target=worker, daemon=True).start()
    return {"ts": _REPO_SIZES["ts"], "repos": dict(_REPO_SIZES["repos"])}


def capacity():
    out = []
    # the NAS is the backup target
    for label, path in [("nas (primary)", S.get("repo_root", ""))] + \
                       ([("nvme (retired)", S["hot_root"])] if S.get("hot_root") else []):
        free_gb = used_gb = 0.0
        try:
            st = os.statvfs(path)
            free_gb = st.f_bavail * st.f_frsize / 1e9
            used_gb = (st.f_blocks - st.f_bfree) * st.f_frsize / 1e9
        except Exception:
            pass
        out.append({"tier": label, "path": path, "free_gb": round(free_gb, 1),
                    "used_gb": round(used_gb, 1), "mounted": bool(path) and os.path.isdir(path)})
        if label.startswith("nas"):
            rs = repo_sizes()
            if rs["repos"]:
                out[-1].update(repos_gb={k: round(v / 1e9, 1) for k, v in rs["repos"].items()},
                               repos_ts=rs["ts"])
    return out


def growth_forecast(days=7):
    """Used-bytes trend of the NAS tier from Prometheus (noctyx_tier_used_bytes),
    least squares over `days`; None until there is >= 2 h of history."""
    try:
        res = _pr('max(noctyx_tier_used_bytes{tier=~"nas.*"})', minutes=days * 1440, step=1800)
        pts = [(float(t), float(v)) for r in res for t, v in r["values"]]
    except Exception:
        return None
    if len(pts) < 4 or pts[-1][0] - pts[0][0] < 7200:
        return {"hours": round((pts[-1][0] - pts[0][0]) / 3600, 1) if pts else 0, "rate_per_day": None}
    # fit only the stretch since the last clean-up: a prune or a deleted mirror
    # is a step down, and a line through it says "shrinking" (or, with the growth
    # that caused it still in the window, "full in N days") for a whole week
    drop = 0
    for i in range(1, len(pts)):
        if pts[i - 1][1] - pts[i][1] > max(50e9, 0.005 * pts[i - 1][1]):
            drop = i
    reset = pts[drop][0] if drop else None
    pts = pts[drop:]
    if len(pts) < 4 or pts[-1][0] - pts[0][0] < 7200:
        return {"hours": round((pts[-1][0] - pts[0][0]) / 3600, 1) if pts else 0, "rate_per_day": None,
                "reset": reset}
    n = len(pts); mx = sum(t for t, _ in pts) / n; my = sum(v for _, v in pts) / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    slope = sum((t - mx) * (v - my) for t, v in pts) / sxx if sxx else 0.0
    free = next((t["free_gb"] * 1e9 for t in capacity() if t["tier"].startswith("nas")), None)
    per_day = slope * 86400
    hours = round((pts[-1][0] - pts[0][0]) / 3600, 1)
    return {"hours": hours, "rate_per_day": per_day, "settling": hours < 48, "reset": reset,
            "days_to_full": (free / per_day) if free and per_day > 0 and hours >= 48 else None}


def snapshot_summary():
    """Per-host snapshot counts via LOCAL restic (head node has the hot repos + keys).
    Cached ~60 s; served stale-but-fast when the cache is fresh enough.

    --no-lock: this runs as root every minute; a locking read drops a ROOT-owned
    lock file into the repo, which rest-server (the repository owner) cannot read —
    client backups then get HTTP 500s, and a listing killed mid-read leaves the
    file behind for good. A repo that fails to list keeps its LAST GOOD numbers
    (flagged stale) instead of making its hosts vanish for a minute."""
    cache = UI_STATE / "snapshots.json"
    try:
        c = json.loads(cache.read_text())
        if time.time() - c.get("ts", 0) < 60:
            return c
    except Exception:
        c = {"ts": 0, "hosts": {}}
    hosts, errors = {}, {}
    for repo in CFG["repositories"]:
        key = f"{P['repo_keys']}/{repo['id']}.key"
        repo_path = CM.repo_dir(CFG, repo["id"])
        if not os.path.isdir(repo_path) or not os.path.isfile(key):
            continue
        try:
            r = subprocess.run(["restic", "-r", repo_path, "--password-file", key,
                                "--no-lock", "--no-cache", "snapshots", "--json"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip()[-200:] or f"rc={r.returncode}")
            # times as UTC epochs: restic reports each client's LOCAL offset
            # (some nodes are -05:00), and comparing those strings put
            # "last" in the wrong order
            for s in json.loads(r.stdout or "[]"):
                ts = CM._restic_ts(s["time"])
                prof = next((t[8:] for t in s.get("tags") or [] if t.startswith("profile-")), "?")
                d = hosts.setdefault(s.get("hostname", "?"),
                                     {"count": 0, "last_ts": 0, "repo": repo["id"],
                                      "profiles": {}, "recent": []})
                d["count"] += 1
                d["last_ts"] = max(d["last_ts"], ts)
                pp = d["profiles"].setdefault(prof, {"count": 0, "last_ts": 0})
                pp["count"] += 1
                pp["last_ts"] = max(pp["last_ts"], ts)
                d["recent"].append({"id": s.get("short_id") or s["id"][:8], "ts": ts, "profile": prof})
        except Exception as e:
            errors[repo["id"]] = str(e)[:200]
            for hn, d in (c.get("hosts") or {}).items():
                if d.get("repo") == repo["id"]:
                    hosts[hn] = {**d, "stale": True}
    for d in hosts.values():
        d["recent"] = sorted(d["recent"], key=lambda x: x["ts"])[-10:]
    new = {"ts": time.time(), "hosts": hosts, "errors": errors}
    try:
        UI_STATE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(new))
    except Exception:
        pass
    return new


def _audited_snapshot_ids():
    """Snapshot ids some backup record claims. A repo snapshot NOT in here
    finished without its driver noticing (killed ssh / UI restart) — real
    data, but the run that made it was never seen to end."""
    ids = set()
    try:
        for line in AUDIT.read_text().splitlines():
            if '"snapshot"' in line:
                try:
                    sid = json.loads(line).get("snapshot")
                except Exception:
                    continue
                if sid:
                    ids.add(sid[:8])
    except Exception:
        pass
    return ids


def _systemd_prop(unit, prop):
    if hostos.IS_MAC:        # launchd keeps no such properties: a job is running or not, a timer installed or not
        if prop != "ActiveState":
            return ""
        if unit.endswith(".timer"):
            return "active" if hostos.service_installed(unit.replace(".timer", ".service"), P["launchd_prefix"]) else ""
        if hostos.job_running(str(CM.JOB_LOG_DIR / f"{unit.removesuffix('.service')}.log")) or \
                hostos.service_active(unit, P["launchd_prefix"]):
            return "active"
        return "inactive"
    try:
        return subprocess.run(["systemctl", "show", "-p", prop, "--value", unit],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def schedule_summary(snaps=None):
    """Per host/profile: last snapshot, interval, next due, RPO health — from
    the orchestrator's OWN functions, over the same repo data as the Fleet."""
    c = CM.cfg()                                   # re-read: edits apply at once
    snaps = snaps or snapshot_summary()
    sch = c.get("schedule") or {}
    slack = int(sch.get("tick_minutes", 15)) * 60
    retry = CM.parse_duration(sch.get("retry_after", "1h"))
    try:
        state = json.loads(CM.SCHED_STATE.read_text())
    except Exception:
        state = {}
    overrides = sch.get("hosts") or {}
    now, local_dt = time.time(), datetime.datetime.now()
    rows = []
    for h in CM.backup_hosts(c):
        eff = CM.effective_schedule(c, h["name"])
        for prof in sorted(set(eff) | set((overrides.get(h["name"]) or {}))):
            rule = eff.get(prof)
            last = ((snaps["hosts"].get(h["name"]) or {}).get("profiles", {}).get(prof) or {}).get("last_ts") or None
            row = {"host": h["name"], "profile": prof, "last_ts": last,
                   "override": prof in (overrides.get(h["name"]) or {}), "enabled": bool(rule)}
            if rule:
                att = state.get(f"{h['name']}/{prof}") or {}
                failed_at = att["ts"] if att.get("ok") is False and (last is None or att["ts"] > last) else None
                ok, why = CM.due(now, local_dt, last, rule["every"], rule["window"], failed_at, retry, slack)
                row.update(every=rule["every"], window=rule["window"], due=ok, why=why,
                           failed_at=failed_at,
                           next_ts=CM.next_due(now, last, rule["every"], rule["window"], failed_at, retry, slack),
                           health=CM.health(now, last, rule["every"], rule["window"], slack))
            rows.append(row)
    hot = CM._spindle_of(c, CM.repo_root(c))
    nxt = _systemd_prop(f"{UNIT}-tick.timer", "NextElapseUSecRealtime")
    return {"ts": now, "rows": rows, "tick_minutes": sch.get("tick_minutes", 15),
            "retry_after": sch.get("retry_after", "1h"),
            "defaults": {p: r for p, r in (sch.get("defaults") or {}).items() if not p.startswith("_")},
            "profiles": sorted(c.get("profiles", {})),
            "tick": {"running": _systemd_prop(f"{UNIT}-tick.service", "ActiveState") == "active",
                     "timer": _systemd_prop(f"{UNIT}-tick.timer", "ActiveState"),
                     "next": nxt},
            "spindle": {"name": hot, "holder": CM.spindle_holder(hot)}}


def runs(n=15):
    """Recent detached jobs (the job_logs dir): running, finished (rc)
    or DIED — a unit that ended without writing its exit record."""
    out = []
    try:
        logs = sorted(CM.JOB_LOG_DIR.glob(f"{UNIT}-*.log"),
                      key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    except Exception:
        return out
    for f in logs:
        unit = f.stem
        m = re.match(rf"{re.escape(UNIT)}-([a-z]+)-(?:(.+)-)?(\d{{8}}T\d{{6}}Z)-\d+$", unit)
        if not m:
            continue                     # another file in the folder (a log someone left), not a detached job's
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 6000))
                tail = fh.read().decode("utf-8", "replace")
        except Exception:
            tail = ""
        ex = re.findall(r"^NOCTYX_EXIT (-?\d+)$", tail, re.M)
        if ex:
            state, rc = ("ok" if ex[-1] == "0" else "failed"), int(ex[-1])
        elif _systemd_prop(unit, "ActiveState") in ("active", "activating", "deactivating"):
            state, rc = "running", None
        else:
            state, rc = "died", None
        lines = [l for l in tail.splitlines() if l.strip() and not re.match(r"NOCTYX_EXIT", l)]
        started = datetime.datetime.strptime(m.group(3), "%Y%m%dT%H%M%SZ") \
            .replace(tzinfo=datetime.timezone.utc).timestamp()
        out.append({"unit": unit, "cmd": m.group(1), "host": m.group(2),
                    "started_ts": started, "ended_ts": None if state == "running" else f.stat().st_mtime,
                    "state": state, "rc": rc, "last": lines[-1][:160] if lines else ""})
    return out


# ── live metrics (Prometheus) ────────────────────────────────────────────
# node_exporter on every node -> Prometheus on this head node (infra/metrics/).
# The browser never talks to Prometheus: only the fixed queries below run,
# and the only value ever substituted into them is a host name validated
# against the site config. Every series carries a `host` label = the site's host name.
PROM = os.environ.get("NOCTYX_PROM", "http://127.0.0.1:9090")
_NET = 'device!~"lo|docker.*|veth.*|br-.*|tailscale.*|virbr.*|cni.*|flannel.*"'
_NIC = 'device!~"lo|docker.*|veth.*|br-.*|virbr.*|cni.*|flannel.*"'   # tab: keep tailscale0
_DSK = 'device=~"nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+|disk[0-9]+"'
_FS = 'fstype!~"tmpfs|overlay|squashfs|nsfs|fuse.*|devtmpfs|ramfs|autofs|efivarfs"'
FLEET_Q = {   # instant, all hosts at once: one query per number on the tiles
    "cpu": '100 * (1 - avg by(host)(rate(node_cpu_seconds_total{mode="idle",host!=""}[1m])))',
    "mem_used": '(node_memory_MemTotal_bytes{host!=""} - node_memory_MemAvailable_bytes{host!=""}) or (node_memory_total_bytes{host!=""} - node_memory_free_bytes{host!=""} - node_memory_inactive_bytes{host!=""})',
    "mem_total": 'node_memory_MemTotal_bytes{host!=""} or node_memory_total_bytes{host!=""}',
    "net_tx": f'sum by(host)(rate(node_network_transmit_bytes_total{{{_NET},host!=""}}[1m])) * 8',
    "net_rx": f'sum by(host)(rate(node_network_receive_bytes_total{{{_NET},host!=""}}[1m])) * 8',
    "disk_io": f'sum by(host)(rate(node_disk_read_bytes_total{{{_DSK},host!=""}}[1m]) + rate(node_disk_written_bytes_total{{{_DSK},host!=""}}[1m]))',
    "gpu_util": 'max by(host)(noctyx_gpu_utilization_percent{host!=""})',
    "gpu_temp": 'max by(host)(noctyx_gpu_temperature_celsius{host!=""})',
    "gpu_power": 'sum by(host)(noctyx_gpu_power_watts{host!=""})',
    # CPU/SoC: AMD k10temp Tctl / Intel package, else the ACPI zones (GB10), else macmon (Mac)
    "sys_temp": 'max by(host)(node_hwmon_temp_celsius{host!=""} * on(host,instance,chip,sensor) group_left() node_hwmon_sensor_label{label=~"Tctl|Tdie|Package id 0",host!=""}) or max by(host)(node_thermal_zone_temp{host!=""}) or max by(host)(noctyx_system_temperature_celsius{host!=""})',
    "nvme_temp": 'max by(host)(node_hwmon_temp_celsius{chip=~"nvme.*",host!=""})',
    # macOS: the sealed system volume at / is tiny; the data volume is what fills up
    "root_pct": '(100 * (1 - node_filesystem_avail_bytes{mountpoint="/System/Volumes/Data",host!=""} / node_filesystem_size_bytes{mountpoint="/System/Volumes/Data",host!=""})) or (100 * (1 - node_filesystem_avail_bytes{mountpoint="/",host!=""} / node_filesystem_size_bytes{mountpoint="/",host!=""}))',
    "load1": 'node_load1{host!=""}',
    "failed_units": 'count by(host)(node_systemd_unit_state{state="failed",host!=""} == 1)',
    "reboot": 'max by(host)(node_reboot_required{host!=""})',
    "containers": 'count by(host)(noctyx_container_running{host!=""})',
    "up": 'max by(host)(up{host!=""})',
    # the fast fabric (25 Gb/s+ links: the 200G ConnectX ports carrying tensor-parallel traffic)
    "fabric_tx": 'sum by(host)(rate(node_network_transmit_bytes_total{host!=""}[1m]) and on(host,device) (node_network_speed_bytes > 3e9)) * 8',
    "fabric_rx": 'sum by(host)(rate(node_network_receive_bytes_total{host!=""}[1m]) and on(host,device) (node_network_speed_bytes > 3e9)) * 8',
}
FLEET_SERIES = {   # 1-hour, fleet-wide, for the Cluster panel's charts
    "tokens": 'sum by(model_name)(rate(vllm:generation_tokens_total[1m]))',
    "requests": 'sum(vllm:num_requests_running) or vector(0)',
    "waiting": 'sum(vllm:num_requests_waiting) or vector(0)',
    "gpu_power": 'sum(noctyx_gpu_power_watts)',
    "net_tx": f'sum(rate(node_network_transmit_bytes_total{{{_NET}}}[1m])) * 8',
    "net_rx": f'sum(rate(node_network_receive_bytes_total{{{_NET}}}[1m])) * 8',
    "gpu_temp_max": 'max(noctyx_gpu_temperature_celsius)',
}


def fleet_extras():
    """Cluster panel: fleet charts, model roster, infrastructure strip."""
    out, errors = {"series": {}, "roster": [], "infra": {}}, []
    for k, q in FLEET_SERIES.items():
        try:
            res = _pr(q, minutes=60, step=60)
            out["series"][k] = _series(res, "model_name") if k == "tokens" else \
                next(iter(_series(res).values()), [])
        except Exception as e:
            errors.append(f"fleet {k}: {str(e)[:80]}")
    one = lambda q: next((_num(r["value"][1]) for r in _pq(q)), None)   # noqa: E731
    try:
        cfg_ = {(r["metric"].get("host"), r["metric"].get("model_name")): r["metric"]
                for r in _pq("vllm:cache_config_info")}
        started = {r["metric"].get("host"): _num(r["value"][1]) for r in _pq('process_start_time_seconds{job="llm"}')}
        topo = llm_topology().get("hosts") or {}
        workers = {}
        for hn, t in topo.items():
            for srv in t.get("servers", []):
                if srv.get("role") == "worker" and srv.get("worker_for"):
                    workers.setdefault(srv["worker_for"], []).append(hn)
        for hn, t in topo.items():
            for srv in t.get("servers", []):
                if srv.get("role") != "serving":
                    continue
                cc = cfg_.get((hn, srv.get("model"))) or next((v for (h, m), v in cfg_.items() if h == hn), {})
                blocks, bs = cc.get("num_gpu_blocks"), cc.get("block_size")
                out["roster"].append({
                    "host": hn, "model": srv.get("model"), "engine": srv.get("engine"), "tp": srv.get("tp"),
                    "workers": workers.get(hn, []), "port": srv.get("port"),
                    "max_model_len": srv.get("max_model_len"),
                    "kv_tokens": int(blocks) * int(bs) if blocks and bs and blocks.isdigit() and bs.isdigit() else None,
                    "gpu_mem_util": _num(cc.get("gpu_memory_utilization")), "kv_dtype": cc.get("cache_dtype"),
                    # /v1/models "created" is just the request time on vLLM — not a load time
                    "started_ts": started.get(hn)})
    except Exception as e:
        errors.append(f"roster: {str(e)[:80]}")
    try:
        out["infra"] = {
            "gpu_wh_24h": one("sum(avg_over_time(noctyx_gpu_power_watts[24h])) * 24"),
            "gpu_wh_1h": one("sum(avg_over_time(noctyx_gpu_power_watts[1h]))"),
            "tokens_24h": one("sum(increase(vllm:generation_tokens_total[24h]))"),
            "requests_24h": one("sum(increase(vllm:request_success_total[24h]))"),
            "exporters_up": one(f'count(up{{job=~"{P["prom_job_nodes"]}|node"}} == 1)'),
            "exporters_total": one(f'count(up{{job=~"{P["prom_job_nodes"]}|node"}})'),
            "llm_up": one('count(up{job="llm"} == 1)'), "llm_total": one('count(up{job="llm"})'),
            "rest_up": one("max(noctyx_rest_endpoint_up)"),
            "nas_free": one('max(noctyx_tier_free_bytes{tier=~"nas.*"})'),
            "nas_used": one('max(noctyx_tier_used_bytes{tier=~"nas.*"})'),
            "raid": [{"array": r["metric"].get("array"), "level": r["metric"].get("level"),
                      "active": _num(r["value"][1])} for r in _pq("noctyx_nas_raid_members_active")],
            "raid_total": {r["metric"].get("array"): _num(r["value"][1]) for r in _pq("noctyx_nas_raid_members_total")},
            "raid_sync": {r["metric"].get("array"): _num(r["value"][1]) for r in _pq("noctyx_nas_raid_sync_percent")},
            "alerts": len(alerts_view().get("active") or []),
            "backups_ok": one("count(noctyx_backup_health_code == 0)"), "backups_total": one("count(noctyx_backup_health_code)"),
        }
    except Exception as e:
        errors.append(f"infra: {str(e)[:80]}")
    return out, errors
FLEET_SPARK = ("cpu", "gpu_util", "net_tx", "net_rx", "fabric_tx", "fabric_rx", "mem_used")
# LLM servers (Prometheus job "llm", vLLM metric names; discovered by infra/metrics/llm-discover)
_KV = '(vllm:kv_cache_usage_perc or vllm:gpu_cache_usage_perc)'
LLM_Q = {   # all keyed by (host, model_name)
    "running": 'max by(host,model_name)(vllm:num_requests_running)',
    "waiting": 'max by(host,model_name)(vllm:num_requests_waiting)',
    "kv_pct": f'max by(host,model_name)({_KV}) * 100',
    "gen_tps": 'sum by(host,model_name)(rate(vllm:generation_tokens_total[1m]))',
    "prompt_tps": 'sum by(host,model_name)(rate(vllm:prompt_tokens_total[1m]))',
    "req_rate": 'sum by(host,model_name)(rate(vllm:request_success_total[5m]))',
    "ttft_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:time_to_first_token_seconds_bucket[5m])))',
    "ttft_p95": 'histogram_quantile(0.95, sum by(host,model_name,le)(rate(vllm:time_to_first_token_seconds_bucket[5m])))',
    "e2e_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:e2e_request_latency_seconds_bucket[5m])))',
    "itl_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:inter_token_latency_seconds_bucket[5m])))',
    "prefix_hit": 'sum by(host,model_name)(rate(vllm:prefix_cache_hits_total[5m])) / sum by(host,model_name)(rate(vllm:prefix_cache_queries_total[5m])) * 100',
    "preempt_rate": 'sum by(host,model_name)(rate(vllm:num_preemptions_total[5m]))',
    # phases. gen_tps above is throughput averaged over the last minute INCLUDING
    # prefill and idle time — the number a client sees while its answer streams is
    # decode_tps (generated tokens per second of decode time), and prefill has its own speed.
    "decode_tps": 'sum by(host,model_name)(rate(vllm:generation_tokens_total[5m])) / sum by(host,model_name)(rate(vllm:request_decode_time_seconds_sum[5m]))',
    "prefill_tps": 'sum by(host,model_name)(rate(vllm:prompt_tokens_total[5m])) / sum by(host,model_name)(rate(vllm:request_prefill_time_seconds_sum[5m]))',
    "prefill_share": 'sum by(host,model_name)(rate(vllm:request_prefill_time_seconds_sum[5m])) / (sum by(host,model_name)(rate(vllm:request_prefill_time_seconds_sum[5m])) + sum by(host,model_name)(rate(vllm:request_decode_time_seconds_sum[5m]))) * 100',
    "prefill_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:request_prefill_time_seconds_bucket[5m])))',
    "queue_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:request_queue_time_seconds_bucket[5m])))',
    "decode_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:request_decode_time_seconds_bucket[5m])))',
    "prompt_tok_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:request_prompt_tokens_bucket[5m])))',
    "gen_tok_p50": 'histogram_quantile(0.5, sum by(host,model_name,le)(rate(vllm:request_generation_tokens_bucket[5m])))',
    "tokens_1h": 'sum by(host,model_name)(increase(vllm:generation_tokens_total[1h]))',
    "requests_1h": 'sum by(host,model_name)(increase(vllm:request_success_total[1h]))',
}
LLM_SPARK = ("gen_tps", "running", "prompt_tps", "prefill_tps")
LLM_TOPO = pathlib.Path(P["console_state"]) / "llm-topology.json"


def llm_topology():
    try:
        return json.loads(LLM_TOPO.read_text())
    except Exception:
        return {"ts": 0, "hosts": {}}


def _host_q(q, host):
    """Restrict every vllm selector in q to one host."""
    return re.sub(r"(vllm:[a-z0-9_]+)(\{[^}]*\})?",
                  lambda m: f'{m.group(1)}{{host="{host}"' + ("," + m.group(2)[1:] if m.group(2) else "}"), q)


def llm_metrics():
    """{host: {"servers": [{model, running, waiting, kv_pct, gen_tps, …, spark{}}], "up": 0/1}}"""
    hosts, errors = {}, []
    def put(h, model, k, v):
        d = hosts.setdefault(h, {"servers": {}})
        d["servers"].setdefault(model, {"model": model})[k] = v
    for k, q in LLM_Q.items():
        try:
            for r in _pq(q):
                put(r["metric"].get("host", "?"), r["metric"].get("model_name", "?"), k, _num(r["value"][1]))
        except Exception as e:
            errors.append(f"llm {k}: {str(e)[:100]}")
    try:
        for r in _pq('min by(host)(up_mtplx)'):            # the exporter is up; is the Mac's server?
            d = hosts.setdefault(r["metric"].get("host", "?"), {"servers": {}})
            d["up"] = min(d.get("up", 1), int(float(r["value"][1])))
        for r in _pq('max by(host,port,engine)(up{job="llm"})'):
            d = hosts.setdefault(r["metric"].get("host", "?"), {"servers": {}})
            d["up"] = min(d.get("up", 1), int(float(r["value"][1])))
            d["engine"] = r["metric"].get("engine")
    except Exception as e:
        errors.append(f"llm up: {str(e)[:100]}")
    for k in LLM_SPARK:
        try:
            for r in _pr(LLM_Q[k], minutes=30, step=60):
                h, m = r["metric"].get("host", "?"), r["metric"].get("model_name", "?")
                put(h, m, "spark", hosts.get(h, {}).get("servers", {}).get(m, {}).get("spark") or {})
                hosts[h]["servers"][m]["spark"][k] = [[int(float(t)), _num(v)] for t, v in r["values"]]
        except Exception as e:
            errors.append(f"llm spark {k}: {str(e)[:100]}")
    for h in hosts.values():
        h["servers"] = list(h["servers"].values())
    topo = llm_topology()
    for hn, t in (topo.get("hosts") or {}).items():
        d = hosts.setdefault(hn, {"servers": []})
        d["topology"] = t.get("servers", [])
        for srv in t.get("servers", []):
            if srv.get("role") == "worker":
                d.setdefault("worker_for", srv.get("worker_for"))
                d.setdefault("worker_model", srv.get("model"))
    return hosts, errors


def _prom(path, **params):
    url = f"{PROM}/api/v1/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read())
    if d.get("status") != "success":
        raise RuntimeError(d.get("error", "prometheus error"))
    return d["data"]["result"]


_MP = re.compile(r"\bnoctyx_(?=[a-z])")


def mp(text):
    """Noctyx's metric names (noctyx_…) in the site's prefix (paths.metric_prefix): what /metrics emits and
    what the console queries Prometheus for, so a site keeps its series and dashboards."""
    return text if P["metric_prefix"] == "noctyx" else _MP.sub(P["metric_prefix"] + "_", text)


def _pq(q):
    return _prom("query", query=mp(q))


def _pr(q, minutes=60, step=60):
    now = time.time()
    return _prom("query_range", query=mp(q), start=now - minutes * 60, end=now, step=step)


def _num(v):
    """Prometheus NaN/±Inf (e.g. tokens ÷ zero decode time on an idle server)
    -> None, never Infinity/NaN in the JSON we serve."""
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else round(f, 3)
    except (TypeError, ValueError):
        return None


def _series(res, key="host"):
    """range result -> {label value: [[t, v], ...]}"""
    return {r["metric"].get(key, "?"): [[int(float(t)), _num(v)] for t, v in r["values"]] for r in res}


_FLEET_CACHE = {"ts": 0, "data": None}


def fleet_metrics():
    """Per-host numbers + 30-min sparklines for the Fleet tiles (cached 10 s)."""
    if time.time() - _FLEET_CACHE["ts"] < 10 and _FLEET_CACHE["data"]:
        return _FLEET_CACHE["data"]
    hosts, errors = {}, []
    for k, q in FLEET_Q.items():
        try:
            for r in _pq(q):
                hosts.setdefault(r["metric"].get("host", "?"), {})[k] = _num(r["value"][1])
        except Exception as e:
            errors.append(f"{k}: {str(e)[:120]}")
    for k in FLEET_SPARK:
        try:
            for h, pts in _series(_pr(FLEET_Q[k], minutes=30, step=60)).items():
                hosts.setdefault(h, {}).setdefault("spark", {})[k] = pts
        except Exception as e:
            errors.append(f"spark {k}: {str(e)[:120]}")
    llm, lerr = llm_metrics()
    errors += lerr
    for h, d in llm.items():
        hosts.setdefault(h, {})["llm"] = d
    hy = hygiene_report()
    for host, d in (hy.get("hosts") or {}).items():
        items = [i for i in d.get("items", []) if not i.get("fixed")]
        hosts.setdefault(host, {})["hygiene"] = {"items": len(items), "warnings": sum(1 for i in items if i["severity"] == "warning")}
    extras, xerr = fleet_extras()
    errors += xerr
    order = [h["name"] for h in CFG["hosts"] if h.get("enabled", True) is not False and h.get("address")]
    data = {"ts": time.time(), "hosts": hosts, "order": order, "fleet": extras, "errors": errors, "ok": not errors or bool(hosts)}
    _FLEET_CACHE.update(ts=time.time(), data=data)
    return data


def host_metrics(host, tab):
    """One Machine Report tab's live data. `host` is validated by the caller."""
    H = f'host="{host}"'
    out = {"ts": time.time(), "host": host, "tab": tab}
    if tab == "overview":
        out["series"] = {k: next(iter(_series(_pr(q.replace('host!=""', H), 60, 30)).values()), [])
                         for k, q in FLEET_Q.items()
                         if k in ("cpu", "mem_used", "net_tx", "net_rx", "disk_io", "gpu_util", "load1")}
        out["mem_total"] = next((_num(r["value"][1]) for r in _pq(f"node_memory_MemTotal_bytes{{{H}}}")), None)
    elif tab == "network":
        info = {r["metric"]["device"]: r["metric"] for r in _pq(f"node_network_info{{{_NIC},{H}}}")}
        speed = {r["metric"]["device"]: _num(r["value"][1]) for r in _pq(f"node_network_speed_bytes{{{_NIC},{H}}}")}
        errs = {r["metric"]["device"]: _num(r["value"][1]) for r in _pq(
            f"sum by(device)(increase(node_network_receive_errs_total{{{_NIC},{H}}}[1h]) + increase(node_network_transmit_errs_total{{{_NIC},{H}}}[1h]))")}
        drops = {r["metric"]["device"]: _num(r["value"][1]) for r in _pq(
            f"sum by(device)(increase(node_network_receive_drop_total{{{_NIC},{H}}}[1h]) + increase(node_network_transmit_drop_total{{{_NIC},{H}}}[1h]))")}
        tx = _series(_pr(f"rate(node_network_transmit_bytes_total{{{_NIC},{H}}}[1m]) * 8", 60, 60), "device")
        rx = _series(_pr(f"rate(node_network_receive_bytes_total{{{_NIC},{H}}}[1m]) * 8", 60, 60), "device")
        out["nics"] = [{"device": d, "operstate": m.get("operstate"), "duplex": m.get("duplex"),
                        "address": m.get("address"),
                        # down/virtual links report -1: unknown, not a speed
                        "speed_bps": speed[d] * 8 if (speed.get(d) or 0) > 0 else None,
                        "errors_1h": errs.get(d), "drops_1h": drops.get(d),
                        "tx": tx.get(d, []), "rx": rx.get(d, [])}
                       for d, m in sorted(info.items())]
    elif tab == "storage":
        size = {r["metric"]["mountpoint"]: r["metric"] | {"v": _num(r["value"][1])}
                for r in _pq(f"node_filesystem_size_bytes{{{_FS},{H}}}")}
        avail = {r["metric"]["mountpoint"]: _num(r["value"][1]) for r in _pq(f"node_filesystem_avail_bytes{{{_FS},{H}}}")}
        files = {r["metric"]["mountpoint"]: _num(r["value"][1]) for r in _pq(f"node_filesystem_files{{{_FS},{H}}}")}
        ffree = {r["metric"]["mountpoint"]: _num(r["value"][1]) for r in _pq(f"node_filesystem_files_free{{{_FS},{H}}}")}
        out["filesystems"] = [{"mount": m, "device": v.get("device"), "fstype": v.get("fstype"),
                               "size": v["v"], "avail": avail.get(m), "files": files.get(m), "files_free": ffree.get(m)}
                              for m, v in sorted(size.items())]
        out["io"] = _series(_pr(f"rate(node_disk_read_bytes_total{{{_DSK},{H}}}[1m]) + rate(node_disk_written_bytes_total{{{_DSK},{H}}}[1m])", 60, 60), "device")
        nv = {}
        for metric, key in (("nvme_device_info", "info"), ("nvme_percentage_used_ratio", "wear"),
                            ("nvme_temperature_celsius", "temp"), ("nvme_media_errors_total", "media_errors"),
                            ("nvme_critical_warning", "critical"), ("nvme_available_spare_ratio", "spare"),
                            ("nvme_power_on_hours_total", "hours"), ("nvme_unsafe_shutdowns_total", "unsafe")):
            for r in _pq(f"{metric}{{{H}}}"):
                d = nv.setdefault(r["metric"].get("device", "?"), {})
                d[key] = r["metric"] if key == "info" else _num(r["value"][1])
        out["nvme"] = [{"device": d, "model": (v.get("info") or {}).get("model"),
                        "serial": (v.get("info") or {}).get("serial"), **{k: x for k, x in v.items() if k != "info"}}
                       for d, v in sorted(nv.items())]
    elif tab == "gpu":
        out["info"] = [r["metric"] for r in _pq(f"noctyx_gpu_info{{{H}}}")]
        out["series"] = {k: _series(_pr(f"{m}{{{H}}}", 60, 30), "gpu") for k, m in (
            ("util", "noctyx_gpu_utilization_percent"), ("temp", "noctyx_gpu_temperature_celsius"),
            ("power", "noctyx_gpu_power_watts"), ("clock", "noctyx_gpu_sm_clock_mhz"))}
        out["mem_used"] = {r["metric"].get("gpu"): _num(r["value"][1]) for r in _pq(f"noctyx_gpu_memory_used_bytes{{{H}}}")}
        out["mem_total"] = {r["metric"].get("gpu"): _num(r["value"][1]) for r in _pq(f"noctyx_gpu_memory_total_bytes{{{H}}}")}
        out["proc_mem"] = next((_num(r["value"][1]) for r in _pq(f"noctyx_gpu_process_memory_bytes{{{H}}}")), None)
    elif tab == "llm":
        out["series"] = {k: _series(_pr(_host_q(LLM_Q[k], host), 60, 30), "model_name")
                         for k in ("gen_tps", "prompt_tps", "running", "waiting", "kv_pct", "ttft_p50", "ttft_p95", "e2e_p50", "itl_p50", "prefix_hit",
                                   "decode_tps", "prefill_tps", "prefill_share", "prefill_p50", "queue_p50")}
        out["now"] = {k: {r["metric"].get("model_name"): _num(r["value"][1]) for r in _pq(_host_q(q, host))}
                      for k, q in LLM_Q.items()}
        out["topology"] = (llm_topology().get("hosts") or {}).get(host, {}).get("servers", [])
        out["up"] = {r["metric"].get("port"): _num(r["value"][1]) for r in _pq(f'up{{job="llm",host="{host}"}}')}
    elif tab == "health":
        out["failed_units"] = sorted(r["metric"].get("name") for r in _pq(f'node_systemd_unit_state{{state="failed",{H}}} == 1'))
        one = lambda q: next((_num(r["value"][1]) for r in _pq(q)), None)   # noqa: E731
        out["reboot"] = one(f"max(node_reboot_required{{{H}}})")
        out["upgrades"] = one(f"sum(apt_upgrades_pending{{{H}}})") or 0
        out["autoremove"] = one(f"apt_autoremove_pending{{{H}}}")
        out["time_offset_s"] = one(f"node_timex_offset_seconds{{{H}}}")
        out["time_synced"] = one(f"node_timex_sync_status{{{H}}}")
        out["boot_ts"] = one(f"node_boot_time_seconds{{{H}}}")
        out["textfile_age_s"] = one(f"time() - noctyx_textfile_last_run_timestamp_seconds{{{H}}}")
        out["exporter_up"] = one(f"max(up{{{H}}})")
        out["containers"] = sorted(({"name": r["metric"].get("name"), "image": r["metric"].get("image")}
                                    for r in _pq(f"noctyx_container_running{{{H}}}")), key=lambda x: x["name"] or "")
        # the early warnings' sources (metrics/rules.yml "signals"): sshd, the RoCE fabric, NFS mounts
        out["ssh"] = {"failed_24h": one(f"increase(noctyx_ssh_failed_logins_total{{{H}}}[1d])"),
                      "invalid_24h": one(f"increase(noctyx_ssh_invalid_user_attempts_total{{{H}}}[1d])"),
                      "accepted_24h": one(f"sum(increase(noctyx_ssh_accepted_logins_total{{{H}}}[1d]))"),
                      "known": one(f"noctyx_ssh_known_sources{{{H}}}"),
                      "new": [{"addr": r["metric"].get("addr"), "user": r["metric"].get("user"), "ts": _num(r["value"][1])}
                              for r in _pq(f"noctyx_ssh_new_source_login_timestamp_seconds{{{H}}}")]}
        fab = {}
        for key, q in (("state", f"node_infiniband_state_id{{{H}}}"), ("rate", f"node_infiniband_rate_bytes_per_second{{{H}}}"),
                       ("errors_1h", f"increase(node_infiniband_link_downed_total{{{H}}}[1h]) + increase(node_infiniband_symbol_error_total{{{H}}}[1h]) + increase(node_infiniband_port_errors_received_total{{{H}}}[1h]) + increase(node_infiniband_link_error_recovery_total{{{H}}}[1h])"),
                       ("retrans_s", f"rate(node_infiniband_roce_adp_retransmits_total{{{H}}}[10m]) + rate(node_infiniband_local_ack_timeout_errors_total{{{H}}}[10m]) + rate(node_infiniband_out_of_sequence_packets_received_total{{{H}}}[10m])"),
                       ("rx_bps", f"rate(node_infiniband_port_data_received_bytes_total{{{H}}}[5m])"),
                       ("tx_bps", f"rate(node_infiniband_port_data_transmitted_bytes_total{{{H}}}[5m])")):
            for r in _pq(q):
                fab.setdefault(r["metric"].get("device"), {"device": r["metric"].get("device")})[key] = _num(r["value"][1])
        out["fabric"] = sorted(fab.values(), key=lambda x: (x.get("state") != 4, x["device"] or ""))
        nfs = {}
        for key, q in (("ok", "noctyx_nfs_probe_ok"), ("write", "noctyx_nfs_probe_write_bytes_per_second"),
                       ("read", "noctyx_nfs_probe_read_bytes_per_second"), ("small", "noctyx_nfs_probe_small_op_seconds")):
            for r in _pq(f"{q}{{{H}}}"):
                nfs.setdefault(r["metric"].get("mountpoint"), {"mountpoint": r["metric"].get("mountpoint"),
                                                               "source": r["metric"].get("source")})[key] = _num(r["value"][1])
        out["nfs"] = list(nfs.values())
        out["nfs_probe_ts"] = one(f"noctyx_nfs_probe_last_run_timestamp_seconds{{{H}}}")
        out["nfs_running_s"] = one(f"noctyx_nfs_probe_running_seconds{{{H}}}")
    else:
        raise ValueError("unknown tab")
    return out


# ── snapshot browser ─────────────────────────────────────────────────────
# Read-only restic against the NAS repos, --no-lock (writes nothing into the
# repo), with a private cache so directory listings are fast after the first.
RESTIC_CACHE = UI_STATE / "restic-cache"
_SNAPS_CACHE: dict[str, dict] = {}


def _host_repo(host):
    h = next((x for x in CFG["hosts"] if x["name"] == host), None)
    if not h or not h.get("repo"):
        raise ValueError("unknown host" if not h else "monitored only: no snapshots")
    return h["repo"], CM.repo_dir(CFG, h["repo"])


def _restic_ro(host, args, timeout=120, text=True):
    repo, path = _host_repo(host)
    RESTIC_CACHE.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["restic", "-r", path, "--password-file", f"{P['repo_keys']}/{repo}.key",
                           "--no-lock", "--cache-dir", str(RESTIC_CACHE), "--quiet"] + args,
                          capture_output=True, text=text, timeout=timeout)


def _profile_of(snap):
    return next((t[8:] for t in snap.get("tags") or [] if t.startswith("profile-")), "?")


def host_snapshots(host, max_age=60):
    ent = _SNAPS_CACHE.get(host)
    if ent and time.time() - ent["ts"] < max_age:
        return ent["data"]
    r = _restic_ro(host, ["snapshots", "--host", host, "--json"], timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[-200:] or f"rc={r.returncode}")
    audited = _audited_snapshot_ids()
    out = []
    for s in json.loads(r.stdout or "[]"):
        sm = s.get("summary") or {}
        out.append({"id": s["id"], "short_id": s.get("short_id") or s["id"][:8], "ts": CM._restic_ts(s["time"]),
                    "profile": _profile_of(s), "paths": [p for p in s.get("paths", []) if not p.startswith(f"{P['node_lib']}/fsmeta")],
                    "recorded": (s.get("short_id") or s["id"][:8]) in audited or (s.get("original") or "")[:8] in audited,
                    "files_new": sm.get("files_new"), "files_changed": sm.get("files_changed"),
                    "files_unmodified": sm.get("files_unmodified"), "data_added": sm.get("data_added"),
                    "total_bytes": sm.get("total_bytes_processed"), "total_files": sm.get("total_files_processed"),
                    "duration": (CM._restic_ts(sm["backup_end"]) - CM._restic_ts(sm["backup_start"]))
                                if sm.get("backup_end") and sm.get("backup_start") else None})
    out.sort(key=lambda x: -x["ts"])
    _SNAPS_CACHE[host] = {"ts": time.time(), "data": out}
    return out


def browse(host, snap, path):
    """Direct children of `path` in `snap` (restic ls is non-recursive by default)."""
    r = _restic_ro(host, ["ls", "--json", snap, path], timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[-200:] or f"rc={r.returncode}")
    entries, snapshot = [], None
    for line in (r.stdout or "").splitlines():
        try:
            n = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "struct_type" in n and n.get("struct_type") == "snapshot" or ("hostname" in n and "tree" in n):
            snapshot = {"id": n.get("short_id") or (n.get("id") or "")[:8], "time": n.get("time")}
            continue
        if n.get("path") == path.rstrip("/") or not n.get("name"):
            continue                                   # the directory itself
        entries.append({"name": n["name"], "type": n.get("type"), "size": n.get("size"),
                        "mtime": CM._restic_ts(n["mtime"]) if n.get("mtime") else None,
                        "mode": n.get("permissions"), "target": n.get("linktarget")})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"host": host, "snapshot": snapshot, "path": path, "entries": entries}


def find_in_snapshots(host, pattern, snap=None, limit=200):
    args = ["find", "--json", "--host", host] + (["--snapshot", snap] if snap else []) + [pattern]
    r = _restic_ro(host, args, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[-200:] or f"rc={r.returncode}")
    out = []
    for grp in json.loads(r.stdout or "[]"):
        sid = (grp.get("snapshot") or "")[:8]
        for m in grp.get("matches", []):
            out.append({"snapshot": sid, "path": m.get("path"), "type": m.get("type"), "size": m.get("size"),
                        "mtime": CM._restic_ts(m["mtime"]) if m.get("mtime") else None})
            if len(out) >= limit:
                return {"matches": out, "truncated": True}
    return {"matches": out, "truncated": False}


# ── local LLM assistant ─────────────────────────────────────────────────
# Uses one of the cluster's own LLM servers (the site config's `assistant`) to explain findings, triage alerts and write a morning brief. Every
# prompt is grounded in data we already hold; the answer is text shown to the
# operator with the model's name on it. Nothing here executes anything.
SYSTEM_PROMPT = ("You are the assistant inside Noctyx, a deployment, backup and cluster-health console for a small GPU "
                 "cluster (Linux nodes serving LLMs, backups on a NAS). Write for the "
                 "operator: plain, precise, brief. Use ONLY the data given; if something is not in the data, "
                 "say 'not in the data' rather than guessing. Mark any guess as a guess. Never recommend "
                 "rebooting a node that serves a model unless the data shows a reboot is required. Prefer the "
                 "standard remedy for the platform (systemd, apt, Docker, restic).")
DIGEST_FILE = UI_STATE / "digest.json"


# ── demo mode (noctyx demo): every API answer from a captured, scrubbed site, its times moved to now ──
DEMO = os.environ.get("NOCTYX_DEMO")
_DEMO = None
_TS_EPOCH = re.compile(r"(?<![\d.])1[6-9]\d{8}(?:\.\d+)?(?![\d])")
_TS_ISO = re.compile(r"20\d\d-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


def demo_answer(path):
    """The captured answer for path (exact, then without its query, then any tab of the same report), with
    every timestamp shifted by (now - capture time), so ages read as they did when it was captured."""
    global _DEMO
    if _DEMO is None:
        _DEMO = json.loads(pathlib.Path(DEMO).read_text())
    fx = _DEMO["fixtures"]
    base = path.split("?")[0]
    v = fx.get(path)
    if v is None:
        v = next((fx[k] for k in fx if k.split("?")[0] == base), None)
    if v is None:
        return None
    if base == "/api/v1/overview":
        v = dict(v, demo=True)
    d = time.time() - _DEMO["captured"]
    text = _TS_EPOCH.sub(lambda m: (f"{float(m.group(0)) + d:.3f}" if "." in m.group(0) else str(int(int(m.group(0)) + d))), json.dumps(v))

    def iso(m):
        s = m.group(0)
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return s
        out = (dt + datetime.timedelta(seconds=d)).isoformat(timespec="seconds" if len(s) > 16 else "minutes")
        return out.replace("+00:00", "Z") if s.endswith("Z") else out if ("+" in s[10:] or s.count("-") > 2) else out.split("+")[0]
    return _TS_ISO.sub(iso, text).encode()


ASSETS = {"favicon.svg": "image/svg+xml", "banner.svg": "image/svg+xml", "donate-qr.png": "image/png",
          "fonts/michroma.woff2": "font/woff2", "fonts/dseg7.woff2": "font/woff2", "fonts/LICENSES.txt": "text/plain; charset=utf-8"}
DONATE = "bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff"


def about_info():
    """UI › About: what is running, under which license, and how to support it."""
    from noctyx import __version__
    c = CM.cfg()
    rc = CM.recipes_cfg(c)
    return {"version": __version__, "build": UI_BUILD, "python": sys.version.split()[0],
            "restic": str(c.get("restic_version") or "0.19.1"),
            "site": {"head": c["server"].get("hostname"), "nas": CM.nas_name(c),
                     "hosts": len([h for h in c["hosts"] if h.get("enabled", True) is not False and h.get("address")]),
                     "slots": len(rc["slots"]), "recipes": len(rc["catalog"])},
            "repo": "https://github.com/BobClawblaw/noctyx", "donate": DONATE}


def assistant_cfg():
    a = CM.cfg().get("assistant") or {}
    if not a.get("enabled", False):
        return None
    return a


def _assistant_endpoints(a):
    """Preferred endpoint first, then any serving endpoint discovery knows."""
    eps = [(a.get("url"), a.get("model"), a.get("key_host"))] if a.get("url") else []
    for hn, t in ((llm_topology().get("hosts") or {}) if a.get("fallback", True) else {}).items():
        for srv in t.get("servers", []):
            if srv.get("role") == "serving" and srv.get("engine") in ("vllm", "sglang", "llamacpp", "mtplx", "tensorfold"):
                base = srv.get("upstream") or (f"http://{srv['target']}" if srv.get("target") else None)
                if base:
                    eps.append((base + "/v1", srv.get("model"), hn))
    seen, out = set(), []
    for e in eps:
        e = (e[0], e[1], e[2] if len(e) > 2 else None)
        if e[0] and e[:2] not in seen:
            seen.add(e[:2]); out.append(e)
    return out


KEY_DIR = pathlib.Path(P["llm_keys"])


def assistant_summary():
    """UI › Config: the assistant's settings (never the key), and endpoints to pick from:
    every serving model discovery knows, plus each recipe slot's port."""
    a = dict(CM.cfg().get("assistant") or {})
    a = {k: v for k, v in a.items() if not k.startswith("_")}
    a["has_key"] = bool(a.get("key_host") and _api_key(a["key_host"]))
    eps = []
    for hn, tp in (llm_topology().get("hosts") or {}).items():
        for srv in tp.get("servers", []):
            if srv.get("role") == "serving" and srv.get("target"):
                eps.append({"url": (srv.get("upstream") or f"http://{srv['target']}") + "/v1", "model": srv.get("model"),
                            "host": hn, "engine": srv.get("engine")})
    try:
        c = CM.cfg()
        for n, s in CM.recipes_cfg(c)["slots"].items():
            h = next((x for x in c["hosts"] if x["name"] == s["head"]), None)
            u = f"http://{_addr(h)}:{s['port']}/v1" if h else None
            if u and not any(e["url"] == u for e in eps):
                eps.append({"url": u, "model": None, "host": s["head"], "engine": f"recipe slot {n}"})
    except Exception:
        pass
    return {**a, "endpoints": eps, "ask_builtin": COP.SYSTEM, "ask_fields": list(COP.FIELDS)}


def assistant_test(url, model, timeout=60):
    """One short completion against unsaved settings: {ok, text, model, ms, models} or {ok: False, error}."""
    url = (url or "").rstrip("/")
    if not CM._URL.match(url):
        return {"ok": False, "error": "url: http(s)://host:port/v1"}
    a = CM.cfg().get("assistant") or {}
    hdr = {"Content-Type": "application/json"}
    key = _api_key(a.get("key_host"))
    if key:
        hdr["Authorization"] = f"Bearer {key}"
    out = {"ok": False}
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/models", headers=hdr), timeout=10) as r:
            out["models"] = [m.get("id") for m in json.loads(r.read()).get("data", []) if m.get("id")]
    except Exception as e:
        out["models_error"] = str(e)[:120]
    body = {"model": model or (out.get("models") or [""])[0], "temperature": 0, "max_tokens": 24,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Reply with exactly: Noctyx assistant OK"}]}
    t0 = time.time()
    try:
        req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=max(5, min(int(timeout or 60), 600))) as r:
            d = json.loads(r.read())
        m = (d.get("choices") or [{}])[0].get("message") or {}
        out.update(ok=True, text=(m.get("content") or m.get("reasoning_content") or "").strip()[:300],
                   model=d.get("model") or body["model"], ms=round((time.time() - t0) * 1000))
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read()[:160].decode(errors='replace')}"
    except Exception as e:
        out["error"] = str(getattr(e, "reason", e))[:160]
    return out


def set_assistant_key(key):
    """Write (or with key None, remove) the assistant's API key: 0600 root, never logged or returned."""
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = KEY_DIR / "assistant"
    if key:
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key.strip() + "\n")
        os.replace(tmp, f)
    elif f.exists():
        f.unlink()
    r = subprocess.run([sys.executable, ORCH, "assistant-set", "--json", json.dumps({"key_host": "assistant" if key else ""})],
                       capture_output=True, text=True, timeout=60)
    return {"ok": r.returncode == 0, "error": (r.stderr or r.stdout).strip()[-200:] if r.returncode else None}


def _api_key(host):
    try:
        return open(f"{P['llm_keys']}/{host}").read().strip() if host else None
    except Exception:
        return None


_MODELS = {}


def _served_model(url, hdr):
    """The first model an OpenAI-compatible endpoint lists (cached 10 minutes), or None."""
    hit = _MODELS.get(url)
    if hit and hit[0] > time.time() - 600:
        return hit[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/models", headers=hdr), timeout=10) as r:
            m = next((x.get("id") for x in json.loads(r.read()).get("data", []) if x.get("id")), None)
    except Exception:
        m = None
    _MODELS[url] = (time.time(), m)
    return m


def assistant_ask(user, max_tokens=350, system=SYSTEM_PROMPT):
    """-> (text, model) or (None, reason). Tries the preferred endpoint, then fallbacks."""
    a = assistant_cfg()
    if not a:
        return None, "assistant disabled"
    last = "no endpoint"
    for url, model, host in _assistant_endpoints(a):
        body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Content-Type": "application/json"}
        # the assistant's key goes to the assistant's own url only; a discovered server gets its own key file
        key = _api_key(a.get("key_host")) if url == a.get("url") else _api_key(host)
        if key:
            hdr["Authorization"] = f"Bearer {key}"
        if not model:                    # "empty = the endpoint's first": what /v1/models lists first
            model = body["model"] = _served_model(url, hdr)
        try:
            req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=hdr)
            with urllib.request.urlopen(req, timeout=int(a.get("timeout_seconds", 60))) as r:
                d = json.loads(r.read())
            m = (d.get("choices") or [{}])[0].get("message") or {}
            text = (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()
            if text:
                return text, model
            last = f"{model}: empty answer"
        except Exception as e:
            last = f"{model}: {str(e)[:80]}"
    return None, last


def explain_hygiene(host, item_id):
    rep = hygiene_report()
    d = (rep.get("hosts") or {}).get(host) or {}
    it = next((i for i in d.get("items", []) if i.get("id") == item_id), None)
    if not it:
        return {"error": "no such finding"}
    if it.get("explain"):
        return {"text": it["explain"], "model": it.get("explain_model"), "cached": True}
    h = next((x for x in CFG["hosts"] if x["name"] == host), {})
    f = host_facts(host, max_age=3600)
    ctx = (f"Host: {host} ({', '.join(f.get('os_lines') or [])}; {h.get('arch')}; "
           f"{'serves an LLM with vLLM' if any(s.get('model') for s in (llm_topology().get('hosts') or {}).get(host, {}).get('servers', [])) else 'no LLM server'}).\n"
           f"Finding: {it['title']}\nSeverity: {it['severity']}\nDetail: {it.get('detail') or '(none)'}\n"
           f"Tier: {it.get('tier') or 'report only'} (auto = Noctyx fixes it nightly; manual = operator clicks; report only = Noctyx will not touch it).\n"
           "Explain in 3-5 sentences: what this is, whether it matters, and the standard remedy if one applies.")
    text, model = assistant_ask(ctx)
    if not text:
        return {"error": model}
    it["explain"], it["explain_model"] = text, model
    try:
        HYGIENE_FILE.write_text(json.dumps(rep, indent=1))
    except Exception:
        pass
    return {"text": text, "model": model, "cached": False}


def triage_alert(rec):
    """Background: attach a 2-3 sentence triage to a firing alert record."""
    try:
        host = rec.get("host")
        M = ((fleet_metrics().get("hosts") or {}).get(host) or {}) if host else {}
        snap = {k: M.get(k) for k in ("cpu", "mem_used", "mem_total", "gpu_util", "gpu_temp", "root_pct", "failed_units", "reboot", "up") if M.get(k) is not None}
        llm = [{k: v for k, v in s.items() if k in ("model", "running", "waiting", "kv_pct", "gen_tps")} for s in ((M.get("llm") or {}).get("servers") or [])]
        ctx = (f"Alert: {rec.get('name')} (severity {rec.get('severity')}){' on host ' + host if host else ''}\n"
               f"Summary: {rec.get('summary')}\nFiring since: {rec.get('starts')}\n"
               f"Host metrics now: {json.dumps(snap)}\nLLM servers on host: {json.dumps(llm)}\n"
               "In 2-3 sentences: what this alert means, the most likely cause GIVEN THIS DATA (say if the data is insufficient), and the first thing to check.")
        text, model = assistant_ask(ctx, max_tokens=220)
        if text:
            rec["triage"], rec["triage_model"] = text, model
            with ALERT_LOG.open("a") as f:
                f.write(json.dumps({**rec, "event": "alert-triage"}) + "\n")
    except Exception:
        pass


COP.bind(sys.modules[__name__])                      # Ask Noctyx reads what the panels read


def digest(force=False):
    """Morning brief: a compact state summary -> 5-8 lines from the assistant. Cached 1 h."""
    try:
        cur = json.loads(DIGEST_FILE.read_text())
        if not force and time.time() - cur.get("ts", 0) < 3600:
            return cur
    except Exception:
        cur = None
    sc = schedule_summary()
    fm = fleet_metrics()
    hy = hygiene_report()
    al = alerts_view()
    I = (fm.get("fleet") or {}).get("infra") or {}
    rows = [{"host": r["host"], "profile": r["profile"], "health": r.get("health"),
             "note": "on schedule" if r.get("health") == "ok" else r.get("why")}
            for r in sc.get("rows", []) if r.get("enabled")]
    llm = {h: [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in s.items() if k in ("model", "running", "gen_tps", "requests_1h", "tokens_1h")}
               for s in ((d.get("llm") or {}).get("servers") or [])] for h, d in (fm.get("hosts") or {}).items()}
    hyg = {h: [f"{i['severity']}: {i['title']}" for i in d.get("items", []) if not i.get("fixed")] for h, d in (hy.get("hosts") or {}).items()}
    from noctyx import heal
    hl = heal.summary()
    healed = [f"{r['host']}: {r['what']} -> {r['how']} ({'repaired' if r['ok'] else 'failed: ' + r['detail']})" for r in hl["repairs"]]
    open_ = [f"{x['host']}: {x['what']}" for x in hl["problems"] if x.get("needs_person")]
    nodes = {h: {k: d.get(k) for k in ("up", "gpu_util", "gpu_temp", "root_pct", "failed_units", "reboot")} for h, d in (fm.get("hosts") or {}).items()}
    ctx = ("Write the operator's morning brief for the cluster as 5-8 short lines (no headings, no markdown tables). "
           "Lead with anything wrong; then backups, then the LLM servers, then housekeeping; end with one line on what deserves attention today, if anything.\n"
           "Rules: the backup 'health' field is authoritative (ok = on schedule; do not call an ok host late or behind). "
           "'reboot' on a node is an advisory package flag that may lag by 15 minutes and is NOT a reason to reboot a node serving a model; mention it at most as 'flagged'. "
           "Housekeeping 'info' items are routine; only 'warning' items deserve attention. "
           "Self-repairs are what Noctyx fixed on its own: mention them in one line; problems it could not repair need the operator.\n"
           f"UTC now: {_utcnow()}\nBackups (health vs schedule): {json.dumps(rows)}\n"
           f"Active alerts: {json.dumps(al.get('active'))}\nNodes: {json.dumps(nodes)}\nLLM servers: {json.dumps(llm)}\n"
           f"Last 24 h: {json.dumps({k: I.get(k) for k in ('tokens_24h', 'requests_24h', 'gpu_wh_24h', 'nas_free', 'backups_ok', 'backups_total')})}\n"
           f"NAS RAID: {json.dumps(I.get('raid'))} sync: {json.dumps(I.get('raid_sync'))}\nOpen hygiene findings: {json.dumps(hyg)}\n"
           f"Self-repairs in the last 24 h: {json.dumps(healed)}\nProblems Noctyx could not repair: {json.dumps(open_)}")
    text, model = assistant_ask(ctx, max_tokens=420)
    out = {"ts": time.time(), "text": text, "model": model if text else None, "error": None if text else model}
    if text or not cur:
        try:
            UI_STATE.mkdir(parents=True, exist_ok=True)
            DIGEST_FILE.write_text(json.dumps(out))
        except Exception:
            pass
        return out
    return {**cur, "stale_error": model}


# ── Noctyx's own metrics + alert intake ──────────────────────────────
# /metrics is scraped by the head node's Prometheus (job prom_job_console);
# metrics/rules.yml turns these into alerts, Alertmanager
# posts them back to /api/v1/alert-hook (loopback only) so the UI shows them
# and audit.jsonl records them, then on to whatever external channel is set.
ALERTMANAGER = os.environ.get("NOCTYX_ALERTMANAGER", "http://127.0.0.1:9093")
ALERT_LOG = UI_STATE / "alerts.jsonl"
# Labels stable enough to identify one alert for a silence. Volatile ones (latest/installed,
# instance, job, monitor, severity, upgrade text) are deliberately excluded: a silence built
# on them would stop matching the moment a version number changed.
SILENCE_MATCH_KEYS = ("alertname", "host", "component", "profile", "cmd", "repo", "url", "tier", "array")
SILENCE_MAX_MIN = 90 * 24 * 60
_METRICS_CACHE = {"ts": 0, "text": ""}
_RAID_CACHE = {"ts": 0, "data": None}
HEALTH_CODE = {"ok": 0, "late": 1, "stale": 2, "never": 3}


def _lbl(**kw):
    return "{" + ",".join(f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                         for k, v in kw.items() if v is not None) + "}"


def nas_raid():
    """/proc/mdstat on the NAS over ssh (root's key is authorised as `agent`):
    active/total members and resync progress of the data array (cached 60 s)."""
    if time.time() - _RAID_CACHE["ts"] < 60:
        return _RAID_CACHE["data"]
    data = None
    try:
        r = CM.nas_ssh(CM.cfg(), "cat /proc/mdstat", timeout=15)
        txt = (r.stdout or b"").decode()
        arrays = []
        for m in re.finditer(r"^(md\d+) : active (\w+) .*?\n\s+\d+ blocks.*?\[(\d+)/(\d+)\] \[([U_]+)\]"
                             r"(?:\n\s+\[[=>.]+\]\s+(\w+)\s*=\s*([\d.]+)%)?", txt, re.M | re.S):
            arrays.append({"array": m.group(1), "level": m.group(2), "total": int(m.group(3)),
                           "active": int(m.group(4)), "sync_action": m.group(6),
                           "sync_pct": float(m.group(7)) if m.group(7) else None})
        data = {"ok": bool(arrays), "arrays": arrays}
    except Exception as e:
        data = {"ok": False, "arrays": [], "error": str(e)[:120]}
    _RAID_CACHE.update(ts=time.time(), data=data)
    return data


def metrics_text():
    """Prometheus exposition of Noctyx state. Each source is isolated:
    one failing source drops its series and flips noctyx_source_ok, never the
    whole scrape."""
    if time.time() - _METRICS_CACHE["ts"] < 30:
        return _METRICS_CACHE["text"]
    out, now = [], time.time()
    def src(name, fn):
        try:
            fn()
            out.append(f'noctyx_source_ok{_lbl(source=name)} 1')
        except Exception as e:
            out.append(f'# {name}: {str(e)[:100]}')
            out.append(f'noctyx_source_ok{_lbl(source=name)} 0')

    def schedule():
        sc = schedule_summary()
        out.append("# HELP noctyx_backup_health_code 0 ok, 1 late, 2 stale, 3 never (vs the schedule).")
        out.append("# TYPE noctyx_backup_health_code gauge")
        for r in sc["rows"]:
            if not r.get("enabled"):
                continue
            l = _lbl(host=r["host"], profile=r["profile"])
            out.append(f"noctyx_backup_health_code{l} {HEALTH_CODE.get(r.get('health'), 3)}")
            out.append(f"noctyx_backup_due{l} {int(bool(r.get('due')))}")
            out.append(f"noctyx_backup_interval_seconds{l} {r['every']}")
            if r.get("last_ts"):
                out.append(f"noctyx_backup_last_success_timestamp_seconds{l} {r['last_ts']}")
            if r.get("next_ts") and r["next_ts"] != float("inf"):
                out.append(f"noctyx_backup_next_due_timestamp_seconds{l} {r['next_ts']}")
            if r.get("failed_at"):
                out.append(f"noctyx_backup_last_failure_timestamp_seconds{l} {r['failed_at']}")
        t = sc.get("tick") or {}
        out.append(f"noctyx_tick_timer_active {int(t.get('timer') == 'active')}")
        out.append(f"noctyx_tick_running {int(bool(t.get('running')))}")
        h = (sc.get("spindle") or {}).get("holder") or {}
        out.append(f"noctyx_spindle_busy{_lbl(spindle=(sc.get('spindle') or {}).get('name'))} {int(bool(h))}")

    def snapshots():
        sn = snapshot_summary()
        for host, d in sn.get("hosts", {}).items():
            for prof, v in d.get("profiles", {}).items():
                out.append(f"noctyx_snapshots_total{_lbl(host=host, profile=prof)} {v['count']}")
            out.append(f"noctyx_snapshot_listing_stale{_lbl(host=host)} {int(bool(d.get('stale')))}")
        for repo in sn.get("errors", {}):
            out.append(f"noctyx_repo_unreadable{_lbl(repo=repo)} 1")

    def storage():
        for t in capacity():
            l = _lbl(tier=t["tier"])
            out.append(f"noctyx_tier_mounted{l} {int(t['mounted'])}")
            out.append(f"noctyx_tier_free_bytes{l} {t['free_gb'] * 1e9:.0f}")
            out.append(f"noctyx_tier_used_bytes{l} {t['used_gb'] * 1e9:.0f}")
            for repo, gb in (t.get("repos_gb") or {}).items():
                out.append(f"noctyx_repo_bytes{_lbl(repo=repo)} {gb * 1e9:.0f}")

    def endpoint():
        c = CM.cfg()
        t0 = time.time()
        up = CM.rest_up(c)
        out.append(f"noctyx_rest_endpoint_up{_lbl(url=CM.rest_url(c))} {int(up)}")
        if up:                            # how long it took to answer (TLS and the first byte): the NAS's own latency
            out.append(f"noctyx_rest_endpoint_seconds{_lbl(url=CM.rest_url(c))} {time.time() - t0:.3f}")

    def raid():
        d = nas_raid()
        if not d or not d.get("ok"):
            raise RuntimeError((d or {}).get("error", "no mdstat"))
        for a in d["arrays"]:
            l = _lbl(array=a["array"], level=a["level"])
            out.append(f"noctyx_nas_raid_members_total{l} {a['total']}")
            out.append(f"noctyx_nas_raid_members_active{l} {a['active']}")
            out.append(f"noctyx_nas_raid_syncing{l} {int(a['sync_pct'] is not None)}")
            if a["sync_pct"] is not None:
                out.append(f"noctyx_nas_raid_sync_percent{l} {a['sync_pct']}")

    def jobs():
        seen = set()
        for r in runs(40):
            k = (r["cmd"], r.get("host") or "")
            if k in seen:
                continue
            seen.add(k)
            l = _lbl(cmd=r["cmd"], host=r.get("host") or "")
            out.append(f"noctyx_run_running{l} {int(r['state'] == 'running')}")
            out.append(f"noctyx_run_died{l} {int(r['state'] == 'died')}")
            if r.get("rc") is not None:
                out.append(f"noctyx_run_last_rc{l} {r['rc']}")
            if r.get("started_ts"):
                out.append(f"noctyx_run_last_start_timestamp_seconds{l} {r['started_ts']}")

    def hygiene():
        rep = hygiene_report()
        for host, d in (rep.get("hosts") or {}).items():
            items = [i for i in d.get("items", []) if not i.get("fixed")]
            for sev in ("warning", "info"):
                out.append(f"noctyx_hygiene_items{_lbl(host=host, severity=sev)} {sum(1 for i in items if i['severity'] == sev)}")
            out.append(f"noctyx_hygiene_report_timestamp_seconds{_lbl(host=host)} {d.get('ts', 0)}")

    def components():
        if not CM.VERSIONS.exists():      # not checked yet: the nightly run (or `noctyx versions`) makes it
            return
        inv = _versions()
        out.append("# HELP noctyx_component_behind 1: a critical component is older than Noctyx's pin, or missing.")
        out.append("# HELP noctyx_component_upgrade_available 1: a newer release than the pin exists upstream.")
        for host, rows in (inv.get("hosts") or {}).items():
            for r in rows:
                fx = r.get("fix") or {}           # the alert names the fix that works, not one that does nothing
                how = (f"sudo noctyx upgrade --host {host} --component {r['component']} (or the alert's upgrade button)"
                       if fx.get("argv") else fx.get("why") or r.get("upgrade") or "")
                l = _lbl(host=host, component=r["component"], installed=r.get("installed") or "",
                         pinned=r.get("pinned") or "", latest=r.get("latest") or "", upgrade=how)
                out.append(f"noctyx_component_behind{l} {int(r['state'] in ('behind', 'missing'))}")
                out.append(f"noctyx_component_upgrade_available{l} {int(r['state'] == 'upgrade')}")
        out.append(f"noctyx_versions_timestamp_seconds {inv.get('ts', 0)}")

    def repairs():
        from noctyx import heal
        st = heal.summary()
        if not st.get("ts"):              # the heal timer has not run yet
            return
        out.append("# HELP noctyx_heal_problem 1: something Noctyx manages is broken and noctyx heal cannot repair it.")
        out.append(f"noctyx_heal_last_run_timestamp_seconds {st['ts']}")
        out.append("# HELP noctyx_heal_frozen 1: too many repairs failed in a row; none run until noctyx heal --unfreeze.")
        out.append(f"noctyx_heal_frozen {int(bool(st.get('frozen')))}")
        out.append(f"noctyx_heal_actions_1h {st.get('actions_1h', 0)}")
        try:
            out.append(f"noctyx_heal_auto {int(heal.policy(CM.cfg())['auto'])}")
        except Exception:
            pass
        for pr in st["problems"]:
            if pr.get("needs_person"):
                out.append(f"noctyx_heal_problem{_lbl(check=pr['check'], host=pr['host'], what=pr['what'])} 1")
        n = {}
        for r in st["repairs"]:
            if r.get("ok"):
                n[(r["check"], r["host"])] = n.get((r["check"], r["host"]), 0) + 1
        for (chk, host), k in n.items():
            out.append(f"noctyx_heal_repairs_24h{_lbl(check=chk, host=host)} {k}")

    sources = [("schedule", schedule), ("snapshots", snapshots), ("storage", storage), ("endpoint", endpoint),
               ("runs", jobs), ("hygiene", hygiene), ("components", components), ("heal", repairs)]
    if CM.cfg()["server"].get("nas_ssh"):        # RAID state needs ssh to the NAS; a site without it has none
        sources.append(("nas_raid", raid))
    for name, fn in sources:
        src(name, fn)
    out.append(f"noctyx_metrics_generated_timestamp_seconds {now}")
    text = mp("\n".join(out) + "\n")
    _METRICS_CACHE.update(ts=now, text=text)
    return text


def record_alerts(payload):
    """Alertmanager webhook: one audit + alerts.jsonl line per alert."""
    recs = []
    for a in payload.get("alerts", []):
        rec = {"ts": _utcnow(), "event": "alert", "status": a.get("status"),
               "name": (a.get("labels") or {}).get("alertname"),
               "severity": (a.get("labels") or {}).get("severity"),
               "host": (a.get("labels") or {}).get("host"),
               "summary": (a.get("annotations") or {}).get("summary"),
               "starts": a.get("startsAt"), "ends": a.get("endsAt")}
        recs.append(rec)
    if recs:
        try:
            UI_STATE.mkdir(parents=True, exist_ok=True)
            with ALERT_LOG.open("a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            with AUDIT.open("a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
        except Exception:
            pass
        for r in recs:
            if r.get("status") == "firing" and assistant_cfg():
                threading.Thread(target=triage_alert, args=(r,), daemon=True).start()
            wake_heal(r)
    return len(recs)


_HEAL_WOKEN = {}               # host -> when an alert last ran noctyx heal for it
HEAL_WAKE_EVERY = 120


def wake_heal(rec, now=None, start=None):
    """A firing alert that has a playbook (heal.ALERT_CHECKS) runs `noctyx heal --host H` now instead of at the
    timer's next turn; at most once per host every 2 minutes. -> True when it started one."""
    from noctyx import heal
    chk, host = heal.ALERT_CHECKS.get(rec.get("name")), rec.get("host")
    if rec.get("status") != "firing" or not chk or host not in {h["name"] for h in CM.hosts(CM.cfg())}:
        return False
    pol = heal.policy(CM.cfg())
    if not pol["auto"] or pol["playbooks"].get(chk) == "off":
        return False
    now = now or time.time()
    if now - _HEAL_WOKEN.get(host, 0) < HEAL_WAKE_EVERY:
        return False
    _HEAL_WOKEN[host] = now
    argv = [sys.executable, ORCH, "heal", "--host", host, "--trigger", f"alert:{rec['name']}"]
    (start or (lambda a: threading.Thread(target=lambda: subprocess.run(
        a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90 * 60), daemon=True).start()))(argv)
    audit_write({"ts": _utcnow(), "event": "heal-wake", "alert": rec["name"], "host": host, "check": chk})
    return True


def alerts_view():
    active, err = [], None
    try:
        with urllib.request.urlopen(f"{ALERTMANAGER}/api/v2/alerts?active=true&silenced=false", timeout=5) as r:
            for a in json.loads(r.read()):
                active.append({"name": a["labels"].get("alertname"), "severity": a["labels"].get("severity"),
                               "host": a["labels"].get("host"), "summary": (a.get("annotations") or {}).get("summary"),
                               "since": a.get("startsAt"),
                               "fingerprint": a.get("fingerprint"),
                               # the stable labels that identify THIS alert, and nothing else. A silence
                               # built from these hits exactly one alert: `agent` can have two
                               # ComponentUpgradeAvailable (node_exporter and prometheus) sharing
                               # alertname+host, so matching on those alone would silence both.
                               "match": {k: v for k, v in a["labels"].items() if k in SILENCE_MATCH_KEYS},
                               "labels": {k: v for k, v in a["labels"].items() if k in ("profile", "cmd", "repo", "url", "component")}})
    except Exception as e:
        err = f"alertmanager: {str(e)[:100]}"
    recent = []
    try:
        recent = [json.loads(l) for l in ALERT_LOG.read_text().splitlines()[-25:] if l.strip()]
    except Exception:
        pass
    tri = {(r.get("name"), r.get("host")): r for r in recent if r.get("event") == "alert-triage"}
    for a in active:
        t = tri.get((a["name"], a["host"]))
        if t:
            a["triage"], a["triage_model"] = t.get("triage"), t.get("triage_model")
    recent = [r for r in recent if r.get("event") != "alert-triage"]
    inv = None
    for a in active:                      # a component alert carries what its buttons can do
        if a["name"] in ("ComponentUpgradeAvailable", "ComponentBehind") and a.get("host"):
            inv = inv or _versions()
            row = next((r for r in (inv.get("hosts") or {}).get(a["host"], [])
                        if r["component"] == (a.get("labels") or {}).get("component")), None)
            if row:
                a["fix"] = {k: row.get(k) for k in ("component", "state", "installed", "latest", "fix")}
                a["fix"]["hosts"] = sum(1 for rows in (inv.get("hosts") or {}).values() for r in rows
                                        if r["component"] == row["component"] and (r.get("fix") or {}).get("change"))
        if a["name"] == "NoctyxNeedsAttention":
            a["heal"] = True
    try:
        from noctyx import heal
        hp = {(p["check"], p["host"]): p for p in heal.summary()["problems"]}
        for a in active:
            p = hp.get((heal.ALERT_CHECKS.get(a["name"]), a.get("host")))
            if p:
                a["selfrepair"] = {k: p.get(k) for k in ("state", "why", "detail", "how", "check")}
    except Exception:
        pass
    sev = {"critical": 0, "warning": 1}
    active.sort(key=lambda a: (sev.get(a["severity"], 2), a["name"] or ""))
    sil, serr = silences_view()
    return {"ts": time.time(), "active": active, "silenced": sil, "recent": list(reversed(recent)),
            "error": err or serr}


def audit_write(rec):
    """Append one audit record, creating the audit directory if the install never made it.

    AUDIT lives under /var/lib/noctyx, which a source checkout may never create; the callers
    wrap writes in try/except, so a missing directory silently lost the whole trail.
    """
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a") as f:
            f.write(json.dumps({"ts": _utcnow(), **rec}) + "\n")
        return True
    except Exception:
        return False


def silences_view():
    """Silences still in force, so the UI can show what was dismissed and offer to undo it."""
    out, err = [], None
    try:
        with urllib.request.urlopen(f"{ALERTMANAGER}/api/v2/silences", timeout=5) as r:
            for s in json.loads(r.read()):
                if (s.get("status") or {}).get("state") != "active":
                    continue
                out.append({"id": s.get("id"), "matchers": s.get("matchers") or [],
                            "ends": s.get("endsAt"), "by": s.get("createdBy"),
                            "comment": s.get("comment")})
    except Exception as e:
        err = f"alertmanager: {str(e)[:100]}"
    out.sort(key=lambda s: s.get("ends") or "")
    return out, err


def create_silence(fingerprint, minutes, who):
    """Silence exactly ONE firing alert.

    The matchers are built here from the alert's own stable labels rather than sent up from the
    browser, so the client cannot silence something it never saw, and so two alerts that share an
    alertname and host stay independently dismissible.
    """
    with urllib.request.urlopen(f"{ALERTMANAGER}/api/v2/alerts?active=true&silenced=false", timeout=5) as r:
        alerts = json.loads(r.read())
    hit = next((a for a in alerts if a.get("fingerprint") == fingerprint), None)
    if not hit:
        raise ValueError("that alert is no longer firing")
    labels = hit.get("labels") or {}
    matchers = [{"name": k, "value": labels[k], "isRegex": False, "isEqual": True}
                for k in SILENCE_MATCH_KEYS if labels.get(k)]
    if not matchers:
        raise ValueError("alert carries no identifying labels")
    now = datetime.datetime.now(datetime.timezone.utc)
    body = {"matchers": matchers,
            "startsAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "endsAt": (now + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "createdBy": (who or "noctyx-console")[:64],
            "comment": "silenced from the console"}
    req = urllib.request.Request(f"{ALERTMANAGER}/api/v2/silences", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read()).get("silenceID"), hit


def expire_silence(sid):
    """End a silence early. Alertmanager keeps the record; the alert fires again if still true."""
    req = urllib.request.Request(f"{ALERTMANAGER}/api/v2/silence/{urllib.parse.quote(str(sid), safe='')}",
                                 method="DELETE")
    with urllib.request.urlopen(req, timeout=8) as r:
        r.read()


# ── per-host facts ───────────────────────────────────────────────────────
# Read-only inventory collected over SSH with root's key. The payload is a FIXED
# script — the host name only ever indexes the site config, never interpolated.
# Base64'd so nothing in the payload interacts with the remote shell's quoting.
FACTS_SCRIPT = r"""
echo "==os=="; (. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"); uname -srm
echo "==uptime=="; uptime -p 2>/dev/null; uptime -s 2>/dev/null
echo "==cpu=="; nproc; grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2-
echo "==mem=="; free -b | awk '/^Mem/{printf "%.1f %.1f\n", $3/1e9, $2/1e9}'
echo "==disk=="; df -B1 -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null | awk 'NR>1{printf "%s %s %s %s %s\n",$1,$2,$3,$5,$6}'
echo "==lsblk=="; lsblk -d -b -o NAME,SIZE,SERIAL,TYPE 2>/dev/null | awk '$4=="disk"{print $1,$2,$3}'
echo "==gpu=="; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null
echo "==gputmp=="; nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null
echo "==ip=="; hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+' | head -3 | paste -sd, -
echo "==agent=="; if [ -x @AGENT_BIN@ ]; then md5sum @AGENT_BIN@ | cut -c1-8; fi; (sudo -n ls @NODE_ETC@/ 2>/dev/null || ls @NODE_ETC@/ 2>/dev/null) | grep -o 'profile-[a-z]*\.d' | paste -sd, -
echo "==lastjobs=="; sudo -n tail -3 @NODE_LIB@/audit.jsonl 2>/dev/null || tail -3 @NODE_LIB@/audit.jsonl 2>/dev/null
echo "==load=="; cut -d" " -f1-3 /proc/loadavg
echo "==agentbusy=="; sudo -n fuser @NODE_LIB@/agent.lock 2>/dev/null | wc -w
echo "==progress=="; sudo -n cat @NODE_LIB@/progress 2>/dev/null
echo "==backuplog=="; sudo -n stat -c %Y @NODE_LIB@/last-backup.log 2>/dev/null; sudo -n tail -n 8 @NODE_LIB@/last-backup.log 2>/dev/null
"""
FACTS_SCRIPT = CM.render(FACTS_SCRIPT)                 # the site's node paths
FACTS_B64 = base64.b64encode(FACTS_SCRIPT.encode()).decode()
# macOS variant (no /proc, no lsblk, no nvidia-smi): same section names, parsed by the same code
FACTS_MAC = r"""
echo "==os=="; echo "macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion))"; uname -srm
echo "==uptime=="; uptime | sed 's/.*up /up /;s/,.*//' ; date -r "$(sysctl -n kern.boottime | sed 's/^{ sec = \([0-9]*\).*/\1/')" "+%F %T"
echo "==cpu=="; sysctl -n hw.ncpu; sysctl -n machdep.cpu.brand_string
echo "==mem=="; vm_stat | awk -v total=$(sysctl -n hw.memsize) 'BEGIN{ps=16384} /page size of/{ps=$8} /Pages active/{a=$3} /Pages wired/{w=$4} /Pages occupied by compressor/{c=$5} END{gsub(/\./,"",a);gsub(/\./,"",w);gsub(/\./,"",c); printf "%.1f %.1f\n", (a+w+c)*ps/1e9, total/1e9}'
echo "==disk=="; df -k / /System/Volumes/Data 2>/dev/null | awk 'NR>1{printf "%s %d %d %s %s\n",$1,$2*1024,$3*1024,$5,$9}'
echo "==lsblk=="; diskutil list physical 2>/dev/null | awk '/^\/dev\//{d=$1} /^ *0:/{gsub(/\*/,"",$0); for(i=1;i<=NF;i++) if($i ~ /^[0-9.]+$/ && $(i+1) ~ /^[KMGT]B$/){m=$i; u=$(i+1)}; s=m*(u=="TB"?1e12:u=="GB"?1e9:1e6); print substr(d,6), int(s), "-"}'
echo "==gpu=="; system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/{m=$2} /Total Number of Cores/{c=$2} END{if(m) print m", "c" GPU cores"}'
echo "==gputmp=="; echo ""
echo "==ip=="; ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null
echo "==agent=="; if [ -x @AGENT_BIN@ ]; then md5 -q @AGENT_BIN@ | cut -c1-8; fi; (sudo -n ls @NODE_ETC@/ 2>/dev/null || ls @NODE_ETC@/ 2>/dev/null) | grep -o 'profile-[a-z]*\.d' | paste -sd, -
echo "==lastjobs=="; sudo -n tail -3 @NODE_LIB@/audit.jsonl 2>/dev/null || tail -3 @NODE_LIB@/audit.jsonl 2>/dev/null
echo "==load=="; sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1,$2,$3}'
echo "==agentbusy=="; sudo -n lsof -t @NODE_LIB@/agent.lock 2>/dev/null | wc -w
echo "==progress=="; sudo -n cat @NODE_LIB@/progress 2>/dev/null
echo "==backuplog=="; sudo -n stat -f %m @NODE_LIB@/last-backup.log 2>/dev/null; sudo -n tail -n 8 @NODE_LIB@/last-backup.log 2>/dev/null
echo "==timemachine=="; tmutil destinationinfo 2>/dev/null | awk -F' : ' '/^Name/{print "dest", $2; exit}'
  l=$(sudo -n tmutil latestbackup 2>/dev/null || tmutil latestbackup 2>/dev/null); [ -n "$l" ] && echo "latest $(basename "$l")"
  tmutil status 2>/dev/null | awk -F' = ' '/ Running/{gsub(/;/,"",$2); print "running", $2}'
"""
FACTS_MAC = CM.render(FACTS_MAC)
FACTS_MAC_B64 = base64.b64encode(FACTS_MAC.encode()).decode()


def _parse_facts(out):
    f, sec = {}, None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("==") and s.endswith("=="):
            sec = s.strip("=")
            continue
        if not s or sec is None:
            continue
        if sec == "os":
            f.setdefault("os_lines", []).append(s)
        elif sec == "uptime":
            f.setdefault("uptime_lines", []).append(s)
        elif sec == "cpu":
            f.setdefault("cpu", []).append(s)
        elif sec == "mem":
            a = s.split()
            if len(a) == 2:
                f["mem_used_gb"], f["mem_total_gb"] = float(a[0]), float(a[1])
        elif sec == "disk":
            a = s.split()
            if len(a) == 5:
                f.setdefault("disks", []).append(
                    {"dev": a[0], "size_gb": round(int(a[1]) / 1e9),
                     "used_gb": round(int(a[2]) / 1e9), "pct": a[3], "mnt": a[4]})
        elif sec == "lsblk":
            a = s.split(None, 2)
            if len(a) >= 2 and a[1].isdigit():
                f.setdefault("blockdevs", []).append(
                    {"name": a[0], "size_gb": round(int(a[1]) / 1e9),
                     "serial": (a[2] if len(a) > 2 else "").strip()})
        elif sec == "gpu":
            f.setdefault("gpu", []).append(s)
        elif sec == "gputmp":
            a = s.split(",")
            if len(a) == 3:
                # GB10 unified-memory boxes return "[N/A]" for memory.used even
                # with nounits — parse each field defensively, don't require all.
                gi = lambda v: (int(v.strip()) if v.strip().lstrip("-").isdigit() else None)
                t, u, m = gi(a[0]), gi(a[1]), gi(a[2])
                if t is not None or u is not None:
                    f["gpu_stats"] = {"temp_c": t, "util_pct": u, "mem_used_mib": m}
        elif sec == "ip":
            f["ip"] = s
        elif sec == "agent":
            f.setdefault("agent", []).append(s)
        elif sec == "lastjobs":
            try:
                f.setdefault("last_jobs", []).append(json.loads(s))
            except Exception:
                pass
        elif sec == "load":
            f["loadavg"] = s
        elif sec == "timemachine":            # a Mac's own backups: Noctyx reports them, Time Machine makes them
            k, _, v = s.partition(" ")
            tm = f.setdefault("timemachine", {})
            if k == "latest":
                m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{6})", v)
                if m:
                    tm["latest_ts"] = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S").timestamp()
            elif k in ("dest", "running"):
                tm[k] = v.strip()
        elif sec == "agentbusy":
            f["agent_busy"] = s.isdigit() and int(s) > 0
        elif sec == "progress":
            f["progress"] = dict(kv.split("=", 1) for kv in s.split()[1:] if "=" in kv)
            f["progress"]["at"] = s.split()[0]
        elif sec == "backuplog":
            if "backuplog_mtime" not in f and s.isdigit():
                f["backuplog_mtime"] = int(s)
            else:
                f.setdefault("backuplog", []).append(line.rstrip())
    return f


def host_facts(name, max_age=120):
    cache_p = UI_STATE / "facts.json"
    try:
        cache = json.loads(cache_p.read_text())
    except Exception:
        cache = {}
    ent = cache.get(name)
    if ent and time.time() - ent.get("ts", 0) < max_age:
        return ent
    h = next((x for x in CFG["hosts"] if x["name"] == name and x.get("address")), None)
    if not h:
        return {"error": "unknown host"}
    s = CFG["server"]["ssh"]
    try:
        payload = FACTS_MAC_B64 if h.get("os") == "macos" else FACTS_B64
        remote = f"echo {payload} | base64 -d | bash"
        r = subprocess.run(
            ["sh", "-c", remote] if h.get("local") else
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new", "-i", s["key"],
             f"{h.get('ssh_user') or s['user']}@{_addr(h)}", remote],
            capture_output=True, text=True, timeout=30)
        facts = _parse_facts(r.stdout or "")
        if not facts.get("os_lines") and not facts.get("blockdevs"):
            facts = {"error": (r.stderr or "no output")[-200:].strip() or "no output"}
    except Exception as e:
        facts = {"error": str(e)[:200]}
    facts["ts"] = time.time()
    facts["address"] = _addr(h)
    facts["name"] = name
    cache[name] = facts
    try:
        UI_STATE.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(json.dumps(cache))
    except Exception:
        pass
    return facts


def probe_last():
    try:
        return json.loads((UI_STATE / "probe-last.json").read_text())
    except Exception:
        return {"ts": 0, "mbit": {}}


def _versions():
    """The last versions check, skipped releases marked, each row with how Noctyx would upgrade it (`fix`)."""
    try:
        inv = CM.versions_view()
    except Exception:
        inv = {}
    return inv if inv.get("hosts") is not None else {"ts": 0, "hosts": {}}


def overview():
    # monitored_only: metrics/facts/LLM view but no backup by design (backup:false or no
    # repo — the Mac); the tile says "monitored" instead of flagging it as unscheduled
    hosts = [{"name": h["name"], "repo": h.get("repo"), "arch": h["arch"], "os": h.get("os", "linux"),
              "used_gb": h.get("used_gb"),
              "monitored_only": h.get("backup", True) is False or not h.get("repo")}
             for h in CFG["hosts"] if h.get("enabled", True) is not False]
    facts = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        # 10-min cache: tiles take live numbers from Prometheus; SSH facts are
        # now only inventory (OS, disks, serials) + the fallback when it's down
        futs = {ex.submit(host_facts, h["name"], 600): h["name"] for h in hosts}
        for fu in futs:
            facts[futs[fu]] = fu.result()
    snaps = snapshot_summary()
    audited = _audited_snapshot_ids()
    for d in snaps.get("hosts", {}).values():
        for r in d.get("recent", []):
            r["recorded"] = r["id"] in audited
    try:
        sched = schedule_summary(snaps)
    except Exception as e:
        sched = {"error": str(e)[:200], "rows": []}
    return {"generated": _utcnow(), "ts": time.time(),
            "server": S["hostname"], "hosts": hosts, "facts": facts,
            "snapshots": snaps, "schedule": sched, "network": CFG.get("network") or {},
            "versions": _versions(),
            "auth_required": auth_on()}


# Favicon + brand mark: the watching eye (assets/favicon.svg, the source of truth), inlined so the
# page stays one file.
FAVICON = 'data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%20%3Cdefs%3E%20%3ClinearGradient%20id%3D%22tile%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%220%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23151b3d%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%2305060d%22%2F%3E%20%3C%2FlinearGradient%3E%20%3ClinearGradient%20id%3D%22neon%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%233ff0ff%22%2F%3E%3Cstop%20offset%3D%22.55%22%20stop-color%3D%22%235b8cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b45cff%22%2F%3E%20%3C%2FlinearGradient%3E%20%3CradialGradient%20id%3D%22iris%22%20cx%3D%22.5%22%20cy%3D%22.42%22%20r%3D%22.62%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23f0feff%22%2F%3E%3Cstop%20offset%3D%22.3%22%20stop-color%3D%22%235ff3ff%22%2F%3E%3Cstop%20offset%3D%22.78%22%20stop-color%3D%22%235b6cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%237a3cff%22%2F%3E%20%3C%2FradialGradient%3E%20%3Cfilter%20id%3D%22glow%22%20x%3D%22-30%25%22%20y%3D%22-30%25%22%20width%3D%22160%25%22%20height%3D%22160%25%22%3E%20%3CfeGaussianBlur%20stdDeviation%3D%222%22%20result%3D%22b%22%2F%3E%3CfeMerge%3E%3CfeMergeNode%20in%3D%22b%22%2F%3E%3CfeMergeNode%20in%3D%22SourceGraphic%22%2F%3E%3C%2FfeMerge%3E%20%3C%2Ffilter%3E%20%3C%2Fdefs%3E%20%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2215%22%20fill%3D%22url%28%23tile%29%22%2F%3E%20%3Crect%20x%3D%22.75%22%20y%3D%22.75%22%20width%3D%2262.5%22%20height%3D%2262.5%22%20rx%3D%2214.25%22%20fill%3D%22none%22%20stroke%3D%22%233ff0ff%22%20stroke-opacity%3D%22.25%22%20stroke-width%3D%221.5%22%2F%3E%20%3Cg%20fill%3D%22%23d6ecff%22%3E%3Ccircle%20cx%3D%2212%22%20cy%3D%2212.5%22%20r%3D%221.1%22%2F%3E%3Ccircle%20cx%3D%2251.5%22%20cy%3D%2210%22%20r%3D%22.8%22%2F%3E%3Ccircle%20cx%3D%2254%22%20cy%3D%2252.5%22%20r%3D%221%22%2F%3E%3Ccircle%20cx%3D%229.5%22%20cy%3D%2250%22%20r%3D%22.7%22%2F%3E%3C%2Fg%3E%20%3Cg%20filter%3D%22url%28%23glow%29%22%3E%20%3Cpath%20d%3D%22M6.5%2032%20Q32%207%2057.5%2032%20Q32%2057%206.5%2032%20Z%22%20fill%3D%22none%22%20stroke%3D%22url%28%23neon%29%22%20stroke-width%3D%223.8%22%20stroke-linejoin%3D%22round%22%2F%3E%20%3Ccircle%20cx%3D%2232%22%20cy%3D%2232%22%20r%3D%2210.8%22%20fill%3D%22url%28%23iris%29%22%2F%3E%20%3C%2Fg%3E%20%3Cpath%20d%3D%22M32%2021.8%20Q35.6%2032%2032%2042.2%20Q28.4%2032%2032%2021.8%20Z%22%20fill%3D%22%2305060d%22%2F%3E%20%3Ccircle%20cx%3D%2228.2%22%20cy%3D%2227.9%22%20r%3D%222.1%22%20fill%3D%22%23fff%22%20fill-opacity%3D%22.92%22%2F%3E%20%3C%2Fsvg%3E'
LOGIN_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Noctyx · sign in</title><link rel=icon href="/assets/favicon.svg">__THEME_JS__<style>
:root{--bg:#070913;--bg-1:#0d1020;--bg-0:#05060d;--line:#242a47;--fg:#e2e8f6;--fg-dim:#97a3c2;--fg-faint:#67718f;--accent:#3fe6ff;--accent-2:#c3a6ff;--accent-ink:#0a0d14;--bad:#ef5a5a;--glow:#0b1124;
 --sans:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;--mono:ui-monospace,Menlo,Consolas,monospace}
html,body{height:100%;margin:0}
body{display:grid;place-items:center;background:radial-gradient(900px 500px at 70% -10%,var(--glow),transparent 60%),var(--bg);color:var(--fg);font:14px var(--sans)}
.box{width:min(360px,92vw);background:var(--bg-1);border:1px solid var(--line);border-radius:14px;padding:26px 26px 22px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:18px} .brand img{width:34px;height:34px}
.brand b{font:800 20px var(--sans);letter-spacing:.14em;background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;background-clip:text;color:transparent}
label{display:block;font:600 12px var(--sans);color:var(--fg-dim);margin:12px 0 5px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;background:var(--bg-0);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:9px 10px;font:14px var(--mono)}
input:focus{outline:none;border-color:var(--accent)}
.rem{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12.5px;color:var(--fg-dim)}
button{margin-top:18px;width:100%;background:var(--accent);color:var(--accent-ink);border:0;border-radius:8px;padding:10px;font:700 13px var(--sans);letter-spacing:.06em;text-transform:uppercase;cursor:pointer}
#msg{min-height:18px;margin-top:10px;font-size:12.5px;color:var(--bad)} .foot{margin-top:6px;font-size:11.5px;color:var(--fg-faint)}
</style></head><body>
<form class=box id=lf><div class=brand><img src="/assets/favicon.svg" alt=""><b>NOCTYX</b></div>
 <label for=u>Name</label><input id=u type=text autocomplete=username autofocus required>
 <label for=pw>Password</label><input id=pw type=password autocomplete=current-password required>
 <label class=rem><input id=rem type=checkbox> keep me signed in for 30 days</label>
 <button type=submit>Sign in</button><div id=msg></div><div class=foot id=foot></div></form>
<script>
const next=new URLSearchParams(location.search).get('next')||'/';
document.getElementById('lf').addEventListener('submit',go);
fetch('/api/v1/me').then(r=>r.json()).then(m=>{if(m.mode=='open')document.getElementById('foot').innerHTML='This console is in open access: signing in is optional (per-user settings, your name in the audit). <a href="/" style="color:var(--accent)">Continue without</a>'});
async function go(e){e.preventDefault();const m=document.getElementById('msg');m.textContent='';
 const r=await fetch('/api/v1/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:document.getElementById('u').value,password:document.getElementById('pw').value,remember:document.getElementById('rem').checked})});
 const j=await r.json().catch(()=>({}));
 if(r.ok)location.href=next.startsWith('/')&&!next.startsWith('//')?next:'/';else{m.textContent=j.error||('sign-in failed ('+r.status+')');document.getElementById('pw').select()}}
</script></body></html>"""


def login_page():
    """The sign-in page, drawn with the console's own theme script (the viewer's colours)."""
    theme = PAGE[PAGE.index("<script>"):PAGE.index("</script>") + len("</script>")]
    return LOGIN_PAGE.replace("__THEME_JS__", theme).encode()


def page(view=None):
    """The one page, filled in for this site: brand mark, build stamp, the NAS's name, node paths."""
    out = (PAGE.replace("__FAVICON__", FAVICON).replace("__BUILD__", UI_BUILD)
           .replace("__NAS__", CM.nas_name(CM.cfg())).replace("__NODE_LIB__", P["node_lib"])
           .replace("__JOB_LOGS__", P["job_logs"]))
    if view:
        out = out.replace("<body>", f'<body data-view="{view}">', 1)
    return out.encode()


UI_BUILD = str(int(os.path.getmtime(__file__)))   # changes whenever this file is edited and the UI restarted
PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<script>
/* ── appearance: themes, modes, custom colours ────────────────────────────────
   The page draws only from the CSS custom properties below (:root), so a theme is a set of values
   for them, written onto <html>. Each preset has a dark and a light face; the mode (dark, light,
   system) picks one. Custom is nine colours; every other shade is mixed from them. Settings are
   this browser's (localStorage), over the site's defaults (site config "console", cached here so
   the first paint is already right). Runs in <head>: no flash of the wrong colours. */
const NXT=(()=>{
 const rgb=h=>{const m=/^#([0-9a-f]{6})$/i.exec(String(h||'').trim());if(!m)return null;const n=parseInt(m[1],16);return[n>>16&255,n>>8&255,n&255]};
 const hex=c=>'#'+c.map(v=>Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join('');
 const mix=(a,b,t)=>{const x=rgb(a),y=rgb(b);return x&&y?hex(x.map((v,i)=>v+(y[i]-v)*t)):a};
 const lum=h=>{const c=rgb(h);if(!c)return 0;const l=v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4};return .2126*l(c[0])+.7152*l(c[1])+.0722*l(c[2])};
 const BASE=['bg','panel','text','muted','accent','line','ok','warn','bad'];
 const PHL={bg:'#e9eef1',panel:'#f8fafb',bg2:'#f1f5f7',bg3:'#e3eaee',bg0:'#dde5ea',line:'#c3d0d8',lineSoft:'#d5dfe5',text:'#0c1a22',muted:'#4a6170',faint:'#7d919e',ok:'#0f9d58',warn:'#c77700',bad:'#d1293d',info:'#1f5fe0'};
 const PH=(label,blurb,d)=>({label,blurb,style:'phosphor',dark:d,light:{...PHL,accent:mix(d.accent,'#000000',.4),accentDim:mix(d.accent,'#000000',.55),accent2:mix(d.accent2,'#000000',.4)}});
 const KK=['cpu','gpu','temp','mem','net','disk','power','llm','ok','alert'];
 const PRESETS={
  noctyx:{label:'Noctyx',blurb:'Night console — navy and neon cyan',
   dark:{bg:'#070913',panel:'#0d1020',bg2:'#121629',bg3:'#191e36',bg0:'#05060d',line:'#242a47',lineSoft:'#1a1f38',text:'#e2e8f6',muted:'#a9c0cc',faint:'#9db4c2',accent:'#3fe6ff',accentDim:'#1f93b3',accent2:'#c3a6ff',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a',info:'#58a6ff'},
   light:{bg:'#eef1f8',panel:'#ffffff',bg2:'#f5f7fc',bg3:'#e7ebf5',bg0:'#e3e8f2',line:'#cfd6e6',lineSoft:'#e0e5f0',text:'#121729',muted:'#525d78',faint:'#8790a6',accent:'#0a9cc0',accentDim:'#077a96',accent2:'#8a3fe0',ok:'#1b9460',warn:'#a87a08',bad:'#d13c3c',info:'#2f6fd6'}},
  phosphor:{label:'Phosphor',blurb:'Instrument panel — LED meters, scope traces, LCD numerals',style:'phosphor',
   dark:{k:{cpu:'#3dff8f',gpu:'#3b8cff',temp:'#ffb020',mem:'#c3a6ff',net:'#22d3ee',disk:'#2ee6a6',power:'#ffd60a',llm:'#6ff5d2',ok:'#3dff8f',alert:'#ff4d5e'},bg:'#04070a',panel:'#0a1015',bg2:'#0d151b',bg3:'#132029',bg0:'#020406',line:'#1a2a35',lineSoft:'#12202a',text:'#e4f1f7',muted:'#a9c0cc',faint:'#9db4c2',accent:'#1fd1ff',accentDim:'#118aab',accent2:'#3dff8f',ok:'#3dff8f',warn:'#ffb020',bad:'#ff4d5e',info:'#3b8cff'},
   light:{...PHL,accent:'#0a8fc0',accentDim:'#086f95',accent2:'#0f9d58'}},
  p1green:PH('Phosphor · P1 Green','Green-phosphor CRT: lime, mint and teal on black',
   {bg:'#030806',panel:'#07100c',bg2:'#0a1510',bg3:'#0f1f17',bg0:'#010402',line:'#173525',lineSoft:'#0f2419',text:'#ddf5e5',muted:'#a5d2b2',faint:'#93c4a2',accent:'#33ff77',accentDim:'#1a9944',accent2:'#b6ff4d',ok:'#33ff77',warn:'#e8ff4d',bad:'#ff5c5c',info:'#4dffc3',
    k:{cpu:'#33ff77',gpu:'#4dffc3',temp:'#e8ff4d',mem:'#9dff4d',net:'#22e6a8',disk:'#7dffa8',power:'#d4ff33',llm:'#00ffb3',ok:'#33ff77',alert:'#ff5c5c'}}),
  p3amber:PH('Phosphor · P3 Amber','Amber terminal: gold, orange and honey',
   {bg:'#090704',panel:'#110d08',bg2:'#16110a',bg3:'#1f180e',bg0:'#040302',line:'#3a2b17',lineSoft:'#2a1f11',text:'#f6ead8',muted:'#dcc0a0',faint:'#c4a885',accent:'#ffb000',accentDim:'#a87100',accent2:'#ffd27a',ok:'#ffc94d',warn:'#ff8a1f',bad:'#ff4a3d',info:'#ffd27a',
    k:{cpu:'#ffb000',gpu:'#ffd27a',temp:'#ff8a1f',mem:'#ff9e5e',net:'#ffe08a',disk:'#ffc94d',power:'#fff07a',llm:'#ff7a3d',ok:'#ffc94d',alert:'#ff4a3d'}}),
  nixie:PH('Phosphor · Nixie','Neon tubes behind smoked glass, green indicator lamps',
   {bg:'#080707',panel:'#100e0d',bg2:'#151210',bg3:'#1d1815',bg0:'#040303',line:'#33261f',lineSoft:'#251c17',text:'#f2e8e1',muted:'#d4b8a8',faint:'#bfa090',accent:'#ff6a1a',accentDim:'#b2440a',accent2:'#ffa15c',ok:'#6fe36f',warn:'#ffb347',bad:'#ff3b30',info:'#ff9a5c',
    k:{cpu:'#ff6a1a',gpu:'#ff9a3d',temp:'#ffc04d',mem:'#ff4f6e',net:'#ffb37a',disk:'#ff8c42',power:'#ffd166',llm:'#ff5a36',ok:'#6fe36f',alert:'#ff3b30'}}),
  synthwave:PH('Phosphor · Synthwave','Magenta, violet and cyan on midnight purple',
   {bg:'#0a0812',panel:'#110e1d',bg2:'#161226',bg3:'#1e1832',bg0:'#05040a',line:'#30264c',lineSoft:'#221b38',text:'#efe9fb',muted:'#c6badf',faint:'#ab9fd0',accent:'#ff2fd0',accentDim:'#a3188a',accent2:'#2de2ff',ok:'#2dffb3',warn:'#ffcc33',bad:'#ff3d6e',info:'#2de2ff',
    k:{cpu:'#2dffb3',gpu:'#2de2ff',temp:'#ffcc33',mem:'#a96bff',net:'#5ab0ff',disk:'#39f5d0',power:'#ffe14d',llm:'#ff2fd0',ok:'#2dffb3',alert:'#ff3d6e'}}),
  arctic:PH('Phosphor · Arctic','Ice blue, glacier cyan and frost on cold navy',
   {bg:'#050a12',panel:'#0a1220',bg2:'#0e1828',bg3:'#132134',bg0:'#03060b',line:'#1c2e47',lineSoft:'#142338',text:'#e8f4ff',muted:'#a9c4e0',faint:'#96b2d0',accent:'#7fdcff',accentDim:'#3a95c2',accent2:'#d6f3ff',ok:'#6ff5d2',warn:'#ffd36b',bad:'#ff6b8a',info:'#8fb8ff',
    k:{cpu:'#6ff5d2',gpu:'#8fb8ff',temp:'#ffd36b',mem:'#c3a6ff',net:'#7fdcff',disk:'#a8f0ff',power:'#fff2a8',llm:'#d6f3ff',ok:'#6ff5d2',alert:'#ff6b8a'}}),
  redops:PH('Phosphor · Red Ops','All red, for a dark room: keeps your night vision',
   {bg:'#060202',panel:'#0c0404',bg2:'#110606',bg3:'#190909',bg0:'#030101',line:'#341010',lineSoft:'#240b0b',text:'#f0b8b2',muted:'#d69a96',faint:'#c0807c',accent:'#ff2a2a',accentDim:'#a31616',accent2:'#ff6b6b',ok:'#d94a4a',warn:'#ff8a5c',bad:'#ffd0d0',info:'#ff5c5c',
    k:{cpu:'#ff3b3b',gpu:'#ff6b5c',temp:'#ff8a5c',mem:'#e04870',net:'#ff5c7a',disk:'#ff4d4d',power:'#ff9466',llm:'#ff2a55',ok:'#d94a4a',alert:'#ffd0d0'}}),
  aurora:PH('Phosphor · Aurora','Northern lights: teal, violet and rose over a polar night',
   {bg:'#04090d',panel:'#09131a',bg2:'#0c1820',bg3:'#11202b',bg0:'#020507',line:'#1a3040',lineSoft:'#12232f',text:'#e6f4f2',muted:'#a9d0d4',faint:'#93bcc0',accent:'#5cffc8',accentDim:'#2a9e7c',accent2:'#b18cff',ok:'#5cffc8',warn:'#ffcf6b',bad:'#ff6b81',info:'#6aa8ff',
    k:{cpu:'#5cffc8',gpu:'#6aa8ff',temp:'#ffcf6b',mem:'#b18cff',net:'#4de0ff',disk:'#7af0b0',power:'#ffe58a',llm:'#ff7ad9',ok:'#5cffc8',alert:'#ff6b81'}}),
  outrun:PH('Phosphor · Outrun','Sunset over a neon grid: orange, hot pink and teal',
   {bg:'#0a0718',panel:'#110d24',bg2:'#16112e',bg3:'#1e173c',bg0:'#05030c',line:'#33275a',lineSoft:'#241c44',text:'#f5ecf7',muted:'#d4c3ea',faint:'#b8a9d0',accent:'#ff8a3d',accentDim:'#b0571f',accent2:'#ff3dac',ok:'#2ef2d8',warn:'#ffd23f',bad:'#ff4f5e',info:'#5b8cff',
    k:{cpu:'#2ef2d8',gpu:'#5b8cff',temp:'#ffb347',mem:'#c77dff',net:'#53d8ff',disk:'#3ee6a0',power:'#ffd23f',llm:'#ff3dac',ok:'#2ef2d8',alert:'#ff4f5e'}}),
  lcd:{label:'Phosphor · LCD',blurb:'Reflective calculator LCD by day; blue backlight by night',style:'phosphor',glow:{light:0,dark:.6},
   light:{bg:'#b8c3a0',panel:'#c8d2b1',bg2:'#c2cdab',bg3:'#b4bf98',bg0:'#aab68c',line:'#94a07a',lineSoft:'#a5b08b',text:'#1b2416',muted:'#46533b',faint:'#6a7660',accent:'#1f2e18',accentDim:'#3c4d33',accent2:'#2b3d22',ok:'#1f5a1f',warn:'#7a5200',bad:'#8a1a1a',info:'#1b2f4a',
    k:{cpu:'#1f4a1a',gpu:'#1b2f5a',temp:'#7a4a10',mem:'#432460',net:'#124848',disk:'#2a4a1a',power:'#5a4800',llm:'#5c1a44',ok:'#1f5a1f',alert:'#8a1a1a'}},
   dark:{bg:'#061433',panel:'#0a1c45',bg2:'#0d2252',bg3:'#122b63',bg0:'#040e26',line:'#1e3b7a',lineSoft:'#173068',text:'#eaf2ff',muted:'#a9bde6',faint:'#7189bf',accent:'#ffffff',accentDim:'#b7c9f0',accent2:'#cfe0ff',ok:'#b9ffd8',warn:'#ffe7a3',bad:'#ffb3c1',info:'#cfe0ff',
    k:{cpu:'#b9ffd8',gpu:'#cfe0ff',temp:'#ffe7a3',mem:'#e2cfff',net:'#bff4ff',disk:'#d2ffe8',power:'#fff3c4',llm:'#ffd0f0',ok:'#b9ffd8',alert:'#ffb3c1'}}},
  ember:{label:'Ember',blurb:'Charcoal and amber',
   dark:{bg:'#0b0d10',panel:'#12151a',bg2:'#171b21',bg3:'#1e232b',bg0:'#0c0e11',line:'#262c36',lineSoft:'#1d222a',text:'#dfe5ee',muted:'#96a0b0',faint:'#6a7484',accent:'#f7931a',accentDim:'#b06c10',accent2:'#ffb85c',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a',info:'#58a6ff'},
   light:{bg:'#f3f1ec',panel:'#ffffff',bg2:'#f8f6f2',bg3:'#eeeae3',bg0:'#ebe7e0',line:'#d8d2c8',lineSoft:'#e6e1d8',text:'#1c1f26',muted:'#5b6470',faint:'#8a929e',accent:'#d97706',accentDim:'#b8690a',accent2:'#a8640d',ok:'#1f9d63',warn:'#c48a08',bad:'#d63f3f',info:'#2f6fd6'}},
  mono:{label:'Mono',blurb:'Grayscale — quiet and focused',
   dark:{bg:'#0f0f0f',panel:'#161616',bg2:'#1b1b1b',bg3:'#232323',line:'#2c2c2c',text:'#e8e8e8',muted:'#9a9a9a',faint:'#6b6b6b',accent:'#f2f2f2',accentDim:'#9a9a9a',ok:'#9ccfae',warn:'#d9c48a',bad:'#d99090',info:'#a9b9c9'},
   light:{bg:'#f4f4f4',panel:'#ffffff',bg2:'#f8f8f8',bg3:'#ececec',line:'#d5d5d5',text:'#151515',muted:'#5a5a5a',faint:'#8c8c8c',accent:'#111111',accentDim:'#555555',ok:'#2f8f5b',warn:'#a37a12',bad:'#c04747',info:'#3a5f8a'}},
  github:{label:'GitHub',blurb:'GitHub Dark and Light Default',
   dark:{bg:'#0d1117',panel:'#161b22',bg2:'#1c2128',bg3:'#21262d',bg0:'#010409',line:'#30363d',lineSoft:'#262c34',text:'#e6edf3',muted:'#8b949e',faint:'#6e7681',accent:'#58a6ff',accentDim:'#1f6feb',accent2:'#a371f7',ok:'#3fb950',warn:'#d29922',bad:'#f85149',info:'#79c0ff'},
   light:{bg:'#f6f8fa',panel:'#ffffff',bg2:'#f6f8fa',bg3:'#eaeef2',bg0:'#eff2f5',line:'#d0d7de',lineSoft:'#e1e6eb',text:'#1f2328',muted:'#656d76',faint:'#8c959f',accent:'#0969da',accentDim:'#0a4fb5',accent2:'#8250df',ok:'#1a7f37',warn:'#9a6700',bad:'#cf222e',info:'#0969da'}},
  catppuccin:{label:'Catppuccin',blurb:'Soft pastels — Mocha and Latte',
   dark:{bg:'#181825',panel:'#1e1e2e',bg2:'#242438',bg3:'#313244',bg0:'#11111b',line:'#45475a',lineSoft:'#313244',text:'#cdd6f4',muted:'#a6adc8',faint:'#6c7086',accent:'#89dceb',accentDim:'#5fa8b6',accent2:'#cba6f7',ok:'#a6e3a1',warn:'#f9e2af',bad:'#f38ba8',info:'#89b4fa'},
   light:{bg:'#e6e9ef',panel:'#eff1f5',bg2:'#e9ecf2',bg3:'#dce0e8',bg0:'#dce0e8',line:'#bcc0cc',lineSoft:'#ccd0da',text:'#4c4f69',muted:'#6c6f85',faint:'#9ca0b0',accent:'#209fb5',accentDim:'#1a7f91',accent2:'#8839ef',ok:'#40a02b',warn:'#df8e1d',bad:'#d20f39',info:'#1e66f5'}},
 };
 /* Phosphor effects: all static layers or compositor-only animation (transform, opacity) */
 const FXDEF={glow:'normal',scan:'kiosk',vignette:'on',grain:'off',beam:'off',flicker:'off',fringe:'off'};
 const GLOW={off:0,soft:.5,normal:1,bright:1.6};
 const DEF={mode:'dark',theme:'noctyx',tunit:'F',fx:FXDEF,kiosk:{alerts:0,backups:0,rotation:false,fit:'shrink',dense:false,cols:0},custom:{bg:'#0b0e1a',panel:'#121629',text:'#e2e8f6',muted:'#97a3c2',accent:'#3fe6ff',line:'#242a47',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a'}};
 function derive(b){const f={...b},dark=lum(f.bg)<.4;f.scheme=dark?'dark':'light';
  f.bg0??=mix(f.bg,dark?'#000000':'#ffffff',.25);f.bg2??=mix(f.panel,f.text,dark?.04:.03);f.bg3??=mix(f.panel,f.text,dark?.09:.07);
  f.lineSoft??=mix(f.line,f.bg,.4);f.faint??=mix(f.muted,f.bg,.3);f.accentDim??=mix(f.accent,f.bg,.35);f.accent2??=f.accent;
  f.accentInk??=lum(f.accent)>.45?'#0a0d14':'#ffffff';f.info??=dark?'#58a6ff':'#2f6fd6';
  f.scrim??=dark?'rgba(2,4,10,.62)':'rgba(20,24,34,.38)';f.glow??=mix(f.bg,f.accent,dark?.07:.05);f.dots??=mix(f.bg,f.text,dark?.07:.08);
  return f}
 const LS='nx_settings_v1',LSS='nx_site_settings_v1';
 const read=k=>{try{return JSON.parse(localStorage.getItem(k)||'null')}catch(e){return null}};
 const local=()=>read(LS)||{},site=()=>read(LSS)||{};
 const eff=()=>{const s=site(),l=local();return{...DEF,...s,...l,custom:{...DEF.custom,...(s.custom||{}),...(l.custom||{})},fx:{...FXDEF,...(s.fx||{}),...(l.fx||{})},kiosk:{...DEF.kiosk,...(s.kiosk||{}),...(l.kiosk||{})}}};
 const light=()=>{try{return matchMedia('(prefers-color-scheme: light)').matches}catch(e){return false}};
 function face(s){s=s||eff();if(s.theme=='custom')return{id:'custom',...derive(s.custom)};
  const p=PRESETS[s.theme]||PRESETS.noctyx,sc=s.mode=='system'?(light()?'light':'dark'):s.mode=='light'?'light':'dark';
  const k=p.style?(p[sc].k||Object.fromEntries(Object.entries(p.dark.k).map(([a,b])=>[a,mix(b,'#000000',.4)]))):null;
  return{id:PRESETS[s.theme]?s.theme:'noctyx',style:p.style||'',k,glowScale:(p.glow||{})[sc]??(sc=='light'?.35:1),...derive(p[sc])}}
 const VARS={bg:'--bg',panel:'--bg-1',bg2:'--bg-2',bg3:'--bg-3',bg0:'--bg-0',line:'--line',lineSoft:'--line-soft',text:'--fg',muted:'--fg-dim',faint:'--fg-faint',
  accent:'--accent',accentDim:'--accent-dim',accent2:'--accent-2',accentInk:'--accent-ink',ok:'--ok',warn:'--warn',bad:'--bad',info:'--info',scrim:'--scrim',glow:'--glow',dots:'--dots'};
 function apply(s){const f=face(s),r=document.documentElement;
  for(const[k,v]of Object.entries(VARS))if(f[k])r.style.setProperty(v,f[k]);
  for(const k of KK){if(f.k&&f.k[k])r.style.setProperty('--k-'+k,f.k[k]);else r.style.removeProperty('--k-'+k)}
  const fx=(s||eff()).fx||FXDEF;
  for(const[k,v]of Object.entries(FXDEF))r.dataset['fx'+k[0].toUpperCase()+k.slice(1)]=fx[k]??v;
  if(f.style)r.style.setProperty('--gl',String((GLOW[fx.glow]??1)*f.glowScale));else r.style.removeProperty('--gl');
  r.style.setProperty('color-scheme',f.scheme);r.dataset.theme=f.scheme;r.dataset.palette=f.id;r.dataset.style=f.style||'';return f}
 function save(patch){const l={...local(),...patch};if(patch.custom)l.custom={...(local().custom||{}),...patch.custom};if(patch.fx)l.fx={...(local().fx||{}),...patch.fx};if(patch.kiosk)l.kiosk={...(local().kiosk||{}),...patch.kiosk};
  try{localStorage.setItem(LS,JSON.stringify(l))}catch(e){};apply();return l}
 function clearLocal(){try{localStorage.removeItem(LS)}catch(e){};apply()}
 function setSite(s){try{localStorage.setItem(LSS,JSON.stringify(s||{}))}catch(e){};apply()}
 apply();
 try{matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(eff().mode=='system')apply()})}catch(e){}
 return{PRESETS,BASE,DEF,FXDEF,derive,face,eff,local,site,apply,save,clearLocal,setSite,mix}
})();
</script>

<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<title>Noctyx</title><style>
/* Noctyx console — instrument-panel language:
   token palette, tabular monospace numbers (no jitter on refresh), uppercase
   micro-caps headings, card chrome, grabbable head strips. No webfonts, no CDN. */
:root{
  --bg:#070913; --bg-1:#0d1020; --bg-2:#121629; --bg-3:#191e36;
  --line:#242a47; --line-soft:#1a1f38;
  --fg:#e2e8f6; --fg-dim:#a9c0cc; --fg-faint:#9db4c2;
  --accent:#3fe6ff; --accent-dim:#1f93b3; --accent-2:#c3a6ff;
  --ok:#2ecc8f; --warn:#f0b429; --bad:#ef5a5a; --info:#58a6ff;
  --bg-0:#05060d; --radius:10px; --radius-sm:6px;
  --accent-ink:#0a0d14; --scrim:rgba(2,4,10,.62); --glow:#0b1124; --dots:#151a2a;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.28);
  color-scheme:dark;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;color:var(--fg);font:13px/1.5 var(--sans);-webkit-font-smoothing:antialiased;
  overflow:hidden;background:radial-gradient(1200px 600px at 80% -10%,var(--glow) 0%,transparent 60%),var(--bg)}
.app{display:grid;grid-template-rows:auto 1fr;height:100vh;grid-template-columns:minmax(0,1fr)}
header.top{display:flex;align-items:center;gap:14px;flex-wrap:nowrap;white-space:nowrap;
  min-width:0;overflow:visible;padding:0 14px;height:46px;position:relative;
  background:linear-gradient(180deg,var(--bg-2),var(--bg-1));border-bottom:1px solid var(--line);z-index:50}
.brand{display:flex;align-items:center;gap:9px;font-weight:650}
.mark{width:22px;height:22px;border-radius:6px;flex:0 0 auto;
  background:url("__FAVICON__") center/contain no-repeat;
  box-shadow:0 0 14px color-mix(in srgb,var(--accent) 35%,transparent)}
.wordmark{letter-spacing:.14em;font-weight:800;text-transform:uppercase;
  background:linear-gradient(90deg,var(--accent),color-mix(in srgb,var(--accent),var(--accent-2)) 55%,var(--accent-2));-webkit-background-clip:text;background-clip:text;color:transparent}
.wordmark b{font-weight:800}
.hstat{font:12px var(--mono);color:var(--fg-dim);font-variant-numeric:tabular-nums}
.hpill{font:600 10.5px var(--sans);letter-spacing:.06em;text-transform:uppercase;
  padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--fg-dim);flex:0 0 auto}
.hpill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.hspacer{flex:1}
/* nothing in the header is cut off: when it does not fit, fitHeader() drops the least useful items whole, one
   step at a time (f1 the clock, f2 the access-mode pill the user menu repeats, f3 "on schedule", f4 the server,
   f5 the page name); the alert and backup counts and the buttons always stay */
header.top .hstat,header.top .hpill{flex:0 0 auto}
header.top.f1 #hclock,header.top.f2 #hauth,header.top.f3 .hlong,header.top.f4 #hsrv,header.top.f5 #hpage{display:none}
header.top .brand{flex:0 0 auto}
@media (max-width:1250px){header.top{gap:9px} .hbtn{padding:5px 8px;letter-spacing:.03em} #hclock{display:none}}
@media (max-width:980px){header.top .hstat{display:none}}
.hbtn{background:var(--bg-3);border:1px solid var(--line);color:var(--fg-dim);border-radius:var(--radius-sm);
  padding:5px 11px;font:600 11px var(--sans);letter-spacing:.05em;cursor:pointer;text-transform:uppercase;flex:0 0 auto}
.hbtn:hover{border-color:var(--accent);color:var(--accent)}
a.brand{text-decoration:none;color:inherit;flex:0 0 auto}
.pages{display:flex;gap:2px;flex:0 0 auto}
.menu{position:relative}
.menu-title{background:none;border:1px solid transparent;color:var(--fg-dim);font:600 11px var(--sans);letter-spacing:.06em;text-transform:uppercase;padding:6px 10px;border-radius:var(--radius-sm);cursor:pointer}
.menu-title:hover,.menu.open .menu-title{color:var(--fg);background:var(--bg-3)}
.menu.cur .menu-title,a.menu-title.cur{color:var(--accent)}
a.menu-title{text-decoration:none;display:inline-flex;align-items:center;gap:6px} a.menu-title kbd{font:10px var(--mono);color:var(--fg-faint);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.menu-pop.hide{display:none}
.menu-pop{position:absolute;top:calc(100% + 6px);left:0;min-width:190px;background:var(--bg-2);border:1px solid var(--line);border-radius:var(--radius);padding:5px;box-shadow:0 10px 30px rgba(0,0,0,.35);z-index:80}
.menu-pop a{display:flex;align-items:center;gap:12px;padding:7px 10px;border-radius:var(--radius-sm);color:var(--fg);text-decoration:none;font:12.5px var(--sans)}
.menu-pop a:hover,.menu-pop a.cur{background:var(--bg-3)} .menu-pop a.cur{color:var(--accent)}
.menu-pop kbd{margin-left:auto;font:10.5px var(--mono);color:var(--fg-faint);border:1px solid var(--line);border-radius:4px;padding:0 5px}
.pagename{font:600 12px var(--sans);color:var(--fg);padding-left:10px;border-left:1px solid var(--line);flex:0 0 auto}
.hbtn.gear{display:inline-flex;align-items:center;padding:5px 8px}
.huser{display:inline-flex;align-items:center;gap:8px;font:12px var(--sans);color:var(--fg-dim);flex:0 0 auto}
.huser b{color:var(--fg)} .huser .role{font:10.5px var(--mono);border:1px solid var(--line);border-radius:999px;padding:1px 7px}
.compbtns{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:5px 0 2px;white-space:normal}
.setwrap{position:fixed;inset:0;z-index:200} .setwrap.hide{display:none}
.setscrim{position:absolute;inset:0;background:var(--scrim)}
.setpanel{position:absolute;top:0;right:0;bottom:0;width:min(520px,100vw);overflow:auto;background:var(--bg-1);border-left:1px solid var(--line);padding:18px 20px 40px;box-shadow:-12px 0 40px rgba(0,0,0,.35)}
.setpanel h2{display:flex;align-items:center;gap:8px;margin:0 0 4px;font:700 15px var(--sans)} .setpanel h2 .sp{flex:1}
/* Ask Noctyx: a drawer beside the dashboard (not modal: the panels stay live while it looks things up) */
.askwrap{position:fixed;top:0;right:0;bottom:0;width:min(470px,100vw);z-index:190;display:flex;flex-direction:column;background:var(--bg-1);border-left:1px solid var(--line);box-shadow:-12px 0 40px rgba(0,0,0,.35)}
.askwrap.hide{display:none} body.kiosk .askwrap,body.kiosk #hask{display:none}
.askhead{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line-soft)}
.askhead h2{margin:0;font:700 14px var(--sans);flex:1} .askhead .model{font:10.5px var(--mono);color:var(--fg-faint);max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.asklog{flex:1;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px;font:13px/1.5 var(--sans)}
.askmsg{white-space:pre-wrap;word-wrap:break-word} .askmsg.user{align-self:flex-end;max-width:85%;background:var(--bg-3);border:1px solid var(--line);border-radius:10px 10px 2px 10px;padding:7px 11px}
.askmsg.bot{color:var(--fg)} .askmsg code{font:12px var(--mono);background:var(--bg-0);border:1px solid var(--line-soft);border-radius:4px;padding:0 4px}
.askmsg pre{font:12px/1.45 var(--mono);background:var(--bg-0);border:1px solid var(--line-soft);border-radius:var(--radius-sm);padding:8px 10px;overflow:auto;white-space:pre;margin:4px 0}
.askmsg .by{display:block;font:10px var(--mono);color:var(--fg-faint);margin-top:3px}
.askmsg.err{color:var(--bad);font-size:12px}
.asktool{font:11.5px var(--mono);color:var(--fg-dim)} .asktool summary{cursor:pointer;list-style:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .asktool summary::before{content:'▸ ';color:var(--fg-faint)}
.asktool[open] summary::before{content:'▾ '} .asktool.bad summary{color:var(--warn)}
.asktool pre{max-height:260px;overflow:auto;font:11px/1.4 var(--mono);background:var(--bg-0);border:1px solid var(--line-soft);border-radius:var(--radius-sm);padding:6px 8px;margin:4px 0 0;white-space:pre-wrap;word-break:break-all}
.askbusy{font:12px var(--sans);color:var(--fg-dim)} .askbusy::after{content:'…';animation:askdots 1.2s steps(4) infinite}
@keyframes askdots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'…'}}
.askhint{display:flex;flex-wrap:wrap;gap:6px} .askhint button{background:var(--bg-2);border:1px solid var(--line);color:var(--fg-dim);border-radius:999px;padding:4px 10px;font:12px var(--sans);cursor:pointer}
.askhint button:hover{border-color:var(--accent);color:var(--accent)}
.askform{display:flex;gap:8px;padding:10px 14px 14px;border-top:1px solid var(--line-soft)}
.askform textarea{flex:1;resize:none;min-height:38px;max-height:140px;background:var(--bg-0);color:var(--fg);border:1px solid var(--line);border-radius:var(--radius-sm);padding:8px 10px;font:13px var(--sans)}
.askform textarea:focus{outline:none;border-color:var(--accent)}
.asbuiltin{max-height:320px;overflow:auto;white-space:pre-wrap;font:11px/1.45 var(--mono);color:var(--fg-dim)}
.askprompt pre{white-space:pre-wrap;word-break:normal;overflow-wrap:anywhere;max-height:50vh} .askprompt summary{white-space:normal}
.asknote{padding:0 14px 10px;font:11px var(--sans);color:var(--fg-faint)}
.setnote{margin:0 0 14px;font:12px var(--sans);color:var(--fg-dim)}
.setstore{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font:12px var(--sans);padding:9px 11px;background:var(--bg-0);border:1px solid var(--line-soft);border-radius:var(--radius-sm);margin-bottom:6px}
.setgrp{border-top:1px solid var(--line-soft);padding:14px 0 6px}
.setgrp h3{margin:0 0 4px;font:700 11px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-dim)}
.setgrp .gnote{margin:0 0 10px;font:11.5px var(--sans);color:var(--fg-faint)}
.setrow{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;align-items:start;padding:8px 0}
.setrow>label{font:600 12px var(--sans);padding-top:6px} .setrow .hint{grid-column:2;font:11px var(--sans);color:var(--fg-faint)}
.setrow.dim{opacity:.45}
.setrow>.seg{justify-self:start}
/* About */
.about{display:flex;flex-direction:column;gap:14px}
.abanner{width:100%;max-width:1280px;border-radius:var(--radius);border:1px solid var(--line-soft);display:block;margin:0 auto}
.acols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.acard{background:var(--bg-2);border:1px solid var(--line-soft);border-radius:var(--radius);padding:14px 16px;font:12.5px/1.55 var(--sans)}
.acard h3{margin:0 0 8px;font:700 11px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-dim)}
.acard p{margin:0 0 8px}
.arow{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid var(--line-soft);font:12px var(--mono)} .arow span{color:var(--fg-dim)}
.adonate{display:flex;gap:14px;align-items:center} .adonate img{border-radius:8px;background:#fff;padding:4px}
.addr{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);font:11.5px var(--mono);padding:6px 9px;border-radius:999px;cursor:pointer;word-break:break-all;text-align:left}
.addr:hover{border-color:var(--accent)}
.apills{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.apill{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden;font:11.5px var(--mono)}
.apill span{background:var(--bg-3);color:var(--fg-dim);padding:3px 9px} .apill b{background:color-mix(in srgb,var(--accent) 22%,var(--bg-1));color:var(--fg);padding:3px 9px}
.apill.warn b{background:color-mix(in srgb,var(--warn) 25%,var(--bg-1))}
.afeat{display:grid;grid-template-columns:72px 1fr;gap:10px;padding:6px 0;border-bottom:1px solid var(--line-soft)} .afeat b{color:var(--accent);font:700 12px var(--sans)} .afeat span{color:var(--fg-dim)}
.alinks{display:flex;flex-direction:column;gap:5px;margin-top:4px} .alinks a{color:var(--accent);text-decoration:none} .alinks a:hover{text-decoration:underline}
.akeys{font:12px var(--sans);color:var(--fg-dim);line-height:2} .akeys kbd{font:10.5px var(--mono);border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--fg);margin-right:2px}
/* Kiosk: no chrome, big type */
body.kiosk header.top{display:none} body.kiosk .panel{border:0;background:transparent;box-shadow:none} body.kiosk .panel>.grip{display:none}
/* One screen: the wrap is exactly the viewport (body overflow:hidden) and kioskFit() sizes the
   node grid to the height left under the header and the slots: rows = floor(leftover / card
   height), columns adapt to the node count, and the whole wrap scales down as a last resort. */
body.kiosk #desk{padding:0;overflow:hidden} body.kiosk .pbody{padding:0;overflow:hidden}
.kiosk-wrap{display:flex;flex-direction:column;gap:calc(18px*var(--kf-h,1));padding:calc(10px*var(--kf-h,1)) calc(8px*var(--kf-h,1));box-sizing:border-box;
  width:100vw;height:100vh;transform-origin:top left}
html.kiosk-1{--kf-h:.78} html.kiosk-2{--kf-h:.62}
/* the header's numbers scale with the bar's own width (cqw), capped at their full size: a
   narrow window gets smaller numbers and keeps room for the graphs (user, 2026-10-23) */
.ktop{display:flex;align-items:baseline;gap:calc(18px*var(--kf-h,1));flex-wrap:nowrap;overflow:hidden;width:100%;min-width:0;container-type:inline-size;
  --kbig-fs:min(calc(44px*var(--kf-h,1)),2.4cqw);--kclock-fs:min(calc(44px*var(--kf-h,1)),3.2cqw)}
.kbrand{align-self:center;display:flex;align-items:center;gap:calc(10px*var(--kf-h,1));text-decoration:none} .kbrand .mark{width:calc(34px*var(--kf-h,1));height:calc(34px*var(--kf-h,1))} .kbrand .wordmark{font-size:min(calc(22px*var(--kf-h,1)),1.9cqw)}
/* each header number sits centred over its label (user, 2026-10-23) */
.kbig{display:flex;flex-direction:column;align-items:center} .kbig>span{text-align:center}
.kbig b{font:800 var(--kbig-fs)/1 var(--mono)} .kbig span,.kclock span{font:calc(11px*var(--kf-h,1)) var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-dim)}
.kclock{display:flex;flex-direction:column;align-items:flex-end;padding-left:calc(20px*var(--kf-h,1));border-left:1px solid var(--line);margin-left:auto;flex:0 0 auto} .kclock b{font:700 var(--kclock-fs)/1 var(--mono);white-space:nowrap}
/* header graphs sit directly beside their own number (user, 2026-10-22), so each KPI reads as
   one unit: the graph with its number to the right and the label under the number. The
   pairs share the row's free width and the graphs take all of it (user, 2026-10-23: the graphs
   were slivers between big numbers and three-line labels). The clock and the vitals never
   shrink, or the clock's digits spill left over 'tok / 24h'. Every number in the bar sits on
   one baseline (user, 2026-10-23): the bar aligns on baselines, and the number is the only
   baseline-aligned item in its pair's first row, so the pair's baseline is the number's. Its
   line box is the graph's height, which keeps it centred beside the graph. */
.kpair{display:grid;grid-template-columns:minmax(0,1fr) minmax(calc(74px*var(--kf-h,1)),auto);column-gap:calc(12px*var(--kf-h,1));row-gap:calc(3px*var(--kf-h,1));
  align-items:center;flex:1 1 0;min-width:0;max-width:calc(380px*var(--kf-h,1))}
.kpair .kbig{display:contents}
/* the label sits under its number on two short lines (broken in the markup), like the vitals' captions */
.kpair .kbig>span{grid-area:2/2;align-self:start;margin:0;text-align:center;white-space:nowrap}
/* the number keeps a slot of about two and a half digits, so a graph barely moves when its number gains a digit */
.kpair .kbig>b{grid-area:1/2;align-self:baseline;line-height:calc(40px*var(--kf-h,1));min-width:2em;text-align:center}
.kvitals{display:flex;align-items:stretch;gap:calc(16px*var(--kf-h,1));flex:0 0 auto;padding-left:calc(16px*var(--kf-h,1));border-left:1px solid var(--line)}
.kvital{display:flex;flex-direction:column;align-items:flex-start;min-width:0;flex:0 0 auto}
.kvital b{font:800 calc(23px*var(--kf-h,1))/1 var(--mono);color:var(--fg)}
.kvital span{font:calc(9.5px*var(--kf-h,1)) var(--sans);letter-spacing:.09em;text-transform:uppercase;color:var(--fg-faint);white-space:nowrap}
/* the graph fills its whole cell, edge to edge and as tall as the number and its label together
   (user, 2026-10-23): a compact chart (no axes, stretched viewBox) inside a column flex */
.ktopg{grid-area:1/1/3/2;align-self:stretch;display:flex;min-width:0;overflow:hidden}
.ktopg .chart{margin:0;flex:1;display:flex;min-width:0}
.ktopg .ch-h{display:none}
.ktopg .chart svg,.ktopg .chart svg.cmp{flex:1;height:auto;min-height:calc(40px*var(--kf-h,1));width:100%;display:block;border-radius:4px}
.ktopg .chart svg path,.ktopg .chart svg line{vector-effect:non-scaling-stroke}
.ktopg .ch-empty{display:none}
.kslots{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:calc(12px*var(--kf-h,1))}
.kslot,.knode{background:var(--bg-1);border:1px solid var(--line-soft);border-radius:var(--radius);padding:calc(14px*var(--kf-h,1)) calc(16px*var(--kf-h,1));min-height:0;display:flex;flex-direction:column}
.kslot.down,.knode.down{border-color:color-mix(in srgb,var(--bad) 55%,var(--line))}
.kh{display:flex;align-items:center;gap:calc(10px*var(--kf-h,1));font:700 calc(18px*var(--kf-h,1)) var(--sans)} .kh .mut{font:500 calc(12px*var(--kf-h,1)) var(--sans)}
.km{font:600 calc(15px*var(--kf-h,1)) var(--mono);margin-top:calc(8px*var(--kf-h,1));color:var(--accent)}
.knodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));grid-auto-rows:minmax(0,auto);gap:calc(12px*var(--kf-h,1));flex:1;min-height:0}
/* kioskFit() rewrites grid-template-columns and grid-auto-rows on .knodes; html.kiosk-{1,2}
   shrink --kf-h, which every kiosk size above derives from */
.kgrid{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:calc(6px*var(--kf-h,1)) calc(10px*var(--kf-h,1));margin-top:calc(10px*var(--kf-h,1));font:calc(12px*var(--kf-h,1)) var(--sans);align-items:baseline}
.kgrid span{color:var(--fg-dim)} .kgrid b{font:700 calc(20px*var(--kf-h,1)) var(--mono)}
.kllm{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;margin-top:auto;padding-top:calc(8px*var(--kf-h,1));border-top:1px solid var(--line-soft);font:calc(12px*var(--kf-h,1)) var(--mono);gap:calc(8px*var(--kf-h,1))}
.kllm .mut:first-child{flex:1 0 100%;white-space:normal;overflow-wrap:break-word;line-height:1.3} .kllm .mut:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0} .kllm b{font:800 calc(22px*var(--kf-h,1)) var(--mono);color:var(--accent);white-space:nowrap;flex:0 0 auto} .kllm small{font:calc(11px*var(--kf-h,1)) var(--sans);color:var(--fg-dim)}
/* kiosk extras: the detail pages (alerts / backups) and fit:'scroll' */
.kpg{font:600 calc(11px*var(--kf-h,1)) var(--mono);color:var(--accent);margin-top:calc(4px*var(--kf-h,1))}
.kbig.dim{opacity:.45}
/* the hour's best rate rides under the live one: same colour family, half the caption's shout */
.kpeak{font:600 calc(10px*var(--kf-h,1)) var(--mono);color:var(--accent);margin-top:calc(2px*var(--kf-h,1));letter-spacing:.02em}
/* dense headers: five KPIs on one line, slightly smaller so they never wrap the clock away */
.kbig.kdense b{font-size:min(calc(34px*var(--kf-h,1)),2.4cqw)} .kbig.kdense span{font-size:calc(10px*var(--kf-h,1))}
/* the 30-min GPU/CPU trace that fills the empty middle of a node tile (accent=gpu, ok=cpu):
   stretched to fill its box (a short SVG at height:100% would sit at the top and leave a void),
   with a hairline grid at 50/100% and the two lines labelled at their own height — no legend
   needed on the wall. */
.kspark{width:100%;height:100%;display:block}
.knode .ktrace{flex:1 1 auto;min-height:calc(56px*var(--kf-h,1));margin-top:calc(10px*var(--kf-h,1));position:relative;overflow:hidden}
.knode .ktrace svg{position:absolute;inset:0}
.knode .ktrace i{position:absolute;right:calc(4px*var(--kf-h,1));font:600 calc(9.5px*var(--kf-h,1)) var(--sans);letter-spacing:.08em;line-height:1}
.knode .ktrace i.a{color:var(--accent)} .knode .ktrace i.g{color:var(--ok)} .knode .ktrace i.t{color:var(--accent-2)} .knode .ktrace i.p{color:var(--warn)}
.kspark path{fill:none;stroke-width:1.8;vector-effect:non-scaling-stroke;stroke-linejoin:round;opacity:.9}
.kspark path.a{stroke:var(--accent)} .kspark path.g{stroke:var(--ok)}
.kspark path.t{stroke:var(--accent-2);stroke-dasharray:3 3;opacity:.85}
/* prompt tok/s: dash-dot so it reads as input, not generation, on its own scale */
.kspark path.p{stroke:var(--warn);stroke-dasharray:5 2 1 2;opacity:.85}
.kspark line.grid{stroke:var(--line-soft);stroke-width:1;vector-effect:non-scaling-stroke}
.kload{margin-left:auto;font:500 calc(11px*var(--kf-h,1)) var(--sans);color:var(--fg-faint)}
/* the alerts page's empty state: one quiet line */
.kticker{display:flex;flex-wrap:wrap;gap:calc(8px*var(--kf-h,1));align-items:flex-start;flex:0 1 auto;min-height:0;
 padding:calc(7px*var(--kf-h,1)) calc(10px*var(--kf-h,1));border:1px solid var(--line-soft);border-radius:var(--radius);background:var(--bg-1);
 font:calc(13px*var(--kf-h,1)) var(--sans)}
.kticker.kempty{gap:8px}
.dot.warn{background:var(--warn);box-shadow:0 0 8px color-mix(in srgb,var(--warn) 70%,transparent)}
.dot.info{background:var(--info);box-shadow:0 0 8px color-mix(in srgb,var(--info) 60%,transparent)}
.kdetail{flex:0 1 auto;min-height:0;overflow:auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,calc(340px*var(--kf-h,1))),1fr));gap:calc(12px*var(--kf-h,1));align-content:start}
.kcard{background:var(--bg-1);border:1px solid var(--line-soft);border-radius:var(--radius);padding:calc(12px*var(--kf-h,1)) calc(14px*var(--kf-h,1));display:flex;flex-direction:column;gap:calc(6px*var(--kf-h,1))}
/* a detail card's header must survive a narrow column: the name truncates with an ellipsis,
   the since/host tags stay readable (the wall has no pointer, so nothing may hide behind a hover) */
.kcard .kh{min-width:0} .kcard .kh>b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:0 1 auto}
.kcard.critical{border-color:color-mix(in srgb,var(--bad) 55%,var(--line))} .kcard.warning{border-color:color-mix(in srgb,var(--warn) 55%,var(--line))}
.ksum{font:calc(13px*var(--kf-h,1))/1.45 var(--sans);color:var(--fg-dim)}
.ktri{font:calc(12px*var(--kf-h,1))/1.45 var(--sans);color:var(--fg-faint);border-top:1px solid var(--line-soft);padding-top:calc(6px*var(--kf-h,1));
 display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
/* fit:'scroll' (?fit=scroll or the site setting): natural rows, the wrap itself scrolls so
   nothing is ever cut off — the wall keeps scrolling inside #desk, body's overflow:hidden
   is what cut content off before (operator, 2026-10-22: scroll must never mean 'hidden') */
.kiosk-wrap.kfit-scroll{overflow:visible}
body.kiosk .kiosk-wrap.kfit-scroll{overflow-y:auto;overscroll-behavior:contain}
.kiosk-wrap.kfit-scroll .knodes{flex:0 0 auto;grid-auto-rows:minmax(0,auto)}
.kiosk-wrap.kfit-scroll .kdetail{flex:0 0 auto}
.kiosk-wrap.kfit-scroll .knode{min-height:calc(170px*var(--kf-h,1))}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--radius-sm);overflow:hidden;background:var(--bg-0)}
.seg button{background:none;border:0;border-right:1px solid var(--line);color:var(--fg-dim);font:600 11.5px var(--sans);padding:6px 12px;cursor:pointer}
.seg button{white-space:nowrap}
.hpill:empty{display:none}
.seg button:last-child{border-right:0} .seg button:hover{color:var(--fg);background:var(--bg-3)}
.seg button.on{background:var(--accent);color:var(--accent-ink)}
.tcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.tcard{text-align:left;background:var(--bg-2);border:1px solid var(--line);border-radius:var(--radius-sm);padding:8px 9px;cursor:pointer;color:var(--fg)}
.tcard:hover{border-color:var(--fg-faint)} .tcard.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.tcard b{font:700 12px var(--sans);display:block} .tcard small{display:block;font:10.5px var(--sans);color:var(--fg-faint);margin-top:2px;min-height:26px}
.swatch{display:flex;height:16px;border-radius:4px;overflow:hidden;margin-top:6px;border:1px solid var(--line-soft)} .swatch i{flex:1}
.cpick{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.cpick label{display:flex;align-items:center;gap:6px;font:11px var(--sans);color:var(--fg-dim)}
.cpick input[type=color]{width:30px;height:24px;padding:0;border:1px solid var(--line);border-radius:4px;background:none;cursor:pointer}

/* desktop: a snapping grid, not a floating canvas (operator, 2026-09-26:
   "windows floating over other windows — they should never do that; snap into
   place"). Panels are grid items: overlap is structurally impossible, drag
   reorders slots, resize changes the span. The desk scrolls when content is
   taller than the viewport. */
#desk{position:relative;overflow:auto;min-width:0;padding:12px;display:grid;
  grid-template-columns:repeat(12,1fr);grid-auto-rows:auto;gap:10px;
  grid-auto-flow:dense;align-content:start;
  background-image:radial-gradient(circle at 1px 1px,var(--dots) 1px,transparent 0);background-size:28px 28px}
.panel{background:var(--bg-1);border:1px solid var(--line-soft);
  border-radius:var(--radius);box-shadow:var(--shadow);min-width:0;min-height:64px;
  display:flex;flex-direction:column;overflow:hidden;resize:vertical;position:relative}
.panel.dragging{opacity:.45;box-shadow:none;border-style:dashed}
.panel.drop-before::before,.panel.drop-after::after{content:"";position:absolute;inset:-6px;
  border:2px solid var(--accent);border-radius:var(--radius);pointer-events:none;z-index:5}
.panel.drop-after::after{inset:-6px}
.grip{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:grab;user-select:none;
  background:linear-gradient(180deg,var(--bg-2),var(--bg-1));border-bottom:1px solid var(--line-soft);flex:0 0 auto}
.grip:active{cursor:grabbing}
.grip .dots{display:grid;grid-template-columns:repeat(2,3px);gap:3px;padding:1px 2px;flex:0 0 auto}
.grip .dots i{width:3px;height:3px;border-radius:50%;background:var(--fg-faint)}
.grip:hover .dots i{background:var(--accent)}
.grip h2{margin:0;font:700 11px var(--sans);letter-spacing:.09em;text-transform:uppercase;
  color:var(--fg-faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.grip .st{font:400 10px var(--mono);color:var(--fg-faint);flex:0 0 auto}
.grip .st.live{color:var(--ok)}
.grip .mini{background:none;border:0;color:var(--fg-faint);cursor:pointer;font:700 12px var(--mono);padding:2px 5px;flex:0 0 auto}
.grip .mini:hover{color:var(--fg)}
.pbody{flex:1;overflow:auto;padding:10px 12px;min-height:0}
.panel.collapsed{resize:none;min-height:0}
/* content-sized panels (/cluster): no fixed height, no inner scroll, nothing clipped */
.panel.auto{resize:none!important;overflow:visible!important;height:auto!important;max-height:none!important;min-height:0!important;flex:none!important}
.panel.auto .pbody{flex:none!important;overflow:visible!important;height:auto!important;max-height:none!important}
#desk.stack{display:flex;flex-direction:column;align-items:stretch}
.panel.collapsed .pbody{display:none}

/* shared bits */
.bar{background:var(--bg-0);border:1px solid var(--line-soft);border-radius:4px;height:8px;overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--info)}
.bar.w i{background:var(--warn)} .bar.b i{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{text-align:left;padding:4px 8px 4px 0;border-bottom:1px solid var(--line-soft);font-weight:normal;
  font-variant-numeric:tabular-nums;white-space:nowrap}
th{font:700 10px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-faint)}
tr:last-child td{border-bottom:none}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--fg-dim)}
#rbban{position:fixed;top:0;left:0;right:0;z-index:999;padding:8px 16px;text-align:center;font:12.5px var(--sans);color:var(--fg);
 background:color-mix(in srgb,var(--warn) 18%,var(--bg));border-bottom:1px solid color-mix(in srgb,var(--warn) 45%,var(--line))}
button.act{background:var(--bg-3);border:1px solid var(--line);color:var(--fg);padding:6px 12px;
  border-radius:var(--radius-sm);cursor:pointer;font:600 11.5px var(--sans);letter-spacing:.03em;margin:0 5px 5px 0}
button.act:hover{border-color:var(--accent);color:var(--accent)}
pre{background:var(--bg-0);border:1px solid var(--line-soft);border-radius:var(--radius-sm);
  padding:10px;overflow:auto;font:12px/1.5 var(--mono);white-space:pre-wrap;margin:8px 0 0}
input.pw{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);padding:6px;
  border-radius:var(--radius-sm);font:12px var(--mono);width:100%}

/* fleet tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.tile{background:var(--bg-2);border:1px solid var(--line-soft);border-radius:var(--radius-sm);
  padding:10px 12px;cursor:pointer;position:relative}
.tile:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line))}
.tile.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-dim) inset,0 0 18px rgba(247,147,26,.12)}
.tile .nm{font:600 14px var(--sans);display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:var(--bad)}
.dot.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.tile .sub{font:11px var(--mono);color:var(--fg-faint);margin:2px 0 8px}
.metric .v{font:600 22px var(--mono);letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.metric .u{font:10px var(--mono);color:var(--fg-faint);margin-left:3px}
.mrow{display:flex;gap:14px;align-items:baseline;justify-content:space-between}
.snapbadge{position:absolute;top:10px;right:10px;font:700 10px var(--mono);
  padding:2px 7px;border-radius:999px;border:1px solid var(--line)}
.snapbadge.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,var(--line))}
.snapbadge.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,var(--line))}
.snapbadge.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--line))}
.gauges{display:flex;flex-direction:column;gap:6px;margin-top:6px}
.gauge{display:grid;grid-template-columns:38px 1fr auto;gap:8px;align-items:center;font:11px var(--mono);color:var(--fg-dim)}

/* machine report */
.rep .hero{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.rep .hero .big{font:600 24px var(--sans);letter-spacing:.2px}
.rep h3{margin:14px 0 6px;font:700 10px var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-faint)}
.rep h3:first-child{margin-top:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.card{background:var(--bg-2);border:1px solid var(--line-soft);border-radius:var(--radius-sm);padding:8px 10px}
.card .l{font:700 9.5px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-faint);margin-bottom:3px}
.card .v{font:600 17px var(--mono);font-variant-numeric:tabular-nums}
.card .v span.u{font:10px var(--mono);color:var(--fg-faint)}
.card .s{font:10.5px var(--mono);color:var(--fg-dim);margin-top:2px}
/* live job state on tiles + report */
.tjob{margin-left:auto;display:inline-flex;align-items:center;gap:4px;font:700 11px var(--mono)}
.spin{color:var(--warn);animation:spin 1.4s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.okj{color:var(--ok)}.badj{color:var(--bad)}.qj{color:var(--fg-faint)}
.tline{margin-top:6px;min-height:14px}
.jline{font:10.5px var(--mono);letter-spacing:.02em;color:var(--fg-faint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jline.run{color:var(--warn)} .jline.ok{color:var(--ok)} .jline.bad{color:var(--bad)}
.tile.busy{border-color:color-mix(in srgb,var(--warn) 55%,var(--line-soft));
  box-shadow:0 0 0 1px color-mix(in srgb,var(--warn) 30%,transparent) inset}
.jlive{margin-top:8px;border:1px solid var(--line-soft);border-left:3px solid var(--warn);
  border-radius:var(--radius-sm);background:var(--bg-2);padding:8px 10px}
.jlive.ok{border-left-color:var(--ok)} .jlive.bad{border-left-color:var(--bad)}
.jlive .jl-h{font:700 11px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--fg-dim)}
.jlive .jl-d{color:var(--fg-faint);text-transform:none;font-weight:400}
.jlive .jl-t{max-height:150px;margin-top:6px}
.empty{color:var(--fg-faint);font:12px var(--mono);padding:18px 6px;text-align:center}
/* schedule + RPO */
.hpill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
.hpill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}
.tag{font:700 9.5px var(--mono);padding:1px 6px;border-radius:999px;border:1px solid var(--line);color:var(--fg-dim);margin-left:4px}
.tag.ok{color:var(--ok)}.tag.warn{color:var(--warn)}.tag.bad{color:var(--bad)}.tag.info{color:var(--info)}
/* navigation: anything coloured for attention is a link to the panel where you act on it */
.go{cursor:pointer}
a.tag{text-decoration:none}
.tag.go:hover,.snapbadge.go:hover,.hpill.go:hover{border-color:var(--accent);color:var(--accent)}
tr.go:hover td{background:color-mix(in srgb,var(--accent) 9%,transparent)}
tr.hl td{background:color-mix(in srgb,var(--warn) 13%,transparent)}
.golnk{color:var(--info);cursor:pointer;font:11px var(--mono);white-space:nowrap}
/* /heal: the self-repair page */
.sr{display:flex;flex-direction:column;gap:12px}
.srbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;font:12px var(--mono)}
.srbar .tag{margin-left:0}
.sr h4{margin:4px 0 2px;font:700 12px var(--mono);color:var(--fg-dim);text-transform:uppercase;letter-spacing:.06em}
.srf{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px}
.srf input[type=search]{min-width:180px}
.srrun{border:1px solid var(--line-soft);border-radius:var(--radius);background:var(--bg-1)}
.srrun>summary{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;padding:6px 10px;cursor:pointer;font:12px var(--mono)}
.srrun>summary .tag{margin-left:0}
.srsteps{width:100%;font:11.5px/1.45 var(--mono);border-top:1px solid var(--line-soft)}
.srsteps td{padding:3px 8px;vertical-align:top;border-bottom:1px solid var(--line-soft);white-space:normal;overflow-wrap:anywhere}
.srsteps td:first-child{color:var(--fg-dim);white-space:nowrap;width:1%}
.srsteps .k{white-space:nowrap;width:1%}
.srsteps pre{margin:4px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto;background:var(--bg-0);padding:6px;border-radius:4px}
.srsteps details>summary{cursor:pointer}
.srsteps code{white-space:pre-wrap;overflow-wrap:anywhere}
.srpb{display:flex;flex-wrap:wrap;gap:6px}
.srpb span{font:11px var(--mono);border:1px solid var(--line-soft);border-radius:999px;padding:2px 8px}
.srnote{font:11.5px/1.5 var(--sans);margin-top:4px;border-left:2px solid var(--line);padding-left:8px;white-space:normal}
.srt td{white-space:normal;overflow-wrap:anywhere;vertical-align:top}.srt td.nowrap{white-space:nowrap;overflow-wrap:normal}
.srscroll{overflow-x:auto;max-width:100%}
@media (max-width:640px){.srsteps td:first-child{display:none}
 .srsteps,.srsteps tbody,.srsteps tr{display:block}.srsteps tr{border-bottom:1px solid var(--line-soft);padding:3px 0}
 .srsteps td,.srsteps td.k{display:inline-block;width:auto;border:0;padding:1px 6px}.srsteps td:last-child{display:block}
 .srt,.srt tbody,.srt tr,.srt td{display:block;width:auto}.srt tr:first-child:has(th){display:none}
 .srt tr{border-bottom:1px solid var(--line-soft);padding:4px 0}.srt td{border:0;padding:1px 0}}
.golnk:hover{text-decoration:underline}
.nowrap{white-space:nowrap}
/* per-alert dismissal controls: small enough to sit in the table, explicit about the window */
.silbtn{font:10px var(--mono);color:var(--fg-dim);background:none;border:1px solid var(--line);border-radius:3px;padding:2px 5px;margin-right:3px;cursor:pointer;line-height:1.2}
.silbtn:hover:not(:disabled){color:var(--accent);border-color:var(--accent)}
.silbtn:disabled{opacity:.45;cursor:default}
.silbtn.bad{color:var(--bad);border-color:var(--bad)}
.panel.flash{animation:pflash 2.4s ease-out}
@keyframes pflash{0%,45%{box-shadow:0 0 0 2px var(--accent),0 0 22px rgba(247,147,26,.35)}100%{box-shadow:var(--shadow)}}
.sline{font:11.5px var(--mono);color:var(--fg-dim);margin-bottom:8px}
.sline b{color:var(--fg);font-weight:600}
.lnk{background:none;border:0;color:var(--info);cursor:pointer;font:11px var(--mono);padding:0 4px}
.lnk:hover{text-decoration:underline}
tr.ed td{background:var(--bg-2);padding:8px}
.cfg .cfgbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;font:12px var(--mono)}
.cfg .cfgsec{border-top:1px solid var(--line-soft);padding:10px 0 8px}
.cfg h4{margin:0 0 8px;font:700 11px var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--fg-dim)} .cfg h4 .mut{text-transform:none;letter-spacing:0;font-weight:400}
.cfg .tog{display:block;font:12.5px var(--sans);margin:4px 0} .cfg .tog .mut{font-size:11px}
.cfg .cfgcols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px 18px;margin-top:8px}
.cfg .k{font:11px var(--sans);color:var(--fg-dim);margin-bottom:3px} .cfg code{font:11px var(--mono)}
.cfg textarea,.cfg select,.cfg input[type=number]{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);border-radius:var(--radius-sm);font:12px var(--mono);padding:4px 6px}
.cfg textarea{width:100%;box-sizing:border-box;resize:vertical;min-height:34px}
.cfg input[type=number]{width:64px;margin:0 14px 0 4px}
.cfg .ret{font:12px var(--sans)}
.cfg input.asin,.cfg textarea.asin,.cfg input[type=password],.cfg #as-pick{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);border-radius:var(--radius-sm);font:12px var(--mono);padding:4px 6px;width:100%;box-sizing:border-box;max-width:520px}
.cfg input[type=password]{width:240px}
input.sw{appearance:none;-webkit-appearance:none;width:36px;height:20px;border-radius:10px;background:var(--bg-3);border:1px solid var(--line);position:relative;cursor:pointer;vertical-align:middle;margin:4px 0;transition:background .15s}
input.sw::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--fg-dim);transition:left .15s,background .15s}
input.sw:checked{background:var(--accent);border-color:var(--accent)} input.sw:checked::after{left:18px;background:var(--accent-ink)}
.cfg .cfgbar{position:sticky;top:0;z-index:6;background:var(--bg-1);padding:8px 0;margin:-4px 0 6px;border-bottom:1px solid var(--line-soft)}
.cfg .gnote{margin:-2px 0 6px;font:11.5px var(--sans);color:var(--fg-faint)}
.cfg .setrow{grid-template-columns:170px minmax(0,1fr);max-width:980px} .cfg .setrow textarea{width:100%}
.cfg .setrow input[type=number]{margin:0 6px 0 0}
.cfgt{width:100%} .cfgt td{vertical-align:top;padding:6px 8px 6px 0} .cfgt td:last-child{min-width:200px;width:24%}
.cfgt td{white-space:normal} .cfgt .dbs{line-height:1.9}
.rslots{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px;margin-bottom:12px}
.rslot{border:1px solid var(--line);border-radius:var(--radius-sm);padding:10px 12px;background:var(--bg-1);font:12.5px var(--sans);display:flex;flex-direction:column;gap:6px}
.rslot .rh{display:flex;align-items:center;gap:8px} .rslot .rh b{font:700 13px var(--mono)} .rslot .rh .hspacer{flex:1}
.rslot .rm{font:12px var(--mono);word-break:break-word} .rslot .drift{color:var(--warn);font-size:11.5px}
.rslot .ract{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:2px}
.rslot select,.rslot input,.rcat input,.rcat select{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);border-radius:var(--radius-sm);font:12px var(--mono);padding:4px 6px}
.rslot input{width:110px} .rslot details{font-size:11.5px} .rslot details td{padding:1px 8px 1px 0;font:11px var(--mono)}
.rcat td{font:11.5px var(--mono);padding:3px 10px 3px 0;vertical-align:top} .rcat .radd{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
.rcat .radd input[name=repo]{flex:1;min-width:260px}
.dbc{display:inline-flex;align-items:center;gap:4px;margin:0 10px 0 0;font:11px var(--mono);white-space:nowrap;cursor:pointer}
.dbc .mut{font-size:10px} .hfline{font:12px var(--mono);display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;margin-bottom:6px} .dbts{font-size:10px;display:block;line-height:1.4}
.cfgpre{background:var(--bg-0);border:1px solid var(--line-soft);border-radius:var(--radius-sm);padding:8px;font:11px var(--mono);white-space:pre-wrap;max-height:340px;overflow:auto;margin:6px 0 0}
.cfgpre.bad{border-color:var(--bad)} .cfgpre.hide{display:none}
.cfg details summary{cursor:pointer;font:12px var(--sans);color:var(--fg-dim)}
.ed input{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);padding:4px 6px;
  border-radius:var(--radius-sm);font:12px var(--mono);width:84px;margin-right:6px}
.ed label{font:10px var(--sans);letter-spacing:.06em;text-transform:uppercase;color:var(--fg-faint);margin-right:4px}
.ed .hint{font:10.5px var(--mono);color:var(--fg-faint);margin-top:6px;white-space:normal}
.jline.warn{color:var(--warn)}
.tnext{font:10.5px var(--mono);color:var(--fg-faint)}
/* live metrics: tile sparklines, report tabs + charts */
.mgrid{display:grid;gap:3px;margin:2px 0 4px}
.mcell{display:grid;grid-template-columns:28px 1fr 70px;align-items:center;gap:6px;font:11px var(--mono);color:var(--fg-faint)}
.mcell .mv{color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.spk{width:70px;height:18px;display:block}
.spk path{fill:none;stroke:var(--info);stroke-width:1.5;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.spk path.a{stroke:var(--accent)} .spk path.g{stroke:var(--ok)} .spk path.w{stroke:var(--warn)} .spk path.dim{stroke:var(--fg-faint)}
/* chart marks (dataviz: 2px lines, 10% wash, ringed end-dot, hairline grid, ink text) */
.chart .ln{fill:none;stroke:var(--info);stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.chart .wash{fill:var(--info);opacity:.11} .chart .dot{fill:var(--info);stroke:var(--bg-0);stroke-width:2}
.chart .ln.a{stroke:var(--accent)} .chart .wash.a{fill:var(--accent)} .chart .dot.a{fill:var(--accent)} .key.a{background:var(--accent)}
.chart .ln.g{stroke:var(--ok)} .chart .wash.g{fill:var(--ok)} .chart .dot.g{fill:var(--ok)} .key.g{background:var(--ok)}
.chart .ln.w{stroke:var(--warn)} .chart .wash.w{fill:var(--warn)} .chart .dot.w{fill:var(--warn)} .key.w{background:var(--warn)}
.chart .gl{stroke:var(--line-soft);stroke-width:1;vector-effect:non-scaling-stroke}
.chart .tk{font:7.5px var(--mono);fill:var(--fg-faint);text-anchor:end} .chart .ax{font:7.5px var(--mono);fill:var(--fg-faint)} .chart .ax.r{text-anchor:end}
.chart .xh{stroke:var(--fg-dim);stroke-width:1;vector-effect:non-scaling-stroke;pointer-events:none}
.chart .hd{fill:var(--fg);stroke:var(--bg-0);stroke-width:2;pointer-events:none}
.key{display:inline-block;width:10px;height:2px;border-radius:1px;background:var(--info);vertical-align:middle;margin-right:5px}
.tip{position:fixed;z-index:1000;background:var(--bg-2);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font:11px var(--mono);box-shadow:var(--shadow);pointer-events:none;min-width:120px}
.tip-t{color:var(--fg-faint);margin-bottom:3px} .tip-r{display:flex;gap:6px;align-items:center;white-space:nowrap} .tip-r b{color:var(--fg);font-variant-numeric:tabular-nums} .tip-r span{color:var(--fg-dim)}
/* dim only when a refresh is slow (>1.2 s) or failed — never on the routine 15 s tick.
   The wall never dims for a fetch (operator, 2026-10-22: no flash, ever): on /kiosk the stale
   flag marks the status pill only, the screen itself holds its last good frame */
.pbody{transition:opacity .3s} .pbody.stale{opacity:.65}
body.kiosk .pbody{transition:none} body.kiosk .pbody.stale{opacity:1}
/* categorical series palette: blue, orange, green, magenta, cyan, yellow, red, violet */
:root{--c0:#3987e5;--c1:#d95926;--c2:#199e70;--c3:#c98500;--c4:#d55181;--c5:#008300;--c6:#9085e9;--c7:#e66767}
.spk path.c0,.chart .ln.c0{stroke:var(--c0)} .chart .wash.c0{fill:var(--c0)} .chart .dot.c0{fill:var(--c0)} .key.c0{background:var(--c0)}
.spk path.c1,.chart .ln.c1{stroke:var(--c1)} .chart .wash.c1{fill:var(--c1)} .chart .dot.c1{fill:var(--c1)} .key.c1{background:var(--c1)}
.spk path.c2,.chart .ln.c2{stroke:var(--c2)} .chart .wash.c2{fill:var(--c2)} .chart .dot.c2{fill:var(--c2)} .key.c2{background:var(--c2)}
.spk path.c3,.chart .ln.c3{stroke:var(--c3)} .chart .wash.c3{fill:var(--c3)} .chart .dot.c3{fill:var(--c3)} .key.c3{background:var(--c3)}
.spk path.c4,.chart .ln.c4{stroke:var(--c4)} .chart .wash.c4{fill:var(--c4)} .chart .dot.c4{fill:var(--c4)} .key.c4{background:var(--c4)}
.spk path.c5,.chart .ln.c5{stroke:var(--c5)} .chart .wash.c5{fill:var(--c5)} .chart .dot.c5{fill:var(--c5)} .key.c5{background:var(--c5)}
.spk path.c6,.chart .ln.c6{stroke:var(--c6)} .chart .wash.c6{fill:var(--c6)} .chart .dot.c6{fill:var(--c6)} .key.c6{background:var(--c6)}
.spk path.c7,.chart .ln.c7{stroke:var(--c7)} .chart .wash.c7{fill:var(--c7)} .chart .dot.c7{fill:var(--c7)} .key.c7{background:var(--c7)}
.flags{display:flex;gap:4px;flex-wrap:wrap;margin-top:3px;min-height:0}
.dtabs{display:flex;gap:2px;margin:10px 0 2px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.dtab{background:none;border:0;border-bottom:2px solid transparent;color:var(--fg-dim);padding:5px 9px;
  font:700 10.5px var(--sans);letter-spacing:.07em;text-transform:uppercase;cursor:pointer;margin-bottom:-1px}
.dtab:hover{color:var(--fg)} .dtab.on{color:var(--accent);border-bottom-color:var(--accent)}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 12px}
.chart{margin:4px 0 6px;min-width:0}
.chart svg{width:100%;height:auto;display:block;background:var(--bg-0);border-radius:6px;cursor:crosshair}
.ch-h{font:10.5px var(--mono);display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;margin-bottom:3px;color:var(--fg-dim)}
.ch-t{font:600 11px var(--sans);color:var(--fg);letter-spacing:.02em}
.ch-h b{color:var(--fg);font-weight:600} .lg{color:var(--fg-dim);white-space:nowrap}
.ch-max{margin-left:auto;color:var(--fg-faint)}
.ch-empty{font:10.5px var(--mono);color:var(--fg-faint);margin-top:-46px;height:44px;display:flex;align-items:center;justify-content:center;position:relative;pointer-events:none}
/* hero + stat tiles (cluster page) */
.hero{display:flex;align-items:flex-end;gap:14px;padding:4px 0 10px;border-bottom:1px solid var(--line-soft);margin-bottom:10px;flex-wrap:wrap}
.hero .hv{font:600 52px/1 var(--sans);letter-spacing:-1.5px;color:var(--fg)}
.hero .hl{font:12px var(--sans);color:var(--fg-dim);padding-bottom:6px} .hero .hl b{color:var(--fg);font-weight:600}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto;justify-content:flex-end}
.stat{background:var(--bg-2);border:1px solid var(--line-soft);border-radius:var(--radius-sm);padding:8px 12px;min-width:132px}
.stat .l{font:700 9.5px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-faint);margin-bottom:2px}
.stat .v{font:600 20px/1.2 var(--sans);color:var(--fg)} .stat .v .u{font:10px var(--mono);color:var(--fg-faint);margin-left:3px}
.stat .s{font:10.5px var(--mono);color:var(--fg-dim);margin-top:2px} .stat .spk{width:100%;height:16px;margin-top:4px}
/* ring gauges */
.rings{display:flex;gap:6px;justify-content:space-between;margin:8px 0 2px}
.ring{position:relative;width:58px;text-align:center} .ring svg{width:58px;height:58px;display:block}
.ring .track{fill:none;stroke:var(--bg-3);stroke-width:5} .ring .fill{fill:none;stroke:var(--info);stroke-width:5;stroke-linecap:round;transition:stroke-dasharray .4s}
.ring .fill.a{stroke:var(--accent)} /* memory stays neutral: on the GB10s ~97% "used" is the resident model, not pressure */ .ring .fill.ok{stroke:var(--ok)} .ring .fill.warn{stroke:var(--warn)} .ring .fill.bad{stroke:var(--bad)}
.ring .rv{position:absolute;top:19px;left:0;right:0;font:600 12px var(--sans);color:var(--fg)} .ring .rv small{font:9px var(--mono);color:var(--fg-faint);display:block;margin-top:-1px}
.ring .rl{font:700 9px var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--fg-faint);margin-top:2px}
.nc{border-top:2px solid var(--line)} .nc.ok{border-top-color:color-mix(in srgb,var(--ok) 60%,var(--line))} .nc.warn{border-top-color:var(--warn)} .nc.bad{border-top-color:var(--bad)}
.nc .charts{grid-template-columns:1fr 1fr;gap:6px 10px;margin-top:6px}
.cl tr:nth-child(even) td{background:color-mix(in srgb,var(--bg-2) 45%,transparent)}
/* control room (/cluster): one row per node, everything on one screen */
.ctl .hero{padding:0 0 4px;margin-bottom:5px} .ctl .hero .hv{font-size:clamp(34px,2.6vw,46px)}
.ctl .stat{padding:6px 10px;min-width:120px} .ctl .stat .v{font-size:18px} .ctl .stat .spk{height:14px;margin-top:2px}
.nrow{display:grid;grid-template-columns:150px 176px 330px minmax(0,1fr);gap:0 12px;align-items:center;
  padding:3px 8px;border-radius:var(--radius-sm);background:var(--bg-2);border:1px solid var(--line-soft);border-left:3px solid var(--line);margin-bottom:3px}
.nrow.ok{border-left-color:color-mix(in srgb,var(--ok) 60%,var(--line))} .nrow.warn{border-left-color:var(--warn)} .nrow.bad{border-left-color:var(--bad)}
.nid .nm{font:600 13px var(--sans);display:flex;align-items:center;gap:6px;line-height:1.3}
.nid .md{font:10.5px var(--mono);color:var(--fg-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px;line-height:1.3}
.nid .fl{display:inline-flex;gap:2px;min-width:0;overflow:hidden} .nid .fl .tag{padding:0 4px;font-size:8.5px;margin-left:0;white-space:nowrap}
.nrings{display:flex;gap:4px} .nrings .ring{width:42px} .nrings .ring svg{width:42px;height:42px}
.nrings .ring .rv{top:14px;font-size:10.5px} .nrings .ring .rv small{font-size:8px} .nrings .ring .rl{font-size:7.5px;margin-top:0;line-height:1.1;white-space:nowrap}
.nrings .ring .rl b{font:600 10px var(--mono);letter-spacing:0;text-transform:none;color:var(--fg)} .nrings .ring .rl b.warn{color:var(--warn)} .nrings .ring .rl b.bad{color:var(--bad)}
.nst{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px 8px;font:10.5px var(--mono)}
.nst>div{display:flex;align-items:baseline;gap:5px;min-width:0}
.nst .k{color:var(--fg-faint);font-size:8px;letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;flex:0 0 auto;line-height:1.35}
.nst .v{color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.35;min-width:0} .nst .v .u{color:var(--fg-faint);font-size:9.5px}
.nst.wk{grid-template-columns:repeat(2,minmax(0,1fr))} .nst .full{grid-column:1/-1}
.nch{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 10px;min-width:0}
.nch .chart{margin:0} .nch .ch-h{font-size:9.5px;margin-bottom:1px;line-height:1.2} .nch .ch-t{font-size:9.5px} .nch .ch-max{display:none}
.nch .chart svg{border-radius:4px} .nch .ch-h{flex-wrap:nowrap;white-space:nowrap;overflow:hidden} .chart svg.cmp{height:30px}
.nid .nm>.tjob{white-space:nowrap;flex:0 0 auto} .nid .nm .fl{flex:0 1 auto}
.ctl .foot{display:flex;flex-wrap:wrap;gap:4px 18px;font:10.5px var(--mono);color:var(--fg-dim);margin-top:6px;line-height:1.45}
.ctl .foot b{color:var(--fg);font-weight:600}
@media (max-width:1400px){
 .nch{grid-template-columns:repeat(2,minmax(0,1fr))} .nch .chart:nth-child(3){display:none}
 .nrow{grid-template-columns:165px 176px 265px minmax(0,1fr)}
 .nst{grid-template-columns:repeat(2,minmax(0,1fr))} .nst:not(.wk)>div:nth-child(n+7){display:none}
 .ctl .hero{flex-wrap:nowrap;align-items:center} .ctl .hero .hl{white-space:nowrap}
 .ctl .stats{flex:1;width:auto;margin-left:10px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;justify-content:stretch}
 .ctl .stat{min-width:0;padding:5px 8px} .ctl .stat .v{font-size:16px} .ctl .stat .s{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
}
@media (max-width:1150px){.nrow{grid-template-columns:150px 176px 240px minmax(0,1fr)} .nid .md{max-width:150px} .ctl .hero .hl{display:none}}
@media (max-width:1000px){.nrow{grid-template-columns:150px 176px minmax(0,1fr)} .nst{display:none} .ctl .stats{grid-template-columns:repeat(3,minmax(0,1fr))}}
.dot.ok{box-shadow:0 0 6px var(--ok),0 0 2px var(--ok)}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;font:12px var(--mono)}
.kv .k{color:var(--fg-faint)}
/* snapshot browser */
.crumb{font:12px var(--mono);margin:6px 0;display:flex;flex-wrap:wrap;gap:2px;align-items:center}
.crumb button{background:none;border:0;color:var(--info);cursor:pointer;font:12px var(--mono);padding:1px 3px}
.crumb button:hover{text-decoration:underline} .crumb .sep{color:var(--fg-faint)}
.brow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.brow select,.brow input{background:var(--bg-0);border:1px solid var(--line);color:var(--fg);padding:4px 6px;border-radius:var(--radius-sm);font:12px var(--mono)}
.brow input{flex:1;min-width:140px}
tr.dir td:first-child{cursor:pointer;color:var(--info)} tr.dir td:first-child:hover{text-decoration:underline}
td.acts{white-space:nowrap} td.acts a,td.acts button.lnk{margin-right:6px}
/* cluster at-a-glance */
.cl{font-size:11.5px;width:auto;max-width:100%} .cl td,.cl th{padding:4px 10px 4px 0;vertical-align:middle;line-height:1.3}
.cl td.n{white-space:nowrap} .cl .spk{width:52px;height:14px;vertical-align:middle}
.llms{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:3px 10px;margin:6px 0 4px;font:11px var(--mono)}
.llms .v{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.llms .k{color:var(--fg-faint);font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;display:block}
.llms .v{color:var(--fg);font-variant-numeric:tabular-nums} .llms .v .u{color:var(--fg-faint);font-size:10px}
.cl .hn{font:600 13px var(--sans)} .cl .md{font:11px var(--mono);color:var(--fg-dim);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cl .n{font-variant-numeric:tabular-nums;text-align:right} .cl .u{color:var(--fg-faint);font-size:10px}
.cl .wk{color:var(--fg-faint);font-style:italic}
.cltot{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}
.clwrap{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,36%);gap:6px 22px;align-items:start}
.clwrap>div:first-child{min-width:0;overflow-x:auto}   /* a too-wide table scrolls, never paints under the charts */
.clwrap .charts{grid-template-columns:1fr 1fr}
@media (max-width:1700px){.clwrap{grid-template-columns:minmax(0,1fr)}.clwrap .charts{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media (max-width:1300px){
 .clwrap .charts{grid-template-columns:repeat(2,minmax(0,1fr))}
 .stats{margin-left:0;width:100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
 .stat{min-width:0}
 .cl .lp{display:none}                      /* prefill / latency / root·io live in the node cards */
 .hero{gap:10px 14px}
}
@media (max-width:1000px){
 .clwrap .charts{grid-template-columns:minmax(0,1fr)}
 .cl .lp2{display:none}
 .nc .charts{grid-template-columns:1fr 1fr}
}
.hero .hv{font-size:clamp(36px,3.2vw,52px)}
.strip{display:flex;flex-wrap:wrap;gap:6px 16px;font:11px var(--mono);color:var(--fg-dim);margin-top:8px}
.strip b{color:var(--fg);font-weight:600}
.roster td{font-size:11.5px}
.cltot .card{min-width:120px}
.snaprow{cursor:pointer} .snaprow:hover td{background:var(--bg-2)} .snaprow.sel td{background:var(--bg-3)}
@media (max-width:860px){.tiles{grid-template-columns:1fr}}

/* ── shared by every theme: the kiosk's per-node meters ── */
.kmeters{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:7px 10px;align-items:center;margin-top:12px;font:700 9.5px var(--sans);letter-spacing:.1em;color:var(--fg-faint)}
.kmeters .bar{min-width:0}
.kclock b small{font:600 14px var(--sans);margin-left:6px;color:var(--fg-dim);letter-spacing:.04em}

/* ── Phosphor: an instrument panel. Every subsystem has one colour everywhere (data-k on the meter);
   segmented LED meters with unlit segments; oscilloscope grids behind every trace; LCD numerals with
   their unlit 8s; glow tuned per mode (--gl). Fonts ship with the console (assets/fonts, OFL). ── */
@font-face{font-family:Michroma;src:url(/assets/fonts/michroma.woff2) format('woff2');font-display:swap}
@font-face{font-family:DSEG7;src:url(/assets/fonts/dseg7.woff2) format('woff2');font-display:block}
html[data-style=phosphor]{
 --k-cpu:#3dff8f;--k-gpu:#3b8cff;--k-temp:#ffb020;--k-mem:#c3a6ff;--k-net:#22d3ee;--k-disk:#2ee6a6;--k-power:#ffd60a;--k-llm:#6ff5d2;--k-ok:#3dff8f;--k-alert:#ff4d5e;
 --k:var(--accent);--gl:1;--display:Michroma,var(--sans);--lcd:DSEG7,var(--mono);--radius:9px;--radius-sm:6px;
 --c0:var(--k-gpu);--c1:var(--k-temp);--c2:var(--k-cpu);--c3:var(--k-power);--c4:var(--k-llm);--c5:var(--k-disk);--c6:var(--k-mem);--c7:var(--k-alert)}
html[data-style=phosphor][data-theme=light]{--gl:.35;
 --k-cpu:#0b9b55;--k-gpu:#1f5fe0;--k-temp:#c77700;--k-mem:#8a3fe0;--k-net:#0891b2;--k-disk:#0d9b6c;--k-power:#a87d00;--k-llm:#c026a0;--k-ok:#0b9b55;--k-alert:#d1293d}
html[data-style=phosphor] [data-k=cpu]{--k:var(--k-cpu)} html[data-style=phosphor] [data-k=gpu]{--k:var(--k-gpu)}
html[data-style=phosphor] [data-k=temp]{--k:var(--k-temp)} html[data-style=phosphor] [data-k=mem]{--k:var(--k-mem)}
html[data-style=phosphor] [data-k=net]{--k:var(--k-net)} html[data-style=phosphor] [data-k=disk]{--k:var(--k-disk)}
html[data-style=phosphor] [data-k=power]{--k:var(--k-power)} html[data-style=phosphor] [data-k=llm]{--k:var(--k-llm)}
html[data-style=phosphor] [data-k=ok]{--k:var(--k-ok)} html[data-style=phosphor] [data-k=alert]{--k:var(--k-alert)}

/* the room: deep glass, two coloured light sources, a faint graticule */
html[data-style=phosphor] body{background:
 radial-gradient(1100px 620px at 100% -8%,color-mix(in srgb,var(--k-net) calc(10% * var(--gl)),transparent),transparent 62%),
 radial-gradient(900px 560px at -6% 108%,color-mix(in srgb,var(--k-cpu) calc(8% * var(--gl)),transparent),transparent 60%),
 linear-gradient(color-mix(in srgb,var(--fg) 2.6%,transparent) 1px,transparent 1px) 0 0/44px 44px,
 linear-gradient(90deg,color-mix(in srgb,var(--fg) 2.6%,transparent) 1px,transparent 1px) 0 0/44px 44px,var(--bg)}
html[data-style=phosphor] header.top{background:linear-gradient(180deg,color-mix(in srgb,var(--bg-1) 94%,transparent),color-mix(in srgb,var(--bg) 88%,transparent));
 border-bottom:1px solid color-mix(in srgb,var(--accent) 22%,var(--line));
 box-shadow:0 1px 0 color-mix(in srgb,var(--accent) calc(14% * var(--gl)),transparent),0 10px 34px rgba(0,0,0,calc(.4 * var(--gl)))}
html[data-style=phosphor] .wordmark{font-family:var(--display);font-weight:400;letter-spacing:.2em;
 text-shadow:0 0 calc(14px * var(--gl)) color-mix(in srgb,var(--accent) 55%,transparent)}
html[data-style=phosphor] .menu-title{font-family:var(--display);font-weight:400;letter-spacing:.06em;font-size:9px}
html[data-style=phosphor] .hpill{box-shadow:inset 0 0 12px color-mix(in srgb,currentColor calc(12% * var(--gl)),transparent),0 0 calc(10px * var(--gl)) color-mix(in srgb,currentColor calc(14% * var(--gl)),transparent)}

/* panels: machined glass with a lit top edge */
html[data-style=phosphor] .panel{background:linear-gradient(180deg,color-mix(in srgb,var(--bg-1) 96%,var(--fg)),var(--bg-1) 40px);
 border-color:color-mix(in srgb,var(--accent) 9%,var(--line-soft));
 box-shadow:inset 0 1px 0 color-mix(in srgb,var(--accent) calc(20% * var(--gl)),transparent),0 12px 36px rgba(0,0,0,calc(.45 * var(--gl)))}
html[data-style=phosphor] .grip{border-bottom:1px solid color-mix(in srgb,var(--accent) 8%,var(--line-soft))}
html[data-style=phosphor] .grip h2{font-family:var(--display);font-weight:400;font-size:10px;letter-spacing:.2em;color:var(--fg)}
html[data-style=phosphor] th,html[data-style=phosphor] .card .l,html[data-style=phosphor] .ring .rl{font-family:var(--display);font-weight:400;letter-spacing:.14em}
html[data-style=phosphor] th{font-size:8.5px}

/* LED meters: lit segments glow, unlit ones stay visible; disk and temperature run green → amber → red */
html[data-style=phosphor] .bar{height:11px;border-radius:2px;padding:1px;
 border:1px solid color-mix(in srgb,var(--k) 24%,var(--line-soft));
 background:repeating-linear-gradient(90deg,color-mix(in srgb,var(--k) 15%,var(--bg-0)) 0 5px,var(--bg-0) 5px 7px)}
html[data-style=phosphor] .bar i{border-radius:1px;
 background:repeating-linear-gradient(90deg,transparent 0 5px,var(--bg-0) 5px 7px),var(--k);
 filter:drop-shadow(0 0 calc(4px * var(--gl)) color-mix(in srgb,var(--k) 75%,transparent))}
html[data-style=phosphor] .bar:not(.n){border-color:color-mix(in srgb,var(--k-ok) 22%,var(--line-soft));
 background:repeating-linear-gradient(90deg,transparent 0 5px,var(--bg-0) 5px 7px),
  linear-gradient(90deg,color-mix(in srgb,var(--k-ok) 15%,var(--bg-0)) 55%,color-mix(in srgb,var(--k-temp) 18%,var(--bg-0)) 80%,color-mix(in srgb,var(--k-alert) 20%,var(--bg-0)))}
html[data-style=phosphor] .bar:not(.n) i{
 background:repeating-linear-gradient(90deg,transparent 0 5px,var(--bg-0) 5px 7px),
  linear-gradient(90deg,var(--k-ok) 55%,var(--k-temp) 80%,var(--k-alert)) 0 0/calc(10000% / var(--p,100)) 100% no-repeat;
 filter:drop-shadow(0 0 calc(4px * var(--gl)) color-mix(in srgb,var(--k-ok) 55%,transparent))}
html[data-style=phosphor] .bar.w i{filter:drop-shadow(0 0 calc(5px * var(--gl)) color-mix(in srgb,var(--k-temp) 70%,transparent))}
html[data-style=phosphor] .bar.b i{filter:drop-shadow(0 0 calc(6px * var(--gl)) color-mix(in srgb,var(--k-alert) 80%,transparent))}

/* oscilloscope traces */
html[data-style=phosphor] .spk{border-radius:3px;border:1px solid color-mix(in srgb,var(--k) 16%,var(--line-soft));
 background:linear-gradient(color-mix(in srgb,var(--k) 9%,transparent) 1px,transparent 1px) 0 0/100% 6px,
  linear-gradient(90deg,color-mix(in srgb,var(--k) 9%,transparent) 1px,transparent 1px) 0 0/10px 100%,var(--bg-0)}
html[data-style=phosphor] .spk path,html[data-style=phosphor] .spk path.a,html[data-style=phosphor] .spk path.dim{stroke:var(--k)}
html[data-style=phosphor] .spk path{stroke-width:1.6;filter:drop-shadow(0 0 calc(2.5px * var(--gl)) var(--k))}
html[data-style=phosphor] .spk path.g{stroke:var(--k-cpu)} html[data-style=phosphor] .spk path.c0{stroke:var(--k-net)}
html[data-style=phosphor] .chart svg{border:1px solid color-mix(in srgb,var(--accent) 12%,var(--line-soft));border-radius:5px;
 background:linear-gradient(color-mix(in srgb,var(--accent) 6%,transparent) 1px,transparent 1px) 0 0/100% 12px,
  linear-gradient(90deg,color-mix(in srgb,var(--accent) 6%,transparent) 1px,transparent 1px) 0 0/24px 100%,
  radial-gradient(120% 140% at 50% 0%,color-mix(in srgb,var(--accent) calc(5% * var(--gl)),transparent),transparent 70%),var(--bg-0);
 box-shadow:inset 0 0 28px rgba(0,0,0,calc(.55 * var(--gl)))}
html[data-style=phosphor] .chart .ln{--g:var(--info);stroke-width:2.2;filter:drop-shadow(0 0 calc(3px * var(--gl)) var(--g))}
html[data-style=phosphor] .chart .ln.a{--g:var(--accent)} html[data-style=phosphor] .chart .ln.g{--g:var(--ok)} html[data-style=phosphor] .chart .ln.w{--g:var(--warn)}
html[data-style=phosphor] .chart .ln.c0{--g:var(--c0)} html[data-style=phosphor] .chart .ln.c1{--g:var(--c1)} html[data-style=phosphor] .chart .ln.c2{--g:var(--c2)} html[data-style=phosphor] .chart .ln.c3{--g:var(--c3)}
html[data-style=phosphor] .chart .ln.c4{--g:var(--c4)} html[data-style=phosphor] .chart .ln.c5{--g:var(--c5)} html[data-style=phosphor] .chart .ln.c6{--g:var(--c6)} html[data-style=phosphor] .chart .ln.c7{--g:var(--c7)}
html[data-style=phosphor] .chart .wash{opacity:.16}
html[data-style=phosphor] .chart .gl{stroke:color-mix(in srgb,var(--accent) 12%,var(--line-soft));stroke-dasharray:2 3}

/* LED rings */
html[data-style=phosphor] .ring .track{stroke:color-mix(in srgb,var(--k) 16%,var(--bg-3));stroke-dasharray:2.4 1.3}
html[data-style=phosphor] .ring .fill:not(.ok):not(.warn):not(.bad){stroke:var(--k)}
html[data-style=phosphor] .ring .fill{stroke-linecap:butt;filter:drop-shadow(0 0 calc(3px * var(--gl)) var(--k))}

/* stat cards wear their subsystem's colour */
html[data-style=phosphor] .stat{border-color:color-mix(in srgb,var(--k) 30%,var(--line-soft));
 background:linear-gradient(180deg,color-mix(in srgb,var(--k) 9%,var(--bg-2)),color-mix(in srgb,var(--k) 2%,var(--bg-2)));
 box-shadow:inset 0 1px 0 color-mix(in srgb,var(--k) 28%,transparent)}
html[data-style=phosphor] .card[data-k]{border-color:color-mix(in srgb,var(--k) 26%,var(--line-soft));
 background:linear-gradient(180deg,color-mix(in srgb,var(--k) 7%,var(--bg-2)),var(--bg-2));box-shadow:inset 0 1px 0 color-mix(in srgb,var(--k) 24%,transparent)}
html[data-style=phosphor] .card[data-k] .l{color:var(--k)}
html[data-style=phosphor] .card[data-k] .v{text-shadow:0 0 calc(10px * var(--gl)) color-mix(in srgb,var(--k) 35%,transparent)}
html[data-style=phosphor] .stat .l{font-family:var(--display);font-weight:400;font-size:8.5px;letter-spacing:.16em;color:var(--k)}
html[data-style=phosphor] .stat .v{color:var(--k);text-shadow:0 0 calc(12px * var(--gl)) color-mix(in srgb,var(--k) 45%,transparent)}

/* fleet tiles: label in its colour, selection lit */
html[data-style=phosphor] .tile{background:linear-gradient(180deg,var(--bg-2),color-mix(in srgb,var(--bg-2) 60%,var(--bg-0)));border-color:color-mix(in srgb,var(--accent) 7%,var(--line-soft))}
html[data-style=phosphor] .tile .nm{font-family:var(--display);font-weight:400;letter-spacing:.06em}
html[data-style=phosphor] .tile.sel{border-color:var(--accent);box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 60%,transparent),0 0 calc(26px * var(--gl)) color-mix(in srgb,var(--accent) 28%,transparent)}
html[data-style=phosphor] .mcell>span:first-child,html[data-style=phosphor] .gauge>span:first-child{color:var(--k);font-weight:700;text-transform:uppercase;letter-spacing:.06em}
html[data-style=phosphor] .dot.ok{box-shadow:0 0 calc(9px * var(--gl)) var(--ok),0 0 2px var(--ok)}
html[data-style=phosphor] .dot{box-shadow:0 0 calc(9px * var(--gl)) color-mix(in srgb,var(--bad) 80%,transparent)}

/* LCD numerals: digits lit over their unlit 8s */
html[data-style=phosphor] .hero .hv,html[data-style=phosphor] .kbig b,html[data-style=phosphor] .kclock b{
 font-family:var(--lcd);font-weight:700;letter-spacing:.04em;position:relative;z-index:0}
/* the ghost 8s are positioned against their own number: without this the vitals' ghosts land at the page's top left */
html[data-style=phosphor] .kvital b{position:relative;z-index:0}
html[data-style=phosphor] .hero .hv{font-size:clamp(34px,3vw,50px);color:var(--accent);
 text-shadow:0 0 calc(20px * var(--gl)) color-mix(in srgb,var(--accent) 55%,transparent),0 0 2px color-mix(in srgb,var(--accent) 80%,transparent)}
html[data-style=phosphor] [data-ghost]::before{content:attr(data-ghost);position:absolute;left:0;top:0;z-index:-1;
 color:color-mix(in srgb,currentColor 9%,transparent);text-shadow:none;pointer-events:none;white-space:pre}
html[data-style=phosphor] .kbig b:not(.ok):not(.warn):not(.bad){color:var(--k)}
html[data-style=phosphor] .kbig b{font-size:var(--kbig-fs)} html[data-style=phosphor] .kclock b{font-size:var(--kclock-fs)}
html[data-style=phosphor] .kbig b,html[data-style=phosphor] .kclock b{
 text-shadow:0 0 calc(18px * var(--gl)) color-mix(in srgb,currentColor 55%,transparent),0 0 2px color-mix(in srgb,currentColor 80%,transparent)}
html[data-style=phosphor] .kclock b{color:var(--accent)}
html[data-style=phosphor] .kbig span,html[data-style=phosphor] .kclock span{font-family:var(--display);font-size:calc(9px*var(--kf-h,1));letter-spacing:.18em;margin-top:calc(6px*var(--kf-h,1))}
html[data-style=phosphor] .kpair .kbig>span{letter-spacing:.12em;margin-top:0}
html[data-style=phosphor] .kpair [data-ghost]::before{right:0;text-align:center}
html[data-style=phosphor] .kmeters .bar{height:calc(13px*var(--kf-h,1))}

/* kiosk cards and the CRT */
html[data-style=phosphor] .kslot,html[data-style=phosphor] .knode{background:linear-gradient(180deg,color-mix(in srgb,var(--bg-1) 94%,var(--fg)),var(--bg-1) calc(44px*var(--kf-h,1)));
 border-color:color-mix(in srgb,var(--accent) 10%,var(--line-soft));box-shadow:inset 0 1px 0 color-mix(in srgb,var(--accent) calc(22% * var(--gl)),transparent),0 12px 30px rgba(0,0,0,calc(.45 * var(--gl)))}
html[data-style=phosphor] .kslot.down,html[data-style=phosphor] .knode.down{border-color:color-mix(in srgb,var(--bad) 60%,var(--line));
 box-shadow:inset 0 1px 0 color-mix(in srgb,var(--bad) 40%,transparent),0 0 calc(26px * var(--gl)) color-mix(in srgb,var(--bad) 18%,transparent)}
html[data-style=phosphor] .kh b{font-family:var(--display);font-weight:400;letter-spacing:.06em}
html[data-style=phosphor] .km,html[data-style=phosphor] .kllm b{color:var(--k-llm);text-shadow:0 0 calc(10px * var(--gl)) color-mix(in srgb,var(--k-llm) 45%,transparent)}
html[data-style=phosphor] .kmeters>span{font-family:var(--display);font-weight:400;font-size:8.5px;letter-spacing:.14em}
html[data-style=phosphor] .kmeters>span:nth-child(1){color:var(--k-gpu)} html[data-style=phosphor] .kmeters>span:nth-child(3){color:var(--k-cpu)}
html[data-style=phosphor] .kmeters>span:nth-child(5){color:var(--k-mem)} html[data-style=phosphor] .kmeters>span:nth-child(7){color:var(--k-temp)}
/* effects: each one fixed layer that never repaints; the two moving ones animate transform / opacity only */
html[data-style=phosphor][data-fx-scan=all] body::after,html[data-style=phosphor][data-fx-scan=kiosk] body.kiosk::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:60;
 background:repeating-linear-gradient(0deg,rgba(0,0,0,.17) 0 1px,transparent 1px 3px);will-change:opacity}
html[data-style=phosphor][data-theme=light][data-fx-scan] body::after{background:repeating-linear-gradient(0deg,rgba(0,0,0,.05) 0 1px,transparent 1px 3px)}
html[data-style=phosphor][data-fx-vignette=on]::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:61;
 background:radial-gradient(130% 115% at 50% 50%,transparent 60%,rgba(0,0,0,calc(.5 * min(var(--gl),1))))}
html[data-style=phosphor][data-theme=light][data-fx-vignette=on]::before{background:radial-gradient(130% 115% at 50% 50%,transparent 65%,rgba(40,50,30,.16))}
html[data-style=phosphor][data-fx-grain=on]::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:62;opacity:.07;mix-blend-mode:overlay;
 background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
html[data-style=phosphor][data-fx-beam=on] body::before{content:"";position:fixed;left:0;right:0;top:0;height:22vh;pointer-events:none;z-index:59;
 background:linear-gradient(180deg,transparent,color-mix(in srgb,var(--accent) calc(5% * min(var(--gl),1.2)),transparent) 70%,color-mix(in srgb,var(--accent) calc(9% * min(var(--gl),1.2)),transparent) 92%,transparent);
 transform:translateY(-25vh);will-change:transform;animation:nxbeam 9s linear infinite}
@keyframes nxbeam{0%{transform:translateY(-25vh)}70%,100%{transform:translateY(105vh)}}
html[data-style=phosphor][data-fx-flicker=on] body::after{animation:nxflick 7s steps(1) infinite}
html[data-style=phosphor][data-fx-flicker=on][data-fx-scan=off] body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:60;background:rgba(0,0,0,.05);will-change:opacity}
@keyframes nxflick{0%,100%{opacity:1}31%{opacity:.82}32%{opacity:1}64%{opacity:.9}65%{opacity:1}88%{opacity:.86}89%{opacity:1}}
@media (prefers-reduced-motion:reduce){html[data-style=phosphor] body::before,html[data-style=phosphor] body::after{animation:none!important}}
html[data-style=phosphor][data-fx-fringe=on] .hero .hv,html[data-style=phosphor][data-fx-fringe=on] .kbig b,html[data-style=phosphor][data-fx-fringe=on] .kclock b{
 text-shadow:-1.5px 0 rgba(255,40,90,.45),1.5px 0 rgba(40,200,255,.45),0 0 calc(18px * var(--gl)) color-mix(in srgb,currentColor 55%,transparent)}
/* scenery: cheap static backdrops that give a variant its place */
html[data-palette=aurora] body{background:
 radial-gradient(42% 34% at 24% 16%,color-mix(in srgb,#5cffc8 calc(15% * var(--gl)),transparent),transparent 72%),
 radial-gradient(36% 30% at 52% 10%,color-mix(in srgb,#4de0ff calc(10% * var(--gl)),transparent),transparent 72%),
 radial-gradient(40% 34% at 78% 18%,color-mix(in srgb,#b18cff calc(15% * var(--gl)),transparent),transparent 72%),
 radial-gradient(50% 26% at 62% 30%,color-mix(in srgb,#ff7ad9 calc(6% * var(--gl)),transparent),transparent 72%),
 linear-gradient(color-mix(in srgb,var(--fg) 2.2%,transparent) 1px,transparent 1px) 0 0/44px 44px,
 linear-gradient(90deg,color-mix(in srgb,var(--fg) 2.2%,transparent) 1px,transparent 1px) 0 0/44px 44px,var(--bg)}
html[data-palette=outrun] body{background:
 radial-gradient(38% 30% at 50% 102%,color-mix(in srgb,#ff8a3d calc(22% * var(--gl)),transparent),transparent 70%),
 radial-gradient(70% 40% at 50% 104%,color-mix(in srgb,#ff3dac calc(12% * var(--gl)),transparent),transparent 72%),
 linear-gradient(transparent 62%,color-mix(in srgb,#ff3dac 4%,transparent) 62.2%,transparent 62.4%) 0 0/100% 100%,
 linear-gradient(color-mix(in srgb,#c77dff 5%,transparent) 1px,transparent 1px) 0 0/56px 32px,
 linear-gradient(90deg,color-mix(in srgb,#c77dff 5%,transparent) 1px,transparent 1px) 0 0/56px 32px,var(--bg)}
html[data-palette=outrun] header.top{border-image:linear-gradient(90deg,#ff3dac,#ff8a3d 50%,#ffd23f) 1;border-bottom-width:1px;border-bottom-style:solid}
html[data-palette=lcd][data-theme=light] body{background:
 linear-gradient(color-mix(in srgb,var(--fg) 5%,transparent) 1px,transparent 1px) 0 0/4px 4px,var(--bg)}
html[data-palette=lcd] .panel,html[data-palette=lcd] .kslot,html[data-palette=lcd] .knode{box-shadow:inset 0 2px 6px rgba(0,0,0,calc(.14 + .2 * var(--gl))),0 1px 0 rgba(255,255,255,.18)}
html[data-palette=lcd][data-theme=light] [data-ghost]::before{color:color-mix(in srgb,currentColor 11%,transparent)}
html[data-palette=aurora] .mark{filter:hue-rotate(-40deg) saturate(1.2)} html[data-palette=outrun] .mark{filter:hue-rotate(160deg) saturate(1.6)}
html[data-palette=lcd] .mark{filter:grayscale(1) contrast(1.2)}
/* the eye takes the variant's colour (Red Ops above all: one cyan light spoils night vision) */
html[data-palette=p1green] .mark{filter:hue-rotate(-50deg) saturate(1.3)} html[data-palette=p3amber] .mark{filter:hue-rotate(-150deg) saturate(1.6)}
html[data-palette=nixie] .mark{filter:hue-rotate(-170deg) saturate(1.8)} html[data-palette=synthwave] .mark{filter:hue-rotate(115deg) saturate(1.4)}
html[data-palette=redops] .mark{filter:grayscale(1) sepia(1) hue-rotate(-50deg) saturate(6) brightness(.85)}
html[data-style=phosphor] button.act:hover{box-shadow:0 0 calc(14px * var(--gl)) color-mix(in srgb,var(--accent) 35%,transparent)}
</style></head><body>
<div class=app>
<header class=top>
  <a class=brand href="/about" title="About Noctyx"><span class=mark></span><span class=wordmark>Noct<b>yx</b></span></a>
  <nav class=pages id=nav aria-label="Pages">
   <div class=menu><button type=button class=menu-title aria-haspopup=true aria-expanded=false>Monitor</button>
    <div class="menu-pop hide" role=menu><a role=menuitem href="/" data-v=main>Dashboard<kbd>1</kbd></a><a role=menuitem href="/cluster" data-v=cluster>Cluster<kbd>2</kbd></a><a role=menuitem href="/heal" data-v=heal>Self-repair</a></div></div>
   <div class=menu><button type=button class=menu-title aria-haspopup=true aria-expanded=false>Deploy</button>
    <div class="menu-pop hide" role=menu><a role=menuitem href="/recipes" data-v=recipes>Recipes<kbd>3</kbd></a></div></div>
   <div class=menu><button type=button class=menu-title aria-haspopup=true aria-expanded=false>Configure</button>
    <div class="menu-pop hide" role=menu><a role=menuitem href="/config#sec-backups" data-v=config>Backups<kbd>4</kbd></a><a role=menuitem href="/config#sec-retention">Retention</a><a role=menuitem href="/config#sec-mirror">Model mirror</a><a role=menuitem href="/config#sec-assistant">Assistant LLM</a><a role=menuitem href="/config#sec-heal">Self-repair</a><a role=menuitem href="/config#sec-checkpoints">Checkpoints</a></div></div>
   <a class="menu-title direct" href="/kiosk" title="The wall display">Kiosk<kbd>5</kbd></a>
  </nav>
  <span class=pagename id=hpage></span>
  <span class=hstat id=hsrv></span>
  <span class=hstat id=hclock></span>
  <span class=hpill id=hauth></span>
  <span class="hpill go" id=hrpo onclick="goTo({panel:'schedule'})" title="open Schedule"></span>
  <span class="hpill go" id=halert onclick="goTo({panel:'alerts'})" title="open Alerts"></span>
  <span class=hspacer></span>
  <span class=huser id=huser></span>
  <button type=button class=hbtn id=hask title="Ask Noctyx: questions about the cluster, answered from its own data" onclick="askToggle()">Ask</button>
  <button type=button class="hbtn gear" id=hgear title="Display settings: theme, units, layout" aria-label="Display settings" onclick="setOpen()"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="currentColor"><path fill-rule="evenodd" d="M13.9 2c.5 0 .9.4 1 .9l.3 1.8c.5.2 1 .4 1.4.7l1.7-.7c.5-.2 1 0 1.3.4l1.4 2.5c.3.5.2 1-.2 1.3l-1.5 1.2v1.6l1.5 1.2c.4.3.5.8.2 1.3l-1.4 2.5c-.3.4-.8.6-1.3.4l-1.7-.7c-.4.3-.9.5-1.4.7l-.3 1.8c-.1.5-.5.9-1 .9h-2.9c-.5 0-.9-.4-1-.9l-.3-1.8c-.5-.2-1-.4-1.4-.7l-1.7.7c-.5.2-1 0-1.3-.4l-1.4-2.5c-.3-.5-.2-1 .2-1.3l1.5-1.2v-1.6L3.6 9c-.4-.3-.5-.8-.2-1.3l1.4-2.5c.3-.4.8-.6 1.3-.4l1.7.7c.4-.3.9-.5 1.4-.7l.3-1.8c.1-.5.5-.9 1-.9zM12 8.4a3.6 3.6 0 1 0 0 7.2 3.6 3.6 0 0 0 0-7.2z"/></svg></button>
</header>
<aside class="askwrap hide" id=askwrap aria-label="Ask Noctyx">
 <div class=askhead><h2>Ask Noctyx</h2><span class=model id=askmodel></span><button type=button class=hbtn onclick="askPrompt()" title="What Ask is told before your question: its system prompt">Prompt</button><button type=button class=hbtn onclick="askNew()" title="Start a new conversation">New</button><button type=button class=hbtn onclick="askClose()" aria-label="Close">✕</button></div>
 <div class=asklog id=asklog aria-live=polite></div>
 <form class=askform id=askform><textarea id=askq rows=1 placeholder="Ask about the cluster…" maxlength=4000></textarea><button class=hbtn type=submit>Send</button></form>
 <div class=asknote id=asknote>It looks things up but changes nothing: what it suggests, you do.</div>
</aside>
<div class="setwrap hide" id=setwrap role=dialog aria-modal=true aria-labelledby=setTitle><div class=setscrim onclick="setClose()"></div><section class=setpanel id=setpanel></section></div>
<div id=desk></div>
</div>
<script>
/* Snapping grid dashboard (operator, 2026-09-26: panels must never overlap).
   Each panel occupies a column-span on a 12-col grid; dragging reorders the
   grid, resizing changes the span (snap) or the height (free px). NO
   meta-refresh — each panel polls its own endpoint on its own clock and
   repaints only its own DOM node. */
const VIEW=document.body.dataset.view||'main';
const LS='cm_layout_v4'+(VIEW=='main'?'':'_'+VIEW+'_v2')   /* bump when the panel set changes: a layout saved by an older page must not be applied */, LS_SEL='cm_sel_v1', LS_TOK='cm_token_v1';
let SEL=localStorage.getItem(LS_SEL)||null, OVR=null;
const LS_TAB='cm_dtab_v1',DTABS=['overview','llm','network','storage','gpu','health','backups'];
const BUILD='__BUILD__';                              /* server build stamp; see buildCheck() */
let DTAB=DTABS.includes(localStorage.getItem(LS_TAB))?localStorage.getItem(LS_TAB):'overview';
/* snapshot browser state: host, snapshot id, path; loaded on demand (int:0) */
const BR={host:null,snap:null,path:'/',snaps:[],data:null,find:null,busy:false};
async function browseTo(host,snap,path){
 BR.host=host;BR.snap=snap;BR.path=path||'/';BR.find=null;BR.busy=true;BR.data=null;
 repaint('browse',BR);
 try{
  if(!BR.snaps.length||BR.snaps[0]._host!==host){const r=await fetchJSON('/api/v1/snapshots/'+encodeURIComponent(host));
   BR.snaps=(r.snapshots||[]).map(x=>Object.assign({_host:host},x))}
  if(!BR.snap&&BR.snaps.length)BR.snap=BR.snaps[0].short_id;
  BR.data=await fetchJSON(`/api/v1/browse/${encodeURIComponent(host)}/${BR.snap}?path=${encodeURIComponent(BR.path)}`);
 }catch(e){BR.data={error:String(e.message||e)}}
 BR.busy=false;repaint('browse',BR);
 const el=$('p-browse');if(el&&el.classList.contains('collapsed'))toggleP('browse')}
async function browseFind(){const q=($('bq').value||'').trim();if(!q||!BR.host)return;
 BR.busy=true;BR.find={q};repaint('browse',BR);
 try{BR.find=Object.assign({q},await fetchJSON(`/api/v1/find/${encodeURIComponent(BR.host)}?q=${encodeURIComponent(q)}`))}
 catch(e){BR.find={q,error:String(e.message||e)}}
 BR.busy=false;repaint('browse',BR)}
function browseRestore(path,to){
 const what=to=='host'?`a fresh directory under __NODE_LIB__/restore/ on ${BR.host}`:`the staging area on __NAS__ (backups/restores/${BR.host}/<time>/)`;
 if(!confirm(`Restore ${path} from snapshot ${BR.snap} into ${what}?\nNothing on the live system is overwritten.`))return;
 act('restore',{host:BR.host,snapshot:BR.snap,path,to})}
/* ── navigation. One rule: a coloured "look at me" is a link to the panel where you
   act on it and watch the result. goTo() expands/scrolls/flashes the panel, selects
   the host and tab, and remembers the host so that panel highlights its rows.
   URL form (#host=..&panel=..&tab=..) works from /cluster and from a pasted link;
   a bare #host still selects a host (old links). ── */
let FOCUS={};
function hashOf(t){const q=[];for(const k of ['host','panel','tab'])if(t&&t[k])q.push(k+'='+encodeURIComponent(t[k]));return q.length?'#'+q.join('&'):''}
function parseHash(h){h=(h||'').replace(/^#/,'');if(!h)return {};if(!h.includes('='))return {host:decodeURIComponent(h)};
 const t={};for(const kv of h.split('&')){const i=kv.indexOf('=');if(i>0)t[kv.slice(0,i)]=decodeURIComponent(kv.slice(i+1))}return t}
function goTo(t){t=t||{};
 if(VIEW!='main'){location.href='/'+hashOf(t);return}
 if(t.tab&&DTABS.includes(t.tab)&&t.tab!=DTAB){DTAB=t.tab;try{localStorage.setItem(LS_TAB,t.tab)}catch(e){}}
 if(t.host&&t.host!=SEL)selectHost(t.host);else if(t.host&&t.tab)setTab(DTAB);
 const id=t.panel||(t.host?'detail':null);if(!id)return;
 const el=$('p-'+id),p=PANELS.find(x=>x.id==id);if(!el||!p)return;
 FOCUS={panel:id,host:t.host||null};
 if(el.classList.contains('collapsed'))toggleP(id);
 if(id!='detail'&&p._data!==undefined)repaint(id,p._data);          /* row highlight for the host */
 el.scrollIntoView({behavior:'smooth',block:'start'});
 el.classList.remove('flash');void el.offsetWidth;el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),2600);
 const row=el.querySelector('tr.hl');if(row)setTimeout(()=>row.scrollIntoView({block:'nearest'}),400);
 history.replaceState(null,'',location.pathname+hashOf({host:SEL,panel:id,tab:id=='detail'?DTAB:null}))}
const goAttr=t=>`class=go onclick="event.stopPropagation();goTo(${esc(JSON.stringify(t))})"`;
const goA=(t,cls,txt,title)=>`<a class="tag ${cls} go" href="/${hashOf(t)}" onclick="event.stopPropagation();goTo(${esc(JSON.stringify(t))});return false" title="${esc(title||'open')}">${txt}</a>`;
const hlRow=(panel,host)=>FOCUS.panel==panel&&FOCUS.host&&FOCUS.host==host?' hl':'';
/* where each alert is acted on */
function alertTarget(a){const n=a.name||'',h=a.host||null;
 if(/^Backup(Never|Stale|Late)$/.test(n))return {host:h,panel:'detail',tab:'backups',what:'Machine Report › backups'};
 if(n=='BackupJobFailed'||n=='JobDied')return {host:h,panel:'runs',what:'Runs'};
 if(n=='TickTimerInactive')return {panel:'schedule',what:'Schedule'};
 if(/^Nas|^RepoUnreadable/.test(n))return {panel:'storage',what:'Storage'};
 if(n=='RestEndpointDown'||n=='NoctyxMetricsDown')return {panel:'actions',what:'Console'};
 if(/^Host(HygieneWarning|SystemdUnitFailed|RootFilesystemFull)$/.test(n))return {host:h,panel:'hygiene',what:'Hygiene'};
 if(n=='NodeExporterDown')return {host:h,panel:'detail',tab:'health',what:'Machine Report › health'};
 if(/^LLM|Tensorfold|Mtplx/.test(n))return {host:h,panel:'detail',tab:'llm',what:'Machine Report › llm'};
 return h?{host:h,panel:'detail',tab:'overview',what:'Machine Report'}:{panel:'alerts',what:''}}
function setTab(t){DTAB=t;try{localStorage.setItem(LS_TAB,t)}catch(e){}
 const p=PANELS.find(x=>x.id=='detail');if(p._data)p._data._m={loading:true};repaint('detail',p._data);tick(p)}
const $=id=>document.getElementById(id);
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* temperature unit: a display setting (Display settings › Units), °F unless changed. Data and alert
   thresholds stay in °C; only what is DISPLAYED converts. */
let TU=NXT.eff().tunit=='C'?'C':'F';
const tval=c=>TU=='F'?c*9/5+32:c;
const fmtT=(c,short)=>c==null||isNaN(c)?'—':Math.round(tval(+c))+'°'+(short?'':TU);
function setTU(u){TU=u;NXT.save({tunit:u});pushAccount();setRender();
 PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
/* the subsystem a meter shows (data-k): themes may colour by it (Phosphor does); others ignore it */
function kOf(label){const l=String(label||'').toLowerCase().replace(/<[^>]+>/g,'');
 return /temp/.test(l)?'temp':/gpu/.test(l)?'gpu':/cpu|load/.test(l)?'cpu':/mem/.test(l)?'mem':/disk|root|stor/.test(l)?'disk':
  /net|fabric/.test(l)?'net':/power|watt|energy/.test(l)?'power':/tok|llm|request|generat|model/.test(l)?'llm':/health|backup/.test(l)?'ok':''}
/* LCD numerals draw an unlit "8" behind every digit, as a real segment display does */
const ghost=v=>String(v??'').replace(/[0-9]/g,'8').replace(/[^8:.\s/%-]/g,' ');
function bar(pct,neutral,k){const p=clamp(parseInt(pct)||0,0,100);
 return `<div class="bar ${neutral?'n':p>90?'b':p>75?'w':''}" data-k="${k||''}" style="--p:${Math.max(p,1)}"><i style="width:${p}%"></i></div>`}
const ageD=t=>t?(Date.now()-new Date(t))/864e5:null;
/* ALL times shown in UTC (the header clock is UTC): clients run mixed zones,
   and a bare 'HH:MM' that is sometimes Chicago and sometimes UTC is a lie */
function toEpoch(t){if(t==null||t==='')return null;if(typeof t=='number')return t;
 const s=String(t);return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s)?s:s+'Z').getTime()/1000}
function fmtTs(t){const e=toEpoch(t);if(e==null||isNaN(e))return '—';
 return new Date(e*1000).toISOString().slice(5,16).replace('T',' ')+'Z'}
function agoS(t){const e=toEpoch(t);if(!e)return 'never';const s=Date.now()/1000-e;
 return s<0?'in '+fmtDur(-s):fmtDur(s)+' ago'}
function inS(t){const e=toEpoch(t);if(!e)return '—';const s=e-Date.now()/1000;
 return s<=60?'now':'in '+fmtDur(s)}
const HCLS={ok:'ok',late:'warn',stale:'bad',never:'bad'};
const pct=v=>v==null?'—':Math.round(v)+'%';
function fmtBits(b){if(b==null)return '—';return b>=1e9?(b/1e9).toFixed(b>=1e10?0:1)+' Gb/s':b>=1e6?(b/1e6).toFixed(b>=1e8?0:1)+' Mb/s':(b/1e3).toFixed(0)+' kb/s'}
function fmtK(v){if(v==null)return '—';v=+v;return v>=1e9?(v/1e9).toFixed(2)+' G':v>=1e6?(v/1e6).toFixed(v>=1e7?0:1)+' M':v>=1e3?(v/1e3).toFixed(v>=1e5?0:1)+' k':String(Math.round(v))}
function fmtBytes(b){if(b==null)return '—';const u=['B','KB','MB','GB','TB','PB'];let i=0;
 while(Math.abs(b)>=1000&&i<u.length-1){b/=1000;i++}return (b>=100||i==0?b.toFixed(0):b.toFixed(1))+' '+u[i]}
/* ── charts ──────────────────────────────────────────────────────────
   spark(): tiny inline trend. chart(): full component — hairline grid with tick
   labels, 2px line, 10% wash (≤2 series), ringed end-dot, legend with line
   keys (identity on the key, text in ink), crosshair + one tooltip for every
   series on hover. Series: [{name, pts:[[t,v]…], cls}] */
/* curve(): an SVG path through [x,y] points as a monotone cubic (Fritsch–Carlson, as d3's
   curveMonotoneX): smooth bends, but it never overshoots a sample — a 0→100% step stays inside
   0..100 and a flat run stays flat, so the curve can't invent a peak or a dip the data lacks. */
function curve(P){const n=P.length;if(!n)return '';
 const f=v=>v.toFixed(1);let d=`M${f(P[0][0])} ${f(P[0][1])}`;
 if(n<3)return n<2?d:d+` L${f(P[1][0])} ${f(P[1][1])}`;
 const h=[],k=[];for(let i=0;i<n-1;i++){h[i]=(P[i+1][0]-P[i][0])||1e-6;k[i]=(P[i+1][1]-P[i][1])/h[i]}
 const m=[k[0]];
 for(let i=1;i<n-1;i++){const p=(k[i-1]*h[i]+k[i]*h[i-1])/(h[i-1]+h[i]);
  m[i]=k[i-1]*k[i]<=0?0:(Math.sign(k[i-1])+Math.sign(k[i]))*Math.min(Math.abs(k[i-1]),Math.abs(k[i]),.5*Math.abs(p))}
 m[n-1]=k[n-2];
 for(let i=0;i<n-1;i++){const t=h[i]/3,[x0,y0]=P[i],[x1,y1]=P[i+1];
  d+=` C${f(x0+t)} ${f(y0+t*m[i])} ${f(x1-t)} ${f(y1-t*m[i+1])} ${f(x1)} ${f(y1)}`}
 return d}
function _lines(series,W,H,max){
 const ts=series.flatMap(s=>(s.pts||[]).map(p=>p[0]));if(!ts.length)return '';
 // end-stamp the path: a sliding window of the same length maps to the SAME point string
 // (x comes from the normalized span), so without this morph() compares identical attributes
 // and never repaints the trace — it freezes on screen (user, 2026-10-22: graphs not updating)
 const wk=`${Math.min(...ts)}-${Math.max(...ts)}`;
 const t0=Math.min(...ts),t1=Math.max(...ts)||t0+1;
 return series.map(s=>{const d=(s.pts||[]).filter(p=>p[1]!=null).map(p=>
   [(p[0]-t0)/((t1-t0)||1)*W,H-2-Math.min(p[1],max)/(max||1)*(H-4)]);
 return d.length?`<path class="${s.cls||''}" data-w="${wk}" d="${curve(d)}"/>`:''}).join('')}
function spark(series,o={}){const v=series.flatMap(s=>(s.pts||[]).map(p=>p[1])).filter(x=>x!=null);
 if(v.length<2)return '<svg class=spk></svg>';
 const max=o.max??(Math.max(...v)*1.15||1);
 return `<svg class=spk viewBox="0 0 70 18" preserveAspectRatio="none">${_lines(series,70,18,max)}</svg>`}
const CH={};let CHN=0;
function nice(v){if(!(v>0))return 1;const p=Math.pow(10,Math.floor(Math.log10(v))),m=v/p;return (m<=1?1:m<=2?2:m<=2.5?2.5:m<=5?5:10)*p}
const CG={W:300,H:84,X0:30,X1:294,Y0:8,Y1:66},CGC={W:300,H:40,X0:2,X1:298,Y0:3,Y1:37};   /* CGC: compact rows (/cluster) */
function _geo(series,o){const ts=series.flatMap(s=>(s.pts||[]).map(p=>p[0]));const t0=ts.length?Math.min(...ts):0,t1=ts.length?Math.max(...ts):1;
 const vs=series.flatMap(s=>(s.pts||[]).map(p=>p[1])).filter(x=>x!=null);const peak=vs.length?Math.max(...vs):0;
 return {t0,t1:t1>t0?t1:t0+1,peak,max:o.max??nice(peak*1.15||1),cg:o.compact?CGC:CG}}
const _px=(t,g)=>g.cg.X0+(t-g.t0)/(g.t1-g.t0)*(g.cg.X1-g.cg.X0),_py=(v,g)=>g.cg.Y1-Math.min(v,g.max)/g.max*(g.cg.Y1-g.cg.Y0);
function chart(title,series,fmt,o={}){
 series=(series||[]).map(s=>({...s,pts:(s.pts||[]).filter(p=>p&&p[1]!=null)}));
 const id='ch'+(++CHN),g=_geo(series,o),G=g.cg;CH[id]={series,fmt,g,title};
 const has=series.some(s=>s.pts.length);
 const ticks=[0,.5,1].map(f=>({y:G.Y1-f*(G.Y1-G.Y0),v:g.max*f}));
 const tk=v=>{const f=String(fmt(v)).replace(/<[^>]+>/g,'');const m=f.match(/^[↑↓]?\s*(-?[\d.,]+\s?[kMGT]?)/);return m?m[1].replace(/\s+$/,''):f.slice(0,6)};
 const grid=ticks.map(t=>`<line class="gl" x1="${G.X0}" x2="${G.X1}" y1="${t.y}" y2="${t.y}"/>${o.compact?'':`<text class="tk" x="${G.X0-4}" y="${t.y+2.5}">${esc(tk(t.v))}</text>`}`).join('');
 const xy=s=>curve(s.pts.map(p=>[_px(p[0],g),_py(p[1],g)]));
 const wash=series.length<=2?series.map(s=>{if(s.pts.length<2)return '';const d=xy(s);
   return `<path class="wash ${s.cls||''}" d="${d} L${_px(s.pts[s.pts.length-1][0],g).toFixed(1)} ${G.Y1} L${_px(s.pts[0][0],g).toFixed(1)} ${G.Y1} Z"/>`}).join(''):'';
 const lines=series.map(s=>s.pts.length?`<path class="ln ${s.cls||''}" d="${xy(s)}"/>`:'').join('');
 const dots=o.compact?'':series.map(s=>{const p=s.pts[s.pts.length-1];return p?`<circle class="dot ${s.cls||''}" cx="${_px(p[0],g).toFixed(1)}" cy="${_py(p[1],g).toFixed(1)}" r="3.5"/>`:''}).join('');
 const lastv=s=>{const p=s.pts[s.pts.length-1];return p?esc(fmt(p[1])):'—'};
 const legend=series.length>1?series.map(s=>`<span class=lg><i class="key ${s.cls||''}"></i>${esc(s.name||'')} <b>${lastv(s)}</b></span>`).join('')
  :`<span class=lg><b>${series.length?lastv(series[0]):'—'}</b></span>`;
 return `<div class=chart data-ch=${id}><div class=ch-h><span class=ch-t>${esc(title)}</span>${legend}<span class=ch-max>peak ${esc(fmt(g.peak))}</span></div>
  <svg viewBox="0 0 ${G.W} ${G.H}"${o.compact?' preserveAspectRatio="none" class=cmp':''}>${grid}${wash}${lines}${dots}<line class="xh" x1="0" x2="0" y1="${G.Y0}" y2="${G.Y1}" style="display:none"/>${series.map((s,i)=>`<circle class="hd" data-i="${i}" r="3" style="display:none"/>`).join('')}${o.compact?'':`<text class="ax" x="${G.X0}" y="79">−${esc(o.span||'1h')}</text><text class="ax r" x="${G.X1}" y="79">now</text>`}</svg>
  ${has?'':'<div class=ch-empty>no data yet</div>'}</div>`}
/* hover layer: crosshair snaps to the nearest sample, one tooltip lists every series */
let TIP=null;
function hideTip(){if(TIP)TIP.style.display='none';document.querySelectorAll('.chart svg .xh,.chart svg .hd').forEach(l=>l.style.display='none')}
function initTip(){
 TIP=document.createElement('div');TIP.className='tip';TIP.style.display='none';document.body.appendChild(TIP);
 document.addEventListener('pointermove',e=>{
  const svg=e.target&&e.target.closest?e.target.closest('.chart svg'):null;if(!svg){hideTip();return}
  const c=CH[svg.parentElement.dataset.ch];if(!c||!c.series.some(s=>s.pts.length)){hideTip();return}
  const G=c.g.cg,r=svg.getBoundingClientRect(),fx=(e.clientX-r.left)/r.width*G.W,f=clamp((fx-G.X0)/(G.X1-G.X0),0,1),t=c.g.t0+f*(c.g.t1-c.g.t0);
  const rows=c.series.map((s,i)=>{if(!s.pts.length)return null;let b=s.pts[0];for(const p of s.pts)if(Math.abs(p[0]-t)<Math.abs(b[0]-t))b=p;return {s,i,p:b}}).filter(Boolean);
  const tx=rows[0].p[0],x=_px(tx,c.g);
  const xh=svg.querySelector('.xh');xh.setAttribute('x1',x);xh.setAttribute('x2',x);xh.style.display='';
  svg.querySelectorAll('.hd').forEach(d=>d.style.display='none');
  rows.forEach(({i,p})=>{const d=svg.querySelector(`.hd[data-i="${i}"]`);if(d){d.setAttribute('cx',_px(p[0],c.g));d.setAttribute('cy',_py(p[1],c.g));d.style.display=''}});
  TIP.replaceChildren();const h=document.createElement('div');h.className='tip-t';h.textContent=fmtTs(tx);TIP.appendChild(h);
  for(const {s,p} of rows){const d=document.createElement('div');d.className='tip-r';const k=document.createElement('i');k.className='key '+(s.cls||'');
   const v=document.createElement('b');v.textContent=c.fmt(p[1]);const n=document.createElement('span');n.textContent=s.name||c.title;d.append(k,v,n);TIP.appendChild(d)}
  TIP.style.display='';const tw=TIP.offsetWidth,th=TIP.offsetHeight;let left=e.clientX+14,top=e.clientY+14;
  if(left+tw>innerWidth-8)left=e.clientX-tw-14;if(top+th>innerHeight-8)top=e.clientY-th-10;TIP.style.left=left+'px';TIP.style.top=top+'px'});
 document.addEventListener('pointerleave',hideTip);document.addEventListener('scroll',hideTip,true)}
/* stable colour per entity (a model keeps its slot when others come and go) */
const LS_SLOT='cm_slots_v1';let SLOTS=null;
function slotOf(name){if(!SLOTS){try{SLOTS=JSON.parse(localStorage.getItem(LS_SLOT))||{}}catch(e){SLOTS={}}}
 if(SLOTS[name]==null){const used=new Set(Object.values(SLOTS));let i=0;while(used.has(i)&&i<8)i++;SLOTS[name]=i%8;try{localStorage.setItem(LS_SLOT,JSON.stringify(SLOTS))}catch(e){}}
 return 'c'+SLOTS[name]}
function ring(pct,label,inner,cls,labHtml){const r=21,c=2*Math.PI*r,p=clamp(+pct||0,0,100);
 return `<div class=ring data-k="${kOf(label)}"><svg viewBox="0 0 50 50"><circle class="track" cx="25" cy="25" r="${r}"/><circle class="fill ${cls||''}" cx="25" cy="25" r="${r}" stroke-dasharray="${(p/100*c).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 25 25)"/></svg><div class=rv>${inner}</div><div class=rl>${labHtml??esc(label)}</div></div>`}
function statTile(label,value,unit,sub,pts,o={}){return `<div class=stat data-k="${o.k||kOf(label)}"><div class=l>${esc(label)}</div><div class=v>${value}<span class=u>${esc(unit||'')}</span></div>${pts&&pts.length>2?spark([{pts,cls:'dim'}],o):''}<div class=s>${sub||''}</div></div>`}
function ago(ts){if(!ts)return 'never';const m=Date.now()/1000-ts;
 if(m<90)return Math.round(m)+'s ago';if(m<5400)return Math.round(m/60)+'m ago';return Math.round(m/3600)+'h ago'}

const PANELS=[
 {id:'cluster', title:'Cluster',        sp:12,h:330,int:15,render:rCluster,api:()=>fetchJSON('/api/v1/metrics')},
 {id:'fleet',   title:'Fleet',          sp:5,h:430,int:15,render:rFleet,  api:()=>Promise.all([fetchJSON('/api/v1/overview'),
   fetchJSON('/api/v1/metrics').catch(()=>null)]).then(([o,m])=>{o.metrics=m;return o})},
 {id:'detail',  title:'Machine Report', sp:4,h:430,int:15,render:rDetail, api:()=>SEL?Promise.all([
   fetchJSON('/api/v1/facts/'+encodeURIComponent(SEL)+'?age=60'),
   DTAB=='backups'?Promise.resolve(null):fetchJSON('/api/v1/metrics/'+encodeURIComponent(SEL)+'?tab='+DTAB).catch(e=>({error:String(e.message||e)}))
  ]).then(([f,m])=>{if(f)f._m=m;return f}):Promise.resolve(null)},
 {id:'storage', title:'Storage Tiers',  sp:3,h:300,int:30,render:rStorage,api:()=>Promise.all([fetchJSON('/api/v1/capacity'),fetchJSON('/api/v1/growth').catch(()=>null)]).then(([t,g])=>{t._growth=g&&g.growth;return t})},
 {id:'alerts',  title:'Alerts',         sp:5,h:220,int:20,render:rAlerts, api:()=>fetchJSON('/api/v1/alerts')},
 {id:'brief',   title:'Morning Brief',  sp:5,h:220,int:600,render:rBrief,  api:()=>fetchJSON('/api/v1/digest')},
 {id:'hygiene', title:'Hygiene',        sp:7,h:260,int:60,render:rHygiene,api:()=>fetchJSON('/api/v1/hygiene')},
 {id:'browse',  title:'Snapshot Browser',sp:7,h:380,int:0, render:rBrowse, api:null},
 {id:'schedule',title:'Schedule',       sp:7,h:330,int:30,render:rSched,  api:()=>fetchJSON('/api/v1/schedule')},
 {id:'runs',    title:'Runs',           sp:5,h:330,int:15,render:rRuns,   api:()=>fetchJSON('/api/v1/runs')},
 {id:'jobs',    title:'Jobs / Audit',   sp:3,h:340,int:20,render:rJobs,   api:()=>fetchJSON('/api/v1/audit?n=14')},
 {id:'network', title:'Network',        sp:3,h:250,int:60,render:rNet,    api:()=>fetchJSON('/api/v1/probes')},
 {id:'actions', title:'Console',        sp:5,h:340,int:0, render:rActions,api:null},
];

if(VIEW=='config'){
 PANELS.splice(0,PANELS.length,
  {id:'config',title:'Backup Config',sp:12,h:0,int:300,render:rConfig,api:()=>fetchJSON('/api/v1/policy')});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='recipes'){
 PANELS.splice(0,PANELS.length,
  {id:'recipes',title:'Recipes',sp:12,h:0,int:20,render:rRecipes,api:()=>fetchJSON('/api/v1/recipes')});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='heal'){
 PANELS.splice(0,PANELS.length,{id:'heal',title:'Self-repair',sp:12,h:0,int:20,render:rHeal,api:srApi});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='about'){
 PANELS.splice(0,PANELS.length,{id:'about',title:'About',sp:12,h:0,int:3600,render:rAbout,api:()=>fetchJSON('/api/v1/about')});
 document.getElementById('desk').classList.add('stack');
}
/* /kiosk: the wall display — no chrome, big type, every source refreshed together every 10 s */
if(VIEW=='kiosk'){
 PANELS.splice(0,PANELS.length,{id:'kiosk',title:'Kiosk',sp:12,h:0,int:10,render:rKiosk,
  api:()=>Promise.all([fetchJSON('/api/v1/metrics'),fetchJSON('/api/v1/recipes').catch(()=>({})),fetchJSON('/api/v1/overview').catch(()=>({})),fetchJSON('/api/v1/alerts').catch(()=>({}))])
   .then(([m,r,o,a])=>({m,r,o,a}))});
 document.body.classList.add('kiosk');document.getElementById('desk').classList.add('stack');
}
/* rotation mode: the pager is armed after buildPanels() creates #b-kiosk (see kioskArm) */
function kioskArm(){
 if(VIEW!='kiosk'||kioskArm.on)return;
 const k=kioskCfg();
 /* the wall shows one display and stays on it (operator, 2026-10-22): rotation alternates
    views only when this browser asks for it explicitly with ?rotation=1 in the URL — a
    site setting alone can never make the wall page through screens */
 if(!new URLSearchParams(location.search).has('rotation')||new URLSearchParams(location.search).has('page')||!k.rotation||k.fit=='scroll')return;
 kioskArm.on=true;
 setInterval(()=>{if(kioskPages()<2)return;kioskPg=(kioskPg+1)%kioskPages();kioskRepaint();},20000);}
/* /cluster: only the systems. Both panels read the same fleet metrics call. */
if(VIEW=='cluster'){
 PANELS.splice(0,PANELS.length,
  {id:'cluster',title:'Cluster',sp:12,h:0,int:15,render:rControl,api:()=>fetchJSON('/api/v1/metrics')});
 document.getElementById('desk').classList.add('stack');
}
/* ── navigation: menus in the header, the page name, 1–4 to jump ── */
const PAGES={main:['/','Dashboard'],cluster:['/cluster','Cluster'],recipes:['/recipes','Recipes'],config:['/config','Configuration'],heal:['/heal','Self-repair'],about:['/about','About'],kiosk:['/kiosk','Kiosk']};
(function nav(){
 const name=(PAGES[VIEW]||PAGES.main)[1];const pn=$('hpage');if(pn)pn.textContent=name;document.title=`Noctyx · ${name}`;
 document.querySelectorAll('#nav .menu').forEach(m=>{
  if(m.querySelector(`a[data-v="${VIEW}"]`))m.classList.add('cur');
  m.querySelectorAll('a[data-v]').forEach(a=>{if(a.dataset.v==VIEW)a.classList.add('cur')});
  const b=m.querySelector('.menu-title'),pop=m.querySelector('.menu-pop');
  b.addEventListener('click',e=>{e.stopPropagation();const open=!m.classList.contains('open');closeMenus();if(open){m.classList.add('open');pop.classList.remove('hide');b.setAttribute('aria-expanded','true')}});
  m.addEventListener('mouseenter',()=>{if(document.querySelector('#nav .menu.open')&&!m.classList.contains('open'))b.click()});
 });
 document.addEventListener('click',closeMenus);
 document.addEventListener('keydown',e=>{
  if(e.key=='Escape'){closeMenus();setClose();if(document.activeElement&&document.activeElement.id=='askq')askClose();return}
  if(e.ctrlKey||e.metaKey||e.altKey||/INPUT|TEXTAREA|SELECT/.test((document.activeElement||{}).tagName||''))return;
  const go={'1':'/','2':'/cluster','3':'/recipes','4':'/config','5':'/kiosk'}[e.key];if(go&&location.pathname!=go)location.href=go});
 if(VIEW=='config'&&location.hash)setTimeout(()=>{const el=document.getElementById(location.hash.slice(1));if(el)el.scrollIntoView({behavior:'smooth',block:'start'})},900);
})();
function closeMenus(){document.querySelectorAll('#nav .menu.open').forEach(m=>{m.classList.remove('open');m.querySelector('.menu-pop').classList.add('hide');m.querySelector('.menu-title').setAttribute('aria-expanded','false')})}

/* ── display settings (the gear): appearance, units, layout. This browser's, over the site's
   defaults (site config "console", saved by an operator with "save as site default"). ── */
var SITESET=null,ME=null;   // var: fetchJSON may run before this line, and a let would throw there
const LS_WHERE='nx_settings_where';
const whereSaved=()=>{try{return localStorage.getItem(LS_WHERE)||'browser'}catch(e){return 'browser'}};
async function loadMe(){try{ME=await fetchJSON('/api/v1/me')}catch(e){ME=null}
 const c=$('huser');if(c&&ME){
  c.innerHTML=ME.user?`<b>${esc(ME.user)}</b><span class=role>${esc(ME.role)}</span><button class=lnk onclick="signOut()">sign out</button>`
   :ME.mode=='open'?`<span class="hpill" title="anyone on this network may look and act; sign-in is optional">open access</span>${ME.accounts?'<a class=lnk href="/login?next='+encodeURIComponent(location.pathname)+'">sign in</a>':''}`:''}
 if(ME&&ME.user&&whereSaved()=='account'){try{const s=await fetchJSON('/api/v1/settings/mine');
   if(s.settings&&Object.keys(s.settings).length){localStorage.setItem('nx_settings_v1',JSON.stringify(s.settings));NXT.apply();
    const t=NXT.eff().tunit=='C'?'C':'F';if(t!=TU){TU=t;PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}}}catch(e){}}
 setRender()}
async function signOut(){await fetch('/api/v1/logout',{method:'POST'});location.href=ME&&ME.mode=='accounts'?'/login':location.pathname}
async function pushAccount(){if(!(ME&&ME.user&&whereSaved()=='account'))return;
 const r=await fetch('/api/v1/settings/mine',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings:NXT.local()})});
 const m=$('setmsg');if(m&&!r.ok)m.textContent='✗ could not save to your account: '+((await r.json().catch(()=>({}))).error||r.status)}
function setWhere(w){try{localStorage.setItem(LS_WHERE,w)}catch(e){};if(w=='account')pushAccount();setRender()}
async function loadSiteSettings(){try{SITESET=await fetchJSON('/api/v1/console-settings');NXT.setSite(SITESET.settings||{});
 const t=NXT.eff().tunit=='C'?'C':'F';if(t!=TU){TU=t;PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}}catch(e){}
 setRender()}
function setOpen(){$('setwrap').classList.remove('hide');setRender();loadSiteSettings()}
function setClose(){$('setwrap').classList.add('hide')}

/* ── Ask Noctyx (copilot.py): a conversation with the site's assistant, which looks things up with the
   console's own data before it answers. The LLM's text is built as text nodes, never HTML. ── */
const ASK={sid:null,rev:0,busy:false,timer:null,hi:null,draft:''};
try{ASK.sid=sessionStorage.getItem('nx_ask_sid')}catch(e){}
const askEl=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
function askInline(t){const f=document.createDocumentFragment();t.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach(p=>{
 if(p.length>2&&p.startsWith('`')&&p.endsWith('`'))f.append(askEl('code',null,p.slice(1,-1)));
 else if(p.length>4&&p.startsWith('**')&&p.endsWith('**'))f.append(askEl('b',null,p.slice(2,-2)));else f.append(p)});return f}
function askRich(text){const f=document.createDocumentFragment();text.split(/```[a-z]*\n?([\s\S]*?)```/).forEach((p,k)=>{
 if(k%2)f.append(askEl('pre',null,p.replace(/\n$/,'')));else f.append(askInline(p.replace(/^\n|\n$/g,'')))});return f}
const ASK_HINTS=['How is the cluster right now?','Did anything fail in the last 24 hours?','Which GPU ran hottest today?','When will the backup storage fill up?'];
function askToggle(){const w=$('askwrap');if(w.classList.contains('hide'))askOpen();else askClose()}
function askOpen(){$('askwrap').classList.remove('hide');if(ASK.sid&&!ASK.rev)askPoll();else if(!ASK.sid)askEmpty();setTimeout(()=>$('askq').focus(),30)}
function askClose(){$('askwrap').classList.add('hide')}
function askEmpty(){const L=$('asklog');L.textContent='';const h=askEl('div','askhint');
 ASK_HINTS.forEach(t=>{const b=askEl('button',null,t);b.type='button';b.onclick=()=>askSend(t);h.append(b)});
 L.append(askEl('div','askmsg bot','Ask anything about this site: its machines, models, backups, alerts or storage. I look at the live data before I answer.'),h)}
function askNew(){ASK.sid=null;ASK.rev=0;try{sessionStorage.removeItem('nx_ask_sid')}catch(e){}$('askmodel').textContent='';askEmpty();$('askq').focus()}
function askItem(i){
 if(i.kind=='user')return askEl('div','askmsg user',i.text);
 if(i.kind=='assistant'){const d=askEl('div','askmsg bot');d.append(askRich(i.text));if(i.model)$('askmodel').textContent=i.model;return d}
 if(i.kind=='tool'){const d=askEl('details','asktool'+(i.ok?'':' bad'));const a=Object.entries(i.args||{}).map(([k,v])=>k+'='+JSON.stringify(v)).join(' ');
  d.append(askEl('summary',null,(i.ok?'looked at ':'could not look at ')+i.tool+(a?' ('+a+')':'')),askEl('pre',null,i.out||''));return d}
 if(i.kind=='error')return askEl('div','askmsg err',i.text);
 return askEl('div','askmsg',i.text||'')}
function askRender(v){const L=$('asklog');if(!ASK.rev)L.textContent='';
 L.querySelectorAll('.askbusy').forEach(e=>e.remove());
 (v.items||[]).forEach(i=>{const old=L.querySelector(`[data-i="${i.id}"]`);const n=askItem(i);n.dataset.i=i.id;if(old)old.replaceWith(n);else L.append(n)});
 ASK.rev=Math.max(ASK.rev,v.rev||0);ASK.busy=!!v.busy;
 if(ASK.busy)L.append(askEl('div','askbusy','looking'));
 $('asknote').textContent=v.advice_only?'This model does not take tool calls: it answers without looking anything up.':'It looks things up but changes nothing: what it suggests, you do.';
 L.scrollTop=L.scrollHeight}
async function askPoll(){clearTimeout(ASK.timer);if(!ASK.sid)return;
 let v;try{const r=await fetch('/api/v1/ask/'+encodeURIComponent(ASK.sid)+'?since='+ASK.rev);v=await r.json();
  if(!r.ok){if(v.ended){ASK.sid=null;ASK.rev=0;ASK.busy=false;try{sessionStorage.removeItem('nx_ask_sid')}catch(e){}}
   $('asklog').append(askEl('div','askmsg err',v.ended?'This conversation has ended (the console restarted, or it sat unused for hours): your next question starts a new one.':v.error||('HTTP '+r.status)));return}}
 catch(e){ASK.timer=setTimeout(askPoll,2000);return}
 askRender(v);if(ASK.busy)ASK.timer=setTimeout(askPoll,800)}
async function askPrompt(){const L=$('asklog'),tok=localStorage.getItem(LS_TOK)||'';
 let d;try{const r=await fetch('/api/v1/ask-prompt?'+new URLSearchParams({sid:ASK.sid||'',tunit:TU,tz:askZone()}),{headers:{'Authorization':'Bearer '+tok}});d=await r.json()}catch(e){d={error:String(e)}}
 if(d.error){L.append(askEl('div','askmsg err',d.error));return}
 L.querySelectorAll('.askprompt').forEach(e=>e.remove());
 const x=askEl('details','asktool askprompt');x.open=true;
 x.append(askEl('summary',null,'What Ask is told'+(ASK.sid?' in this conversation':' in a new conversation')+': '+(d.custom?"this site's own prompt":'the built-in prompt')+(d.notes?", plus the site's notes":'')+' (Configure › Assistant LLM)'),askEl('pre',null,d.system));
 L.append(x);x.scrollIntoView({block:'start'})}
function askZone(){try{return Intl.DateTimeFormat().resolvedOptions().timeZone||''}catch(e){return ''}}
async function askSend(text){text=(text||'').trim();if(!text||ASK.busy)return;
 const L=$('asklog');if(!ASK.sid)L.textContent='';
 const tok=localStorage.getItem(LS_TOK)||'';
 let r,d;try{r=await fetch('/api/v1/ask',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
  body:JSON.stringify({sid:ASK.sid,text,tunit:TU,tz:askZone()})});d=await r.json()}catch(e){d={error:String(e)}}
 if(!r||!r.ok){if(r&&r.status==409&&/ended/.test(d.error||'')){askNew();return askSend(text)}
  L.append(askEl('div','askmsg err',d.error||'the console did not answer'));return}
 if(d.sid!==ASK.sid){ASK.sid=d.sid;ASK.rev=0;try{sessionStorage.setItem('nx_ask_sid',d.sid)}catch(e){}}
 askRemember(text);ASK.busy=true;askSet('');askPoll()}
/* earlier questions, newest last, kept in this browser: Up/Down step through them like a shell, Enter asks the
   one shown again, Esc clears the box (Esc on an empty box closes the drawer) */
const ASK_HIST='nx_ask_hist',ASK_HIST_MAX=50;
function askHist(){try{const h=JSON.parse(localStorage.getItem(ASK_HIST)||'[]');return Array.isArray(h)?h:[]}catch(e){return []}}
function askRemember(t){const h=askHist().filter(x=>x!==t);h.push(t);try{localStorage.setItem(ASK_HIST,JSON.stringify(h.slice(-ASK_HIST_MAX)))}catch(e){}
 ASK.hi=null;ASK.draft=''}
function askSet(v){const q=$('askq');q.value=v;q.style.height='auto';q.style.height=Math.min(140,q.scrollHeight)+'px';
 q.setSelectionRange(v.length,v.length)}
function askStep(dir){const h=askHist();if(!h.length)return false;
 let i=ASK.hi==null?h.length:ASK.hi;if(ASK.hi==null)ASK.draft=$('askq').value;
 i=Math.max(0,Math.min(h.length,i+dir));if(i===(ASK.hi==null?h.length:ASK.hi))return false;
 ASK.hi=i==h.length?null:i;askSet(i==h.length?ASK.draft||'':h[i]);return true}
$('askform').addEventListener('submit',e=>{e.preventDefault();askSend($('askq').value)});
$('askq').addEventListener('keydown',e=>{const q=e.target;
 if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();askSend(q.value);return}
 if(e.key=='Escape'&&q.value){e.preventDefault();e.stopPropagation();ASK.hi=null;ASK.draft='';askSet('');return}
 const atTop=!q.value.slice(0,q.selectionStart).includes('\n'),atEnd=!q.value.slice(q.selectionEnd).includes('\n');
 if(e.key=='ArrowUp'&&atTop&&!e.shiftKey&&askStep(-1))e.preventDefault();
 else if(e.key=='ArrowDown'&&atEnd&&!e.shiftKey&&askStep(1))e.preventDefault()});
$('askq').addEventListener('input',e=>{const t=e.target;ASK.hi=null;t.style.height='auto';t.style.height=Math.min(140,t.scrollHeight)+'px'});
const segH=(cur,opts,fn)=>`<div class=seg>${opts.map(([v,l])=>`<button type=button class="${v==cur?'on':''}" onclick="${fn}('${v}')">${l}</button>`).join('')}</div>`;
function setRender(){
 const P=$('setpanel');if(!P||$('setwrap').classList.contains('hide'))return;
 const s=NXT.eff(),L=NXT.local(),own=Object.keys(L).length>0,f=NXT.face();
 const sch=s.mode=='light'?'light':s.mode=='system'?(f.scheme):'dark';
 const cards=Object.entries(NXT.PRESETS).map(([id,p])=>{const x=NXT.derive(p[sch=='light'?'light':'dark']);
   return `<button type=button class="tcard${s.theme==id?' on':''}" onclick="setTheme('${id}')"><b>${esc(p.label)}</b><small>${esc(p.blurb)}</small>
    <span class=swatch>${p.style?[x.bg,...['cpu','gpu','mem','net','power','llm','temp'].map(k=>p.dark.k[k])].map(c=>`<i style="background:${c}"></i>`).join(''):['bg','panel','accent','accent2','ok','warn','bad'].map(k=>`<i style="background:${x[k]}"></i>`).join('')}</span></button>`}).join('')
  +`<button type=button class="tcard${s.theme=='custom'?' on':''}" onclick="setTheme('custom')"><b>Custom</b><small>Your nine colours; the rest is mixed from them</small>
    <span class=swatch>${['bg','panel','accent','ok','warn','bad'].map(k=>`<i style="background:${s.custom[k]}"></i>`).join('')}</span></button>`;
 const names={bg:'Background',panel:'Panels',text:'Text',muted:'Muted text',accent:'Accent',line:'Lines',ok:'Good',warn:'Warning',bad:'Bad'};
 const picks=NXT.BASE.map(k=>`<label><input type=color value="${esc(s.custom[k])}" oninput="setCustom('${k}',this.value)">${names[k]}</label>`).join('');
 const site=SITESET&&SITESET.settings&&Object.keys(SITESET.settings).length;
 const canSite=!ME||ME.mode!='accounts'||ME.role=='admin',acct=ME&&ME.user;
 P.innerHTML=`<h2 id=setTitle>Display settings <span class=sp></span><button class=hbtn onclick="setReset()" title="Back to the site's defaults (or the shipped look)">reset</button><button class=hbtn onclick="setClose()">close</button></h2>
 <p class=setnote>Applied as you change them. They change how the console looks, never what it measures or does.</p>
 <div class=setstore>${own?(acct&&whereSaved()=='account'?'Saved in <b>your account</b> ('+esc(ME.user)+')':'<b>This browser</b> has its own settings'):'Using <b>'+(site?'the site\'s defaults':'the shipped look')+'</b>'}
  <span class=hspacer></span>${canSite?'<button class=hbtn onclick="setSaveSite()" title="Every browser without its own settings gets these">save as site default</button>':''}${own?'<button class=hbtn onclick="setReset()">use site default</button>':''}</div>
 ${acct?`<div class=setrow><label>Keep my settings</label>${segH(whereSaved(),[['browser','In this browser'],['account','In my account']],'setWhere')}<div class=hint>In your account they follow you to any browser you sign in on.</div></div>`:''}
 <div id=setmsg class=setnote></div>
 <div class=setgrp><h3>Appearance</h3><p class=gnote>The colours of the console: page, panels, text, lines, accent and the good / warning / bad states.</p>
  <div class=setrow><label>Mode</label>${segH(s.mode,[['light','Light'],['dark','Dark'],['system','System']],'setMode')}<div class=hint>Light or dark, or whatever the operating system asks for (System follows it as it changes).</div></div>
  <div class=setrow><label>Theme</label><div class=tcards>${cards}</div><div class=hint>Each has a light and a dark face; the mode picks which. Custom uses the nine colours below.</div></div>
  <div class="setrow${s.theme=='custom'?'':' dim'}"><label>Custom colours</label><div class=cpick>${picks}</div><div class=hint>Only used by the Custom theme. Its mode comes from the background you pick.</div></div></div>
 ${fxGroup(s)}
 ${kioskGroup(s)}
 <div class=setgrp><h3>Units</h3>
  <div class=setrow><label>Temperature</label>${segH(TU,[['F','°F'],['C','°C']],'setTU')}<div class=hint>Readings and alert thresholds stay in °C underneath; only the display converts.</div></div></div>
 <div class=setgrp><h3>Layout</h3>
  <div class=setrow><label>Panels</label><div><button class=hbtn onclick="layoutReset()">reset panel layout</button></div><div class=hint>Puts every dashboard panel back where it started (this browser).</div></div></div>`}
function setMode(m){NXT.save({mode:m});pushAccount();setRender()}
function setFx(k,v){NXT.save({fx:{[k]:v}});pushAccount();setRender()}
function fxGroup(s){const on=NXT.face().style=='phosphor',fx=s.fx||NXT.FXDEF,
  row=(k,lab,opts,hint)=>`<div class=setrow><label>${lab}</label><div class=seg>${opts.map(([v,l])=>`<button type=button class="${fx[k]==v?'on':''}" onclick="setFx('${k}','${v}')">${l}</button>`).join('')}</div><div class=hint>${hint}</div></div>`,
  oo=[['off','Off'],['on','On']];
 return `<div class="setgrp${on?'':' dim'}"><h3>Phosphor effects</h3><p class=gnote>${on?'Each is a single still layer, or moves with the compositor alone (no repaint of the page): light on the GPU.':'For the Phosphor themes: pick one above to use them.'}</p>
  ${row('glow','Glow',[['off','Off'],['soft','Soft'],['normal','Normal'],['bright','Bright']],'How much the meters, traces and numerals bloom.')}
  ${row('scan','Scanlines',[['off','Off'],['kiosk','Kiosk'],['all','Everywhere']],'Fine horizontal lines, like a CRT.')}
  ${row('vignette','Vignette',oo,'Darker corners, like the curve of a tube.')}
  ${row('grain','Grain',oo,'A faint, still film grain over the glass.')}
  ${row('fringe','RGB fringe',oo,'A hint of colour separation on the big numerals.')}
  ${row('beam','Beam sweep',oo,'A soft band of light that rolls down the screen every few seconds.')}
  ${row('flicker','Flicker',oo,'A barely-there phosphor flicker. Both moving effects stop when the system asks for reduced motion.')}</div>`}
function setTheme(id){NXT.save({theme:id});pushAccount();setRender()}
function setKiosk(k,v){const val=(k=='rotation')?v=='true':(k=='fit'||k=='cols'?v:+v);NXT.save({kiosk:{[k]:val}});pushAccount();setRender();
 if(VIEW=='kiosk'){kioskPg=0;const p=PANELS.find(x=>x.id=='kiosk');if(p&&p._data!==undefined)repaint('kiosk',p._data)}}
function kioskGroup(s){const k=s.kiosk||NXT.DEF.kiosk,
 row=(kk,lab,opts,hint)=>`<div class=setrow><label>${lab}</label><div class=seg>${opts.map(([v,l])=>`<button type=button class=\"${String(k[kk])==String(v)?'on':''}\" onclick=\"setKiosk('${kk}','${v}')\">${l}</button>`).join('')}</div><div class=hint>${hint}</div></div>`,
 lv=[['0','Off'],['1','On'],['2','Detail']];
 return `<div class=setgrp><h3>Kiosk</h3><p class=gnote>How much the wall display (<code>/kiosk</code>) shows. Any browser can also override this with URL flags: <code>?page=1</code>, <code>?alerts=2</code>, <code>?rotation=1</code>, <code>?fit=scroll</code>.</p>
 ${row('alerts','Alerts',lv,'Detail: a full page of alert summaries and triage (?page=1). The nodes page never shows alerts.')}
 ${row('backups','Backups',lv,'Off: the ratio only. On: the count. Detail: a page of per-host last run, next due, window.')}
 ${row('rotation','Rotation',[['false','Off'],['true','On']],'Page through nodes → alerts → backups every 20 seconds. Only takes effect in a browser whose URL asks for it (?rotation=1): the wall never changes views on its own.')}
 ${row('dense','Dense header',[['false','Off'],['true','On']],'Five fleet numbers on the header line (adds LLMs serving); off keeps four with bigger type.')}
 ${row('cols','Grid columns',[['0','Auto'],['2','2'],['3','3'],['4','4'],['5','5']],'Force the node grid to a column count. Auto picks the most columns whose cards still fit the screen. Try 3 for taller cards on a wall with rows to spare.')}
 ${row('fit','Fit',[['shrink','Shrink'],['scroll','Scroll']],'Shrink: everything on one screen, the type gets small (the classic kiosk). Scroll: full-size type, the grid scrolls when it overflows.')}</div>`}
function setCustom(k,v){NXT.save({theme:'custom',custom:{[k]:v}});clearTimeout(setCustom._p);setCustom._p=setTimeout(pushAccount,600);
 /* re-render only the cards/swatches when the pickers settle: keep the open picker */
 clearTimeout(setCustom._t);setCustom._t=setTimeout(setRender,400)}
function setReset(){NXT.clearLocal();TU=NXT.eff().tunit=='C'?'C':'F';PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)});setRender()}
async function setSaveSite(){const s=NXT.eff(),body={mode:s.mode,theme:s.theme,tunit:TU,custom:s.custom,fx:s.fx,kiosk:s.kiosk};
 const m=$('setmsg');if(m)m.textContent='saving as the site default…';
 const r=await act('setConsoleDefaults',{settings:body});
 if(m)m.textContent=r&&r.error?'✗ '+r.error:'✓ saved: browsers without their own settings use these';setTimeout(loadSiteSettings,2500)}
loadSiteSettings();loadMe();
if(location.hash=='#settings')setOpen();

/* ── About ── */
function rAbout(d){
 if(!d)return '<div class=empty>loading…</div>';
 const S=d.site||{},R=d.repo||'https://github.com/BobClawblaw/noctyx';
 const pill=(k,v,c)=>`<span class="apill ${c||''}"><span>${esc(k)}</span><b>${esc(v)}</b></span>`;
 return `<div class=about>
  <img class=abanner src="/assets/banner.svg" alt="Noctyx — the night-shift operator for your AI cluster">
  <div class=apills>${pill('version',d.version)}${pill('license','Apache 2.0')}${pill('python',d.python)}${pill('restic',d.restic)}${pill('status','alpha','warn')}</div>
  <div class=acols>
   <div class=acard><h3>What Noctyx does</h3>
    <div class=afeat><b>Deploy</b><span>Model recipes onto GPU slots: built while the current model serves, switched under the slot's GPU lock, rolled back automatically. Public recipes for your hardware, ranked by measured speed.</span></div>
    <div class=afeat><b>Cache</b><span>One copy of every Hugging Face model on the cluster, served back as <code>HF_ENDPOINT</code>; keeps working when the Hub does not.</span></div>
    <div class=afeat><b>Protect</b><span>Nightly snapshots of every node, Docker volumes, consistent database dumps, retention, verification, a dual-arch recovery stick for bare-metal restores.</span></div>
    <div class=afeat><b>Watch</b><span>Live fleet: temperatures, power, tokens/s per model, alerts with LLM triage, a morning brief, hygiene with the space each fix frees. <b style="font:inherit;color:var(--fg)">Ask</b> answers questions from the cluster's own data.</span></div>
    <div class=afeat><b>Heal</b><span>Every upgrade checks itself and rolls back when it fails, one machine first. A repair loop every 5 minutes restarts what stopped and puts back what drifted, then says what it did.</span></div></div>
   <div class=acard><h3>This site</h3>
    <div class=arow><span>head node</span><b>${esc(S.head||'?')}</b></div>
    <div class=arow><span>hosts</span><b>${esc(S.hosts)}</b></div>
    <div class=arow><span>GPU slots · recipes</span><b>${esc(S.slots)} · ${esc(S.recipes)}</b></div>
    <div class=arow><span>backup target</span><b>${esc(S.nas)}</b></div>
    <div class=arow><span>console build</span><b>${esc(d.build)}</b></div>
    <h3 style="margin-top:14px">Keys</h3>
    <div class=akeys><kbd>1</kbd> dashboard <kbd>2</kbd> cluster <kbd>3</kbd> recipes <kbd>4</kbd> configuration <kbd>5</kbd> kiosk <kbd>Esc</kbd> close menus and the drawer · settings: the gear, or <code>/#settings</code></div></div>
   <div class=acard><h3>Documentation</h3>
    <div class=alinks><a href="${R}#readme" target=_blank rel=noopener>README and screenshots</a><a href="${R}/tree/main/docs/guide" target=_blank rel=noopener>Operator's guide</a>
     <a href="${R}/blob/main/docs/guide/install.md" target=_blank rel=noopener>Install a head node and agents</a><a href="${R}/blob/main/docs/guide/restore.md" target=_blank rel=noopener>Restore a file, a node, the head</a>
     <a href="${R}/blob/main/docs/guide/recipes.md" target=_blank rel=noopener>Deploy recipes</a><a href="${R}/blob/main/docs/guide/site-config.md" target=_blank rel=noopener>Site config reference</a>
     <a href="${R}/blob/main/docs/spec/recipe-deploy.md" target=_blank rel=noopener>Recipe deploy spec (draft)</a></div></div>
   <div class=acard><h3>License</h3>
    <p>Licensed under the <b>Apache License, Version 2.0</b>.</p>
    <p class=mut>Free to use, modify and redistribute. The bundled fonts stay under
     the SIL Open Font License; the recipe deploy spec is CC-BY-4.0.</p>
    <div class=alinks><a href="${R}/blob/main/LICENSE" target=_blank rel=noopener>LICENSE</a><a href="/assets/fonts/LICENSES.txt">Font licenses</a></div></div>
   <div class=acard><h3>Support Noctyx</h3>
    <p class=mut>Donations keep the project going.</p>
    <div class=adonate><img src="/assets/donate-qr.png" alt="QR code: the bitcoin address" width=164 height=164>
     <div><div class=k>Bitcoin</div><button class=addr title="copy" onclick="copyAddr(this,'${esc(d.donate)}')">${esc(d.donate)}</button><div class="mut copied"></div></div></div></div>
  </div></div>`}
function copyAddr(btn,a){const ok=()=>{btn.nextElementSibling.textContent='copied ✓';setTimeout(()=>btn.nextElementSibling.textContent='',1800)};
 try{navigator.clipboard.writeText(a).then(ok,()=>{})}catch(e){const r=document.createRange();r.selectNodeContents(btn);getSelection().removeAllRanges();getSelection().addRange(r)}}

/* ── Kiosk ── */
const lastV=s=>Array.isArray(s)&&s.length?+s[s.length-1][1]||0:0;
/* compact magnitude for the header vitals: no space before the unit, so the number and its
   suffix read as one token at kiosk distance. */
const kTok=v=>v>=1e9?(v/1e9).toFixed(2)+'G':v>=1e6?(v/1e6).toFixed(v>=1e7?0:1)+'M':v>=1e3?(v/1e3).toFixed(v>=1e5?0:1)+'k':String(Math.round(v));
/* the identity of the kiosk's current screen (fit hysteresis compares it): page + the
   settings that change how much room the grid gets. */
function pgKey(){const k=kioskCfg();return[kioskPage(),k.rotation?'r':'-',k.fit].join('/')}
/* the kiosk's info level: site default (console › settings › Kiosk), overridable per browser by
   localStorage and by the URL. ?alerts= ?backups= (2 adds a detail page),
   ?rotation=1 pages through views every 20 s, ?page=n pins one view (0 nodes · 1 alerts ·
   2 backups; views beyond the first only exist as their detail level allows). */
let kioskPg=0;
/* The plain /kiosk is the classic one-screen wall: shrink fit, no extra pages, nothing off the
   display. URL flags extend it per browser: ?page=n pins one screen, and ?alerts= ?backups=
   ?rotation= ?fit= opt into the extra content — every one of which forces a fit that
   keeps whatever it shows fully on screen (shrink) unless the browser explicitly asks for
   ?fit=scroll. The site's Kiosk settings hold the same switches for browsers that want them
   as the standing choice. (operator, 2026-10-22, after a scroll-default regression: a wall
   must never hide information below or beside its own edges.) */
const kioskCfg=()=>{
 const q=new URLSearchParams(location.search),k={...NXT.eff().kiosk};
 const has=['alerts','backups','rotation','page','fit','cols'].some(p=>q.has(p));
 for(const p of['alerts','backups']){const v=+q.get(p);if(q.get(p)!=null&&!isNaN(v))k[p]=v}
 if(q.get('rotation')!=null)k.rotation=q.get('rotation')!='0';
 if(q.get('dense')!=null)k.dense=q.get('dense')!='0';
 {const v=+q.get('cols');if(q.get('cols')!=null&&!isNaN(v)&&v>=1&&v<=8)k.cols=v}   /* grid columns: ?cols=3 */
 if(q.has('page')){k.rotation=false;k.fit='shrink'}           /* a pinned view: one screen, that view */
 else if(has||k.rotation)k.fit='shrink';                     /* extra content must still fit: shrink */
 if(q.get('fit')=='scroll'||q.get('fit')=='shrink')k.fit=q.get('fit');   /* an explicit choice wins */
 return k};
const kioskPages=()=>{const k=kioskCfg();return 1+(k.alerts>1?1:0)+(k.backups>1?1:0)};
const kioskPage=()=>{const q=new URLSearchParams(location.search);
 if(q.get('page')!=null)return Math.max(0,Math.min(kioskPages()-1,+q.get('page')||0));
 return kioskPg%kioskPages()};
/* the header row: brand + big numbers + clock. a=alerts, k=kiosk settings.
   dense (default) packs five KPIs; spare keeps the classic four with bigger type. */
function kioskTop(M,H,order,I,a,k,pg,pages){
 const tok=((M.fleet||{}).series||{}).tokens||{};
 const tokSeries=Object.values(tok).reduce((s,x)=>s.concat(x||[]),[]);
 const tps=Object.values(tok).reduce((s,x)=>s+lastV(x),0);
 /* the hour's peak of the fleet total: bucket the per-model series by timestamp (the scrape
    grid is shared, so points at the same minute sum cleanly; a straggler bucket of one model
    under-reads and can never win the max against a full bucket) */
 const byT={};for(const[t,v]of tokSeries)byT[t]=(byT[t]||0)+(+v||0);
 const peak=Object.values(byT).reduce((m,v)=>v>m?v:m,0);
 const gpuW=order.reduce((s,n)=>s+(+(H[n]||{}).gpu_power||0),0);
 /* fleet GPU-watts history for the header graph: the API's own fleet series when it carries one,
    else the sum of each node's power sparkline bucketed by timestamp */
 const gpw=((M.fleet||{}).series||{}).gpu_power||(()=>{const byT={};
  for(const n of order)for(const[t,v]of (((H[n]||{}).spark||{}).gpu_power||[]))byT[t]=(byT[t]||0)+(+v||0);
  return Object.entries(byT).map(([t,v])=>[+t,v]).sort((x,y)=>x[0]-y[0])})();
 const act=(a.active||[]).length,crit=(a.active||[]).filter(x=>x.severity=='critical').length;
 /* fleet thermal ceiling: the hottest GPU anywhere, and its trend. The tiles show each node's
    own temp; this is the one number that says "something is running hot" from across the room,
    and it pairs with watts as the two physical-risk readings on the wall. */
 const tmaxSpk=((M.fleet||{}).series||{}).gpu_temp_max||(()=>{const byT={};
  for(const n of order)for(const[t,v]of (((H[n]||{}).spark||{}).gpu_temp||[]))byT[t]=Math.max(byT[t]||0,+v||0);
  return Object.entries(byT).map(([t,v])=>[+t,v]).sort((x,y)=>x[0]-y[0])})();
 const tmaxNow=order.reduce((m,n)=>Math.max(m,+(H[n]||{}).gpu_temp||0),0)||
  (tmaxSpk.length?lastV(tmaxSpk):null);
 const tmaxCls=tmaxNow==null?'':tmaxNow>=85?'bad':tmaxNow>=80?'warn':'';
 /* fleet vitals: saturation and the day's totals. Queue depth is the only saturation signal the
    tiles do not carry — a per-node card shows what it serves, not what is waiting behind it. */
 const qNow=lastV(((M.fleet||{}).series||{}).waiting)||0;
 const kwh=I.gpu_wh_24h!=null?I.gpu_wh_24h/1000:null;
 const tok24=I.tokens_24h;
 const llmN=(I.llm_up!=null&&I.llm_total!=null)?`${Math.round(I.llm_up)}/${Math.round(I.llm_total)}`:null;
 const llmBad=llmN&&I.llm_up<I.llm_total;
 const now=new Date(),tm=now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}).match(/^([\d:.\s]+)(.*)$/)||['',now.toTimeString().slice(0,5),''];
 const dim=pgOn=>k.dense?'':(pgOn?'':' dim'), dn=()=>k.dense?' kdense':'';
 return `<div class=ktop><a class=kbrand href="/"><span class=mark></span><span class=wordmark>Noct<b>yx</b></span></a>
 <div class="kpair${pg==0?'':' dim'}" data-k=toks>
  <div class=ktopg>${chart('', Object.entries(tok).map(([m,pts])=>({name:m,pts,cls:'t'})), v=>v.toFixed(0)+' tok/s', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b data-ghost="${ghost(tps.toFixed(0))}">${tps.toFixed(0)}</b><span>tokens /<br>second</span></div>
 </div>
 <div class="kpair${pg==0?'':' dim'}" data-k=watts>
  <div class=ktopg>${chart('', [{name:'fleet',pts:gpw,cls:'w'}], v=>Math.round(v)+' W', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b data-ghost="${ghost(Math.round(gpuW))}">${Math.round(gpuW)}</b><span>GPU<br>watts</span></div>
 </div>
 <div class="kpair${pg==0?'':' dim'}" data-k=temp>
  <div class=ktopg>${chart('', [{name:'hottest',pts:tmaxSpk,cls:'c1'}], v=>Math.round(v)+' °C', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b class="${tmaxCls}" data-ghost="${ghost(Math.round(tmaxNow||0))}">${tmaxNow!=null?Math.round(tmaxNow):'—'}</b><span>GPU temp<br>max</span></div>
 </div>
 <div class="kvitals${pg==0?'':' dim'}">
  <div class=kvital><b class="${qNow>2?'bad':qNow>0?'warn':''}" data-ghost="${ghost(Math.round(qNow))}">${Math.round(qNow)}</b><span>queued</span></div>
  <div class=kvital><b data-ghost="${ghost(kwh!=null?kwh.toFixed(1):'—')}">${kwh!=null?kwh.toFixed(1):'—'}</b><span>kWh / 24h</span></div>
  <div class=kvital><b data-ghost="${ghost(tok24!=null?kTok(tok24):'—')}">${tok24!=null?kTok(tok24):'—'}</b><span>tok / 24h</span></div>
 </div>
 ${k.dense&&llmN?`<div class="kbig${dn()}${pg==0?'':' dim'}" data-k=ok><b class="${llmBad?'warn':'ok'}" data-ghost="${ghost(llmN)}">${llmN}</b><span>LLM serving</span></div>`:''}
  <div class=kclock><b data-ghost="${ghost(tm[1].trim())}">${esc(tm[1].trim())}${tm[2].trim()?`<small>${esc(tm[2].trim())}</small>`:''}</b><span>${now.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'})}</span>${pages>1?`<span class=kpg>${pg+1} / ${pages}</span>`:''}</div></div>`}
/* one node card: metrics, meters, and EVERY serving model with its rate (an idle head-server
   slot no longer masks the model actually running on the node) */
function kioskNode(n,h){const srvs=((h.llm||{}).servers||[]).filter(s=>s.model||s.spark);
 const mem=h.mem_total?Math.round(100*h.mem_used/h.mem_total):null,up=h.up!==0&&h.up!==false;
 const t=(v,w)=>v==null?'—':`<b class="${v>=w?'bad':v>=w-10?'warn':''}">${fmtT(v)}</b>`;
 const sp=h.spark||{};
 /* every node now carries its own throughput: the live tok/s for the models it serves plus the
    last-hour total, and the 30-min generation trace under the GPU/CPU one (user, 2026-10-22). */
 const gen=srvs.reduce((a,s)=>a+((s.spark&&s.spark.gen_tps&&s.spark.gen_tps.length)?lastV(s.spark.gen_tps):(s.gen_tps||0)),0);
 const tok1h=srvs.reduce((a,s)=>a+(s.tokens_1h||0),0);
 const req1h=srvs.reduce((a,s)=>a+(s.requests_1h||0),0);
 const genspk=srvs.map(s=>s.spark&&s.spark.gen_tps).filter(p=>p&&p.length>1);
 /* prefill is a separate phase from decode: prompt tokens per second of prompt-processing time.
    It gets its own trace per endpoint so a burst of prompt work is visible next to generation
    (user, 2026-10-22). Prefill tok/s is orders of magnitude above decode, so it is drawn on its
    own scale against the tile's max, not overlaid on the generation trace's scale. */
 const pfsrv=srvs.map(s=>({model:s.model,pts:((s.spark&&s.spark.prefill_tps)||[]).filter(p=>p[1]!=null)})).filter(x=>x.pts.length>1);
 /* the trace's prompt line is prompt INPUT, not prefill speed: prompt tokens per second
    across the endpoint's requests (0 when idle, a jump when big prompts land), on its own
    zero-based scale — prompt volume runs far above generation. Prefill speed is nearly
    constant per model, so as a line it only rode the top edge; it stays a number below. */
 const ptspk=srvs.map(s=>((s.spark&&s.spark.prompt_tps)||[]).filter(p=>p[1]!=null)).filter(p=>p.length>1);
 const pfNow=srvs.reduce((a,s)=>a+(s.prefill_tps||0),0);
 /* a worker node serves no endpoint of its own: it runs tensor-parallel work for a head, so it
    has no tok/s of its own to report. Show what it IS doing (worker for X, tp N) rather than a
    bare dash, which read as "idle" (user, 2026-10-22: worker nodes don't offer toks). */
 const topo=((h.llm||{}).topology||[]);
 const wk=topo.find(t=>t.role=='worker');
 const hasTok=tok1h>0||req1h>0||gen>0;
 /* prefill readout: per endpoint, because prefill is a property of the endpoint's own prompt
    processing, not of the node. A dash when the endpoint is idle — a 0 tok/s prefill figure is
    misleading, since prefill only exists while a prompt is being processed. */
 const pfLine=srvs.length&&pfsrv.length?`<div class=kllm><span class=mut>${esc(pfsrv.map(x=>x.model||'—').join(', '))}</span><b>${pfNow>0?fmtK(pfNow):'—'}<small> prefill tok/s</small></b>
   <span class=mut>${pfNow>0?'prompt processing':'idle'}</span></div>`:'';
 const tpsLine=srvs.length?`<div class=kllm><span class=mut>${esc(srvs.map(s=>s.model||'—').join(', '))}</span><b>${hasTok?gen.toFixed(0):'—'}<small> tok/s</small></b>
   <span class=mut>${hasTok?`${fmtK(tok1h)} tok · ${Math.round(req1h)} req / 1h`:'no traffic / 1h'}</span></div>`
   :wk?`<div class=kllm><span class=mut>worker → ${esc(wk.worker_for||'head')}${wk.tp?' · tp '+esc(wk.tp):''}</span><b>—<small> tok/s</small></b>
     <span class=mut>${esc(wk.model||'tensor-parallel')}</span></div>`
   :`<div class=kllm><span class=mut>no model served</span><b>—<small> tok/s</small></b><span class=mut>idle</span></div>`;
 /* the 30-min trace fills the tile's middle: a 300x100 user-space box mapped exactly onto the
    measured pixels (none-scaling strokes keep the hairlines crisp); flat lines sit 2px above
    their label so the two never collide. The data window slides one step every tick without the
    box's endpoints moving, so the <svg> carries the window stamp: without it morph() sees an
    identical subtree and never touches the points, and the trace freezes (user, 2026-10-22) */
 const tr=(sp.gpu_util&&sp.gpu_util.length>1)?`<div class=ktrace><svg class=kspark viewBox="0 0 300 100" preserveAspectRatio="none" aria-hidden="true" data-win="${sp.gpu_util[0][0]}|${sp.gpu_util.at(-1)[0]}|pt${ptspk.length}">
   <line class="grid" x1="0" y1="48.3" x2="300" y2="48.3"/><line class="grid" x1="0" y1="2.3" x2="300" y2="2.3"/>${_lines([{pts:sp.gpu_util,cls:'a'},{pts:sp.cpu,cls:'g'},...genspk.map(p=>({pts:p,cls:'t'}))],300,96,100)}${ptspk.length?_lines(ptspk.map(p=>({pts:p,cls:'p'})),300,96,Math.max(...ptspk.flat().map(p=>p[1]))||1):''}</svg>
  <i class=a style="top:calc(2px + .3em)">GPU</i><i class=g style="bottom:2px">CPU</i>${genspk.length?'<i class=t style="bottom:calc(2px + 1.4em)">GEN TOK/S</i>':''}${ptspk.length?'<i class=p style="top:calc(2px + 1.4em)">PROMPT TOK/S</i>':''}</div>`:'';
 return `<div class="knode ${up?'':'down'}"><div class=kh><span class="dot ${up?'ok':'bad'}"></span><b>${esc(n)}</b>
   ${h.load1!=null?`<span class=kload>load ${(+h.load1).toFixed(1)}</span>`:''}</div>
  <div class=kgrid><span>GPU</span><b>${h.gpu_util!=null?Math.round(h.gpu_util)+'%':'—'}</b><span>GPU temp</span>${t(h.gpu_temp,85)}
   <span>power</span><b>${h.gpu_power!=null?Math.round(h.gpu_power)+' W':'—'}</b><span>CPU temp</span>${t(h.sys_temp,90)}
   <span>CPU</span><b>${h.cpu!=null?Math.round(h.cpu)+'%':'—'}</b><span>memory</span><b>${mem!=null?mem+'%':'—'}</b></div>
  ${tr}
  <div class=kmeters><span>GPU</span>${bar(h.gpu_util,true,'gpu')}<span>CPU</span>${bar(h.cpu,true,'cpu')}
   <span>MEM</span>${bar(mem,true,'mem')}<span>TEMP</span>${bar(h.gpu_temp!=null?h.gpu_temp/95*100:0,false,'temp')}</div>
  ${tpsLine}${pfLine}</div>`}
/* the alerts page (alerts: 2): name + host + since + summary + the LLM triage, newest-severity first */
function kioskAlerts(a){const L=(a.active||[]).slice().sort((x,y)=>({critical:0,warning:1,info:2})[x.severity]-({critical:0,warning:1,info:2})[y.severity]);
 if(!L.length)return '<div class=kdetail><div class="kticker kempty"><b class=ok>✓</b><span class=mut>no active alerts</span></div></div>';
 return `<div class=kdetail>${L.map(x=>`<div class="kcard ${x.severity}"><div class=kh><span class="dot ${x.severity=='critical'?'bad':x.severity=='warning'?'warn':'info'}"></span><b>${esc(x.name)}</b><span class=mut>${esc(x.host||'fleet')}</span><span class=hspacer></span><span class=mut>since ${esc((x.since||'').slice(0,16).replace('T',' '))}</span></div>
  <div class=ksum>${esc(x.summary||'')}</div>${x.triage?`<div class=ktri>${esc(x.triage)}</div>`:''}</div>`).join('')}</div>`}
/* the backups page (backups: 2): one row per host/profile — last run, next due, health */
async function kioskBackups(){let S={rows:[]};try{S=await fetchJSON('/api/v1/schedule')}catch(e){}
 const ago=t=>{if(!t)return '—';const s=(Date.now()/1000-t)/3600;return s<1?Math.round(s*60)+' min':s<48?Math.round(s)+' h':Math.round(s/24)+' d'};
 const nxt=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'}):'—';
 return `<div class=kdetail>${(S.rows||[]).map(r=>`<div class="kcard ${r.health=='ok'?'info':r.failed_at?'critical':'warning'}"><div class=kh><span class="dot ${r.health=='ok'?'ok':r.failed_at?'bad':'warn'}"></span><b>${esc(r.host)}</b><span class=mut>${esc(r.profile)}</span></div>
  <div class=kgrid style="grid-template-columns:auto 1fr auto 1fr"><span>last</span><b>${ago(r.last_ts)}</b><span>next</span><b>${nxt(r.next_ts)}</b><span>every</span><b>${r.every?Math.round(r.every/3600)+' h':'—'}</b><span>window</span><b>${esc(r.window||'—')}</b></div>
  ${r.why?`<div class=ksum>${esc(r.why)}</div>`:''}</div>`).join('')}</div>`}
let _kbHtml='',_kbAt=0;
async function kioskBackupsCached(){if(Date.now()-_kbAt>60000){_kbAt=Date.now();_kbHtml=await kioskBackups()}return _kbHtml}
function rKiosk(d){
 if(!d||!d.m)return '<div class=empty>loading…</div>';
 return kioskDraw(d,()=>kioskFit())}
/* the rotation pager asks for a redraw with the new page; the panel loop repaints itself
   every int seconds — this only matters if that tick hasn't come round yet */
function kioskRepaint(){const p=PANELS.find(x=>x.id=='kiosk');if(p&&p._data!==undefined)repaint('kiosk',p._data)}
/* rKiosk stays synchronous for morph(); when a page needs an extra fetch (the backups rows),
   kioskDraw paints the rest now and patches the detail area in, then refits. */
function kioskDraw(d,done){
 const M=d.m,H=M.hosts||{},order=M.order||Object.keys(H),I=(M.fleet||{}).infra||{};
 const k=kioskCfg(),pages=kioskPages();let pg=Math.min(kioskPage(),pages-1);
 /* the recipe/slot row is gone (user, 2026-10-22): the wall is nodes only, and every node
    carries its own trace + throughput. Slot state still drives the per-node LLM readout via
    H[n].llm below, so nothing is lost — it just stops taking a third of the wall. */
 const top=kioskTop(M,H,order,I,d.a||{},k,pg,pages);
 let main='',extra=null;
 if(pg==1&&k.alerts>1)main=kioskAlerts(d.a||{});
 else if(pg==2&&k.backups>1){extra=kioskBackupsCached().then(html=>{const el=document.getElementById('kdetail');if(el)el.innerHTML=html;kioskFit()});
  main='<div class=kdetail id=kdetail></div>'}
 else{const nodes=order.map(n=>kioskNode(n,H[n]||{})).join('');
  main=`<div class=knodes>${nodes}</div>`}
 const out=`<div class="kiosk-wrap${k.fit=='scroll'?' kfit-scroll':''}" data-page="${pg}" data-pages="${pages}">${top}${main}</div>`;
 if(done)done();
 if(extra)extra.catch(()=>{});
 return out}

function llmStats(s){const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u> ms</span>':v.toFixed(1)+'<span class=u> s</span>';
 const kv=(k,v)=>`<div><span class=k>${k}</span><span class=v>${v}</span></div>`;
 return `<div class=llms>
  ${kv('running',`${s.running??0}${s.waiting?' <span class=warn>+'+s.waiting+'</span>':''}`)}
  ${kv('throughput',`${Math.round(s.gen_tps||0)}<span class=u> tok/s</span>`)}
  ${kv('decode',s.decode_tps!=null?`${Math.round(s.decode_tps)}<span class=u> tok/s</span>`:'—')}
  ${kv('prefill',s.prefill_tps!=null?`${esc(fmtK(s.prefill_tps))}<span class=u> tok/s</span>`:'—')}
  ${kv('prefill time',ms(s.prefill_p50))}
  ${kv('prefill / decode',s.prefill_share!=null?`${Math.round(s.prefill_share)}<span class=u>%</span> / ${Math.round(100-s.prefill_share)}<span class=u>%</span>`:'—')}
  ${kv('in → out',s.prompt_tok_p50!=null?`${esc(fmtK(s.prompt_tok_p50))} → ${esc(fmtK(s.gen_tok_p50||0))}`:'—')}
  ${kv('queue',ms(s.queue_p50))}
  ${kv('ttft p50 · p95',`${ms(s.ttft_p50)} · ${ms(s.ttft_p95)}`)}
  ${kv('e2e p50',ms(s.e2e_p50))}
  ${kv('inter-token',s.itl_p50!=null?Math.round(s.itl_p50*1000)+'<span class=u> ms</span>':'—')}
  ${kv('kv cache',`<span class="${s.kv_pct>90?'bad':s.kv_pct>70?'warn':''}">${s.kv_pct!=null?Math.round(s.kv_pct):'—'}<span class=u>%</span></span>`)}
  ${kv('last hour',`${Math.round(s.requests_1h||0)}<span class=u> req</span> · ${esc(fmtK(s.tokens_1h||0))}`)}
  ${kv('prefix hit',`${s.prefix_hit!=null?Math.round(s.prefix_hit):'—'}<span class=u>%</span>${(s.preempt_rate||0)>0?' <span class=warn>preempt</span>':''}`)}
 </div>`}
function rControl(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},ord=m.order||[],names=ord.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!ord.includes(n)&&H[n].up!=null));
 const F=m.fleet||{},S=F.series||{},I=F.infra||{};
 let tps=0,run=0,wait=0,models=0,gpuW=0;
 names.forEach(n=>{const L=(H[n]||{}).llm||{};(L.servers||[]).filter(x=>x.model).forEach(x=>{tps+=x.gen_tps||0;run+=x.running||0;wait+=x.waiting||0;models++});gpuW+=(H[n]||{}).gpu_power||0});
 const tokAll={};Object.values(S.tokens||{}).forEach(pts=>pts.forEach(([t,v])=>{tokAll[t]=(tokAll[t]||0)+(v||0)}));
 const tokPts=Object.entries(tokAll).map(([t,v])=>[+t,v]).sort((a,b)=>a[0]-b[0]);
 const upN=names.filter(n=>H[n].up).length;
 const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u>ms</span>':v.toFixed(1)+'<span class=u>s</span>';
 const kv=(k,v)=>`<div><span class=k>${k}</span><span class=v>${v}</span></div>`;
 const head=`<div class=hero>
  <div class=hv data-ghost="${ghost(Math.round(tps))}">${Math.round(tps)}</div>
  <div class=hl>tokens / second across the fleet<br><b>${models}</b> model${models==1?'':'s'} serving on <b>${upN}</b> of ${names.length} nodes${I.llm_total&&I.llm_up<I.llm_total?` · <span class=bad>${I.llm_total-I.llm_up} endpoint down</span>`:''}</div>
  <div class=stats>
   ${statTile('requests',run,' running',`${wait} waiting`,S.requests)}
   ${statTile('gpu power',Math.round(gpuW),' W',I.gpu_wh_24h!=null?(I.gpu_wh_24h/1000).toFixed(1)+' kWh / 24 h':'',S.gpu_power)}
   ${statTile('generated, 24 h',esc(fmtK(I.tokens_24h)),' tok',`${Math.round(I.requests_24h||0)} requests`,tokPts)}
   ${statTile('network',esc(fmtBits((S.net_tx&&S.net_tx.length?S.net_tx[S.net_tx.length-1][1]:0))),'',`↓ ${esc(fmtBits(S.net_rx&&S.net_rx.length?S.net_rx[S.net_rx.length-1][1]:0))}`,S.net_tx)}
   ${statTile('health',`${I.backups_ok??'—'}<span class=u>/${I.backups_total??'—'}</span>`,'',`backups · ${I.alerts?'<a class="warn go" href="/#panel=alerts" onclick="event.stopPropagation()" style="color:var(--warn)">'+I.alerts+' alert'+(I.alerts>1?'s':'')+' →</a>':'no alerts'} · raid ${(I.raid||[]).filter(r=>r.array=='md2').map(r=>r.active+'/'+((I.raid_total||{})[r.array]||'?')).join('')||'—'}${(I.raid_sync||{}).md2!=null?' <span class=warn>'+Math.round(I.raid_sync.md2)+'%</span>':''}`)}
  </div></div>`;
 const rows=names.map(n=>{const M=H[n],L=M.llm||{},sv=(L.servers||[]).filter(x=>x.model),s0=sv[0],sp=M.spark||{};
  const memP=M.mem_total?100*M.mem_used/M.mem_total:0,state=M.up===0?'bad':(M.failed_units||M.reboot||(s0&&s0.kv_pct>90))?'warn':'ok';
  const gcls=v=>v>90?'bad':v>75?'warn':'';
  const tcls=v=>v==null?'':v>=88?'bad':v>=78?'warn':'',deg=v=>v!=null?fmtT(v,1):'';
  const ttl=`cpu/soc ${fmtT(M.sys_temp)} · gpu ${fmtT(M.gpu_temp)}${M.nvme_temp!=null?' · nvme '+fmtT(M.nvme_temp):''}`;
  const hy=M.hygiene||{};
  const flags=[M.failed_units?goA({host:n,panel:'hygiene'},'warn',M.failed_units+' failed','failed units — fix in Hygiene'):'',M.reboot?goA({host:n,panel:'hygiene'},'warn','reboot','reboot required — Hygiene'):'',M.up===0?goA({host:n,panel:'detail',tab:'health'},'bad','no metrics','exporter down — Machine Report › health'):'',L.up===0&&sv.length?goA({host:n,panel:'detail',tab:'llm'},'bad','llm down','Machine Report › llm'):'',hy.items?goA({host:n,panel:'hygiene'},hy.warnings?'warn':'',hy.items+' hyg',hy.items+' housekeeping findings — Hygiene'):''].join('');
  const stats=s0?`<div class=nst>
    ${kv('run · wait',`${s0.running??0} · ${s0.waiting??0}`)}
    ${kv('throughput',`${Math.round(s0.gen_tps||0)}<span class=u> tok/s</span>`)}
    ${kv('decode',s0.decode_tps!=null?`${Math.round(s0.decode_tps)}<span class=u> tok/s</span>`:'—')}
    ${kv('prefill',s0.prefill_tps!=null?`${esc(fmtK(s0.prefill_tps))}<span class=u> tok/s</span> · ${ms(s0.prefill_p50)}`:'—')}
    ${kv('ttft · e2e',`${ms(s0.ttft_p50)} · ${ms(s0.e2e_p50)}`)}
    ${kv('in → out',s0.prompt_tok_p50!=null?`${esc(fmtK(s0.prompt_tok_p50))} → ${esc(fmtK(s0.gen_tok_p50||0))}`:'—')}
    ${kv('queue · itl',`${ms(s0.queue_p50)} · ${s0.itl_p50!=null?Math.round(s0.itl_p50*1000)+'<span class=u>ms</span>':'—'}`)}
    ${kv('last hour',`${Math.round(s0.requests_1h||0)}<span class=u> req</span> · ${esc(fmtK(s0.tokens_1h||0))}`)}
    ${kv('prefix · kv',`${s0.prefix_hit!=null?Math.round(s0.prefix_hit):'—'}<span class=u>%</span> · <span class="${gcls(s0.kv_pct)}">${s0.kv_pct!=null?Math.round(s0.kv_pct):'—'}<span class=u>%</span></span>`)}
   </div>`
   :L.worker_for?`<div class="nst wk">
    <div class=full><span class=k>tp model</span><span class=v>${esc(L.worker_model||'—')}</span></div>
    ${kv('fabric',`↑${esc(fmtBits(M.fabric_tx))} ↓${esc(fmtBits(M.fabric_rx))}`)}
    ${kv('net',`↑${esc(fmtBits(M.net_tx))} ↓${esc(fmtBits(M.net_rx))}`)}
    ${kv('disk io',`${esc(fmtBytes(M.disk_io))}<span class=u>/s</span>`)}
    ${kv('root',`${Math.round(M.root_pct||0)}<span class=u>%</span>`)}
   </div>`
   :`<div class="nst wk">${kv('llm','none')}${kv('disk io',`${esc(fmtBytes(M.disk_io))}<span class=u>/s</span>`)}${kv('root',`${Math.round(M.root_pct||0)}<span class=u>%</span>`)}</div>`;
  return `<div class="nrow ${state}" onclick="location.href='/#'+encodeURIComponent('${esc(n)}')" title="open in the dashboard" style="cursor:pointer">
   <div class=nid><div class=nm><span class="dot ${M.up?'ok':''}"></span>${esc(n)}${flags?`<span class=fl>${flags}</span>`:''}<span class=tjob>${M.gpu_power!=null?Math.round(M.gpu_power)+' W':''}</span></div>
    <div class=md title="${esc(s0?s0.model:L.worker_model||'')}">${s0?esc(s0.model):L.worker_for?'worker for '+esc(L.worker_for):'—'}</div>
    <div class=md>load ${M.load1??'—'}${M.containers?' · '+M.containers+' ctr':''}</div></div>
   <div class=nrings title="${esc(ttl)}">
    ${ring(M.gpu_util,'gpu',`${M.gpu_util!=null?Math.round(M.gpu_util)+'%':'—'}`,'a',`gpu <b class="${tcls(M.gpu_temp)}">${deg(M.gpu_temp)}</b>`)}
    ${ring(M.cpu,'cpu',`${M.cpu!=null?Math.round(M.cpu)+'%':'—'}`,gcls(M.cpu)||'',`cpu <b class="${tcls(M.sys_temp)}">${deg(M.sys_temp)}</b>`)}
    ${ring(memP,'mem',`${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}`,'')}
    ${s0?ring(s0.kv_pct,'kv',`${s0.kv_pct!=null?Math.round(s0.kv_pct)+'%':'—'}`,gcls(s0.kv_pct)||'ok'):ring(M.root_pct,'root',`${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}`,gcls(M.root_pct)||'ok')}
   </div>
   ${stats}
   <div class=nch>
    ${chart('cpu · gpu',[{name:'cpu',pts:sp.cpu,cls:'c0'},{name:'gpu',pts:sp.gpu_util,cls:'a'}],pct,{max:100,span:'30m',compact:1})}
    ${s0?chart('tokens/s',[{pts:(s0.spark||{}).gen_tps,cls:slotOf(s0.model)}],v=>Math.round(v)+' tok/s',{span:'30m',compact:1})
        :chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})}
    ${s0?chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})
        :L.worker_for?chart('fabric (200G)',[{name:'↑',pts:sp.fabric_tx,cls:'g'},{name:'↓',pts:sp.fabric_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})
        :chart('memory',[{pts:sp.mem_used,cls:'c6'}],fmtBytes,{max:M.mem_total||undefined,span:'30m',compact:1})}
   </div></div>`}).join('');
 const roster=(F.roster||[]).map(r=>`<span><b>${esc(r.model||'?')}</b> ${esc(r.host)}${r.workers&&r.workers.length?'+'+esc(r.workers.join('+')):''}${r.tp?' · tp'+r.tp:''}${r.max_model_len?' · '+esc(fmtK(r.max_model_len))+' ctx':''}${r.kv_tokens!=null?' · '+esc(fmtK(r.kv_tokens))+' kv':''}${r.gpu_mem_util!=null?' · '+Math.round(r.gpu_mem_util*100)+'% gpu mem':''}${r.started_ts?' · up '+esc(fmtDur(Date.now()/1000-r.started_ts)):''}</span>`).join('');
 const hot=k=>names.filter(n=>H[n][k]!=null).sort((a,b)=>H[b][k]-H[a][k])[0];
 const hotS=[['sys_temp','cpu'],['gpu_temp','gpu'],['nvme_temp','nvme']].map(([k,l])=>{const n=hot(k);if(!n)return '';const v=H[n][k];
   return `${l} <b class="${v>=88?'bad':v>=78?'warn':''}">${fmtT(v,1)}</b> ${esc(n)}`}).filter(Boolean).join(' · ');
 const foot=`<div class=foot>${roster}${hotS?`<span>hottest ${hotS}</span>`:''}<span>__NAS__ <b>${I.nas_free!=null?esc(fmtBytes(I.nas_free)):'—'}</b> free · repo endpoint <b class="${I.rest_up?'ok':'bad'}">${I.rest_up?'up':'DOWN'}</b> · exporters <b>${I.exporters_up??'?'}/${I.exporters_total??'?'}</b> · llm endpoints <b>${I.llm_up??'?'}/${I.llm_total??'?'}</b> · backups <b>${I.backups_ok??'?'}/${I.backups_total??'?'}</b> · alerts <b class="${I.alerts?'warn':'ok'}">${I.alerts??0}</b></span></div>`;
 return `<div class=ctl>${head}${rows}${foot}</div>`}
function rNodes(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},order=m.order||(OVR&&OVR.hosts||[]).map(h=>h.name);
 const names=order.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!order.includes(n)&&H[n].up!=null));
 return `<div class=tiles style="grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr))">`+names.map(n=>{
  const M=H[n],L=M.llm||{},sv=(L.servers||[]).filter(s=>s.model),s0=sv[0],sp=M.spark||{};
  const memP=M.mem_total?100*M.mem_used/M.mem_total:0;
  const state=M.up===0?'bad':(M.failed_units||M.reboot||(s0&&s0.kv_pct>90))?'warn':'ok';
  const gcls=v=>v>90?'bad':v>75?'warn':'';
  return `<div class="tile nc ${state}" onclick="location.href='/#'+encodeURIComponent('${esc(n)}')" title="open in the dashboard">
   <div class=nm><span class="dot ${M.up?'ok':''}"></span>${esc(n)}<span class=tjob>${M.gpu_power!=null?Math.round(M.gpu_power)+' W':''}</span></div>
   <div class=sub>${s0?'<b style="color:var(--fg)">'+esc(s0.model)+'</b>'+(L.up===0?' <span class="tag bad">down</span>':''):L.worker_for?'TP worker for <b>'+esc(L.worker_for)+'</b>'+(L.worker_model?' · '+esc(L.worker_model):''):'no LLM server'}${M.load1!=null?' · load '+M.load1:''}</div>
   <div class=rings>
    ${ring(M.gpu_util,'gpu',`${M.gpu_util!=null?Math.round(M.gpu_util)+'%':'—'}<small>${M.gpu_temp!=null?fmtT(M.gpu_temp):''}</small>`,'a')}
    ${ring(M.cpu,'cpu',`${M.cpu!=null?Math.round(M.cpu)+'%':'—'}<small>load ${M.load1??'—'}</small>`,gcls(M.cpu)||'')}
    ${ring(memP,'memory',`${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}<small>of ${Math.round((M.mem_total||0)/1e9)}G</small>`,'')}
    ${s0?ring(s0.kv_pct,'kv cache',`${s0.kv_pct!=null?Math.round(s0.kv_pct)+'%':'—'}<small>${s0.running??0} run</small>`,gcls(s0.kv_pct)||'ok')
        :ring(M.root_pct,'root',`${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}<small>${esc(fmtBytes(M.disk_io))}/s</small>`,gcls(M.root_pct)||'ok')}
   </div>
   ${sv.map(s=>llmStats(s)).join('')}${!sv.length&&L.worker_for?`<div class=mut style="font:10.5px var(--mono);margin:4px 0">serves half of ${esc(L.worker_model||'the model')} for ${esc(L.worker_for)} — request stats are on that node</div>`:''}
   <div class=charts>
    ${chart('cpu',[{pts:sp.cpu}],pct,{max:100,span:'30m'})}
    ${chart('gpu',[{pts:sp.gpu_util,cls:'a'}],pct,{max:100,span:'30m'})}
    ${chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m'})}
    ${s0?chart('tokens/s',[{pts:(s0.spark||{}).gen_tps,cls:slotOf(s0.model)}],v=>Math.round(v)+' tok/s',{span:'30m'})
        :chart('requests',[{pts:[]}],v=>v,{span:'30m'})}
   </div>
   <div class=flags>${s0?`<span class=tag>root ${Math.round(M.root_pct||0)}%</span>`:''}${M.disk_io!=null?`<span class=tag>io ${esc(fmtBytes(M.disk_io))}/s</span>`:''}${M.failed_units?`<span class="tag warn">${M.failed_units} failed unit${M.failed_units>1?'s':''}</span>`:''}${M.reboot?'<span class="tag warn">reboot needed</span>':''}${M.containers?`<span class=tag>${M.containers} ctr</span>`:''}${s0&&s0.ttft_p50!=null?`<span class=tag>ttft ${s0.ttft_p50<1?Math.round(s0.ttft_p50*1000)+' ms':s0.ttft_p50.toFixed(1)+' s'}</span>`:''}</div>
  </div>`}).join('')+'</div>'}
function loadLayout(){try{return JSON.parse(localStorage.getItem(LS))||{}}catch(e){return {}}}
function saveLayout(){const L=Object.assign({},loadLayout());
 L.order=PANELS.map(p=>p.id);
 for(const p of PANELS){const el=$('p-'+p.id);if(!el)continue;
  const sp=parseInt(el.style.gridColumn.replace(/\D/g,''))||p.sp;
  // height is only rewritten while expanded — a collapsed panel (64px) must
  // not clobber the height it restores to when reopened
  const prev=L[p.id]||{};
  L[p.id]={sp, h: !p.h?0 : el.classList.contains('collapsed') ? (prev.h||p.h) : el.offsetHeight,
              c: el.classList.contains('collapsed')}}
 localStorage.setItem(LS,JSON.stringify(L))}
function layoutReset(){localStorage.removeItem(LS);location.reload()}

function applyGrid(el,sp){el.style.gridColumn=`span ${clamp(sp,1,12)}`}

function buildPanels(){
 const desk=$('desk'),L=loadLayout();desk.innerHTML='';
 const order=(L.order||[]).filter(id=>PANELS.some(p=>p.id==id));
 const list=order.map(id=>PANELS.find(p=>p.id==id))
  .concat(PANELS.filter(p=>!order.includes(p.id)));
 for(const p of list){
  const l=L[p.id]||{};
  const el=document.createElement('div');el.className='panel';el.id='p-'+p.id;
  applyGrid(el,l.sp||p.sp);
  if(p.h)el.style.height=Math.max(l.h||p.h,120)+'px';else{   /* clamp: a stale/tiny saved height must not hide the body */el.classList.add('auto');el.style.removeProperty('height');el.style.removeProperty('width')}
  if(l.c)el.classList.add('collapsed');
  el.innerHTML=`<div class=grip><span class=dots><i></i><i></i><i></i><i></i></span>
   <h2>${p.title}</h2><span class=st id="st-${p.id}"></span>
   <button class=mini title=collapse onclick="toggleP('${p.id}')">${l.c?'▸':'▾'}</button></div>
   <div class=pbody id="b-${p.id}"><div class=empty>loading…</div></div>`;
  desk.appendChild(el);
  dragify(el,el.querySelector('.grip'));
 }
}
function toggleP(id){const el=$('p-'+id);el.classList.toggle('collapsed');
 const c=el.classList.contains('collapsed');el.querySelector('.mini').textContent=c?'▸':'▾';
 const ph=PANELS.find(x=>x.id==id).h;el.style.height=c?'64px':ph?Math.max((loadLayout()[id]||{}).h||ph,120)+'px':'';
 if(!c){const p=PANELS.find(x=>x.id==id);if(p._data!==undefined)repaint(id,p._data);else tick(p)}
 saveLayout()}
/* drag to REORDER: the dragged panel ghosts; dropping over another panel
   inserts before/after it depending on cursor position. The grid re-snaps. */
function dragify(el,grip){
 grip.addEventListener('pointerdown',e=>{
  if(e.target.closest('button'))return;
  e.preventDefault();
  const desk=$('desk');
  el.classList.add('dragging');
  let target=null,before=false;
  try{grip.setPointerCapture(e.pointerId)}catch(_){}
  const clear=()=>{document.querySelectorAll('.drop-before,.drop-after')
   .forEach(n=>n.classList.remove('drop-before','drop-after'));target=null};
  const move=ev=>{
   clear();
   const under=document.elementFromPoint(ev.clientX,ev.clientY);
   const t=under&&under.closest?under.closest('.panel'):null;
   if(t&&t!=el){
    const r=t.getBoundingClientRect();
    before=(ev.clientX-r.left)<r.width/2;
    target=t;t.classList.add(before?'drop-before':'drop-after');
   }
  };
  const up=ev=>{
   grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',up);
   grip.removeEventListener('pointercancel',up);
   el.classList.remove('dragging');
   if(target){
    // honor the before/after computed by the last move (already correct for
    // the CURRENT target; recomputing from pointerup coords can disagree)
    if(before)target.before(el);else target.after(el)}
   clear();
   // keep PANELS order in sync with DOM order, then persist
  PANELS.sort((a,b)=>{const A=$('p-'+a.id),B=$('p-'+b.id);
    return A&&B?(A.compareDocumentPosition(B)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1):0});
   saveLayout()};
  grip.addEventListener('pointermove',move);grip.addEventListener('pointerup',up);grip.addEventListener('pointercancel',up);
 });
 /* resize (native corner, vertical only — width is the grid's to own):
    persist the new height */
 el.addEventListener('mouseup',()=>{
  if(el.classList.contains('collapsed'))return;
  saveLayout();
 });
}

async function fetchJSON(u,auth){const h={'Accept':'application/json'};if(auth)h.Authorization='Bearer '+(localStorage.getItem(LS_TOK)||'');
 const r=await fetch(u,{headers:h});
 buildCheck(r);
 if(r.status==401&&ME&&ME.mode=='accounts'){location.href='/login?next='+encodeURIComponent(location.pathname+location.hash);throw new Error('sign in')}
 if(!r.ok)throw new Error(u+' -> '+r.status);return r.json()}
/* the server stamps every response with its build; a page from an older build reloads itself once
   (otherwise a tab left open across a UI restart keeps running stale code with no hint) */
function buildCheck(r){const b=r.headers.get('X-CM-Build');if(!b||b==BUILD)return;
 let last='';try{last=sessionStorage.getItem('noctyx_reloaded_for')||''}catch(e){}
 if(last==b)return;                                   /* already reloaded for this build: don't loop */
 try{sessionStorage.setItem('noctyx_reloaded_for',b)}catch(e){}
 location.reload()}

async function tick(p){
 if(p.int<=0)return;
 if(p.id=='schedule'&&EDIT)return;
 if(p.id=='recipes'&&(RCJOB&&!RCJOB.done||document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#b-recipes')&&/INPUT|SELECT/.test(document.activeElement.tagName)))return;
 if(p.id=='config'&&(CFGDIRTY||(CFGJOB&&/running|starting/.test(CFGJOB.text.split('\n')[0]))))return;
 const el=$('p-'+p.id);if(!el||el.classList.contains('collapsed'))return;
 const body=$('b-'+p.id);
 const slow=(body&&p._data!==undefined)?setTimeout(()=>body.classList.add('stale'),1200):0;
 try{
  const data=await p.api();p._data=data;repaint(p.id,data);p._painted=true;
  const st=$('st-'+p.id);if(st&&data){st.textContent=ago(data.ts||Date.now()/1000);st.removeAttribute('title');st.classList.add('live')}
 }catch(e){if(body&&p._painted)body.classList.add('stale');
  /* the reason is one hover away: a renderer exception or a failed fetch, with where it happened */
  const why=String((e&&e.stack)||e).split('\n').slice(0,2).join(' ').slice(0,300);
  console.error('panel '+p.id+':',e);
  const st=$('st-'+p.id);if(st){st.textContent='err';st.title=why;st.classList.remove('live')}
  if(body&&!p._painted&&VIEW!='kiosk')body.innerHTML=`<div class="empty bad">panel failed: ${esc(why.split(' at ')[0])}<br><span class=mut>details on the “err” pill · retries in ${p.int}s</span></div>`}
 finally{clearTimeout(slow)}
}
/* in-place DOM patch: the renderer stays a pure string function, but the live tree is
   diffed against the new markup and only changed text/attributes are touched. No teardown,
   no re-layout of untouched charts, no flash — and scroll positions/focus survive a tick. */
function morph(a,b){
 if(a.nodeType!==b.nodeType||a.nodeName!==b.nodeName){a.replaceWith(b);return}
 if(a.nodeType===3||a.nodeType===8){if(a.data!==b.data)a.data=b.data;return}
 if(a.nodeType!==1)return;
 for(const at of Array.from(a.attributes))if(!b.hasAttribute(at.name))a.removeAttribute(at.name);
 for(const at of Array.from(b.attributes))if(a.getAttribute(at.name)!==at.value)a.setAttribute(at.name,at.value);
 const tn=a.nodeName;
 if((tn==='INPUT'||tn==='TEXTAREA'||tn==='SELECT')&&document.activeElement!==a&&a.value!==b.value)a.value=b.value;
 if(tn==='TEXTAREA'||tn==='SCRIPT'||tn==='STYLE'){if(a.textContent!==b.textContent)a.textContent=b.textContent;return}
 morphKids(a,b)}
function morphKids(a,b){const ac=a.childNodes,bc=Array.from(b.childNodes);
 for(let i=0;i<bc.length;i++){const x=ac[i],y=bc[i];
  if(!x){a.appendChild(y);continue}
  if(x.nodeType===1&&y.nodeType===1&&(x.id||y.id)&&x.id!==y.id){x.replaceWith(y);continue}
  morph(x,y)}
 while(ac.length>bc.length)a.removeChild(a.lastChild)}
function repaint(id,data){const p=PANELS.find(x=>x.id==id),b=$('b-'+id);if(!b||!p)return;
  /* the kiosk's fit lives in inline styles on the rendered tree, and morph() clears style
     attributes the fresh markup doesn't carry — snapshot them and put them back. But not
     across a page switch (rotation): the old page's column/row fit must not squash the new one. */
  const prevW=b.querySelector('.kiosk-wrap'),prevPg=prevW?prevW.dataset.page:null;
  const pageSw=id=='kiosk'&&prevPg!=null&&prevPg!=String(kioskPage());
  const fit=id=='kiosk'&&!pageSw?kioskFitSnap(b):null;
 if(id=='kiosk'&&_kfSaved)kfStyle(_kfSaved);    /* morph() can't restore <html>'s class: the step it
                                                   chose would evaporate on the next 10s refresh */
 const t=document.createElement('template');t.innerHTML=p.render(data)||'<div class=empty>no data</div>';
 morphKids(b,t.content);b.classList.remove('stale');
 /* morph matches children positionally: a kiosk page whose markup shifted (a page change, an
    alert cleared) is a different screen — swap wholesale, then let the fit re-solve */
 if(id=='kiosk'){const cur=b.firstElementChild,nw=t.content.firstElementChild;
  if(cur&&nw&&(cur.className!==nw.className||(cur.dataset.page||'')!==(nw.dataset.page||'')))b.replaceChildren(nw)}
 if(id=='kiosk'){kioskFitRestore(b,fit);kioskFit()}
 for(const k in CH)if(!document.querySelector(`[data-ch="${k}"]`))delete CH[k];
 if(id=='actions')wireActions()}
function kioskFitSnap(b){const w=b.querySelector('.kiosk-wrap'),n=b.querySelector('.knodes');
 return w||n?{wrap:w&&w.style.transform,boxW:w&&w.style.width,boxH:w&&w.style.height,nodes:n&&n.getAttribute('style'),cls:n&&n.className,page:w&&w.dataset.page,pages:w&&w.dataset.pages,kf:_kfSaved}:null}
function kioskFitRestore(b,f){if(!f)return;
 const w=b.querySelector('.kiosk-wrap'),n=b.querySelector('.knodes');
 if(w&&f.wrap!=null)w.style.transform=f.wrap;
 if(w&&f.boxW!=null)w.style.width=f.boxW;        /* the box goes with the transform: scale() on a */
 if(w&&f.boxH!=null)w.style.height=f.boxH;       /* 100vw wrap would paint a corner of the screen */
 if(w&&f.page!=null)w.dataset.page=f.page;
 if(w&&f.pages!=null)w.dataset.pages=f.pages;
 if(f.kf)kfStyle(f.kf);                          /* the step <html> lost with the old tree */
 if(n&&f.nodes!=null)n.setAttribute('style',f.nodes);
 if(n&&f.cls)n.className=f.cls}
/* /kiosk: everything on one screen, always. The wrap is exactly the viewport (body overflow:hidden)
   and kioskFit() packs the node grid into the height left under the header and the slots: for each
   (type step, column count) it measures the cards' intrinsic height in a hidden in-DOM clone of the
   wrap — the live grid can't be measured (its rows are stretched by the flex container) and a bare
   detached tree resolves no inherited --kf-h and none of the theme selectors — then keeps the
   roomiest step whose rows fit full-size, shares the leftover height between them, and falls back
   to scaling the whole wrap when even the tightest step overflows. The three type steps
   come from --kf-h: all the kiosk CSS above derives from it, and kfStyle steps the ROOT — morph()
   can patch neither body's class list nor <html>, and a root class inherits into the probe's clone
   too. One fit per frame; if the applied tree still overflows its rows (a late theme class, a font
   load, a repaint mid-solve), the settled check refits, guard-bounded to converge. */
const KF={1:.78,2:.62};
/* The wall is painted at scale S: the wrap is laid out in a box of viewport/S and
   transform:scale(S)d from the top left, so it always covers exactly the viewport — type,
   gaps and the rows the solver measured all scale together. S = T x shrink:
   - T, the type scale, grows the wall on viewports taller than 1080p (a 4K panel at 1x, a
     zoomed-out browser), where fixed-px type is tiny on the physical screen. The smaller
     dimension sets it: a wall's room is its height. ?kf= pins it per browser.
   - shrink (< 1) is the last resort when even the smallest type step overflows. It widens
     and heightens the box by the same factor, so the shrunk wall still fills the screen
     (scaling a viewport-sized box would leave it in the top-left corner). */
const kioskT=(w,h)=>{const q=new URLSearchParams(location.search);
 if(q.get('kf'))return Math.max(.5,Math.min(3,+q.get('kf')||1));
 return Math.max(1,Math.min(2.2,Math.min(w,h)/1080))};
function kioskBox(wrap,S){wrap.style.width=(innerWidth/S)+'px';wrap.style.height=(innerHeight/S)+'px';
 wrap.style.transformOrigin='top left';wrap.style.transform=S==1?'':`scale(${S.toFixed(4)})`}
let _kfSaved=0,_fitPend=0,_fitPass=0,_lastFit=null;
function kfStyle(s){_kfSaved=s;document.documentElement.classList.toggle('kiosk-1',s==1);document.documentElement.classList.toggle('kiosk-2',s==2)}
function kioskFit(){
if(_fitPend||!document.querySelector('.kiosk-wrap'))return;   /* not on /kiosk: nothing to size */
_fitPend=true;
/* the fit is inline styles on a tree we morph in place: re-solving it before the browser has
   painted the morphed tree compares a half-updated layout and writes back a different answer
   every few ticks — a 4px reflow of the whole wall, which reads as a flash. One settled paint
   of the new markup is enough; two rAFs guarantee the frame between them gets painted. */
requestAnimationFrame(()=>requestAnimationFrame(()=>{setTimeout(()=>{_fitPend=false;kioskFitNow();
 /* one follow-up pass in the same task settles the shrink↔scroll feedback: a fit that shrinks
    type also shrinks the content it then has to fit, so a single solve can land short */
 kioskFitNow()},32)}))}
function kioskFitNow(){
 const wrap=document.querySelector('.kiosk-wrap');if(!wrap)return;
 if(kioskCfg().fit=='scroll'&&!kioskCfg().rotation){kfStyle(0);      /* natural size: no box, no scale */
  wrap.style.width=wrap.style.height=wrap.style.transform='';return}
 const T=kioskT(innerWidth,innerHeight),W=innerWidth/T,H=innerHeight/T;
 kioskBox(wrap,T);
 const nodes=wrap.querySelector('.knodes');if(!nodes||!nodes.children.length){
  /* no node grid: a detail page (alerts/backups) in shrink mode — fill exactly the room the
     wrap has left (header and gaps already occupy their height in the 100vh column) */
  const det=wrap.querySelector('.kdetail');
  if(det){kfStyle(0);
   det.style.maxHeight=Math.max(120,wrap.clientHeight-det.offsetTop-Math.ceil(10*(document.documentElement.classList.contains('kiosk-2')?KF[2]:1)))+'px'}
  return}
 const n=nodes.children.length;if(!n){kfStyle(0);return}
 /* measure in a hidden clone of the whole wrap: the top row and the slot cards above the grid
    shrink with the step too, so they are part of the room. The clone must sit inside the DOM —
    a bare detached tree resolves no inherited custom properties (--kf-h) and none of the theme
    selectors that hang off body[data-view]/html[data-style]. The clone grid's box is measured,
    not assumed: 100vh on the wrap resolves inside the probe (overflow:hidden), then the grid's
    flex height falls out. Cards carry their style attribute through the clone — a previous
    fit's inline transform/position is cleared, and bar fills are stripped: they animate every
    tick and would jitter the measurement and re-trigger this fit forever. */
 const probe=document.createElement('div');
 probe.style.cssText=`position:fixed;visibility:hidden;left:-9999px;top:0;width:${W}px;height:${H}px;overflow:hidden`;
 document.body.appendChild(probe);
 const solve=s=>{
  kfStyle(s);
  probe.innerHTML='';
  const copy=wrap.cloneNode(true);
  copy.querySelectorAll('.kmeters .bar').forEach(b=>{b.className='barx';b.style.cssText='height:8px'});
  copy.querySelectorAll('.ktrace').forEach(s=>s.style.cssText='height:calc(64px*var(--kf-h,1))');   /* fixed, data-independent */
  copy.querySelectorAll('.kiosk-wrap,.knodes').forEach(e=>e.removeAttribute('style'));
  copy.querySelectorAll('.knodes>*').forEach(e=>{e.style.position='';e.style.transform=''});
  copy.style.width=W+'px';copy.style.height=H+'px';  /* the layout box at T (100vw is the viewport) */
  probe.appendChild(copy);
  const pn=copy.querySelector('.knodes');
  if(!pn||!pn.children.length)return null;
  /* an explicit column count (?cols=3 / the Kiosk setting) pins the grid: fewer, wider columns
     trade vertical space for horizontal — what a wall with rows to spare (or tall cards to
     read) wants. Clamped to the node count and to one card per column. */
  const want=kioskCfg().cols;
  let maxCols=Math.max(1,Math.min(n,(want>=1&&want<=8)?want:Math.floor((W+12)/262)));
  /* auto: the fewest columns that keep the same row count — 6 nodes in 4 columns is two
     rows with two holes; 3 columns is the same two rows with wider cards */
  if(!(want>=1&&want<=8))maxCols=Math.ceil(n/Math.ceil(n/maxCols));
  const kfh=s?KF[s]:1,bottom=Math.ceil(10*kfh);
  const fitc=(cols,rows)=>{const avail=pn.offsetHeight;
    const gw=pn.getBoundingClientRect().width;const colW=(gw-(cols-1)*12)/cols;
    const cells=[...pn.children];
    cells.forEach(c=>{c.style.position='absolute';c.style.width=colW+'px'});
    let cell=0;cells.forEach(c=>{cell=Math.max(cell,c.offsetHeight||c.scrollHeight)});
    cells.forEach(c=>{c.style.position='';c.style.width=''});
    if(!cell)return null;
    const nat=rows*cell+(rows-1)*12;
    if(nat<=avail+1)return {cols,rows,rh:(avail-(rows-1)*12)/rows,s,_slack:avail-rows*cell-(rows-1)*12,_cell:cell,_avail:avail};
    /* the widest we may go: shrink. The box grows by 1/scale, and only the grid needs the
       room — the header above it keeps its layout height — so H/(header+grid) fits it. */
    if(cols>=maxCols)return {cols,rows,rh:cell,s,scale:H/(H-avail+nat)};
    return null};
  for(let cols=maxCols;cols>=1;cols--){
   pn.style.gridTemplateColumns=`repeat(${cols},minmax(0,1fr))`;
   pn.style.gridAutoRows='';
   const m=fitc(cols,Math.ceil(n/cols));
   if(m)return m}
  return null};
 /* Type step: all three fit when the grid has rows to spare. Step 0 (kf=1) is the LARGEST
    type; it also demands the tallest rows, so the old first-wins scan from s=0 already
    picks the biggest font that fits — keep that. The mistake this fixes: with a pinned
    column count and a big screen (a 3x2 grid on a 2000x1010 wall), the rows were forced to
    the cards' natural height and the leftover just pooled into empty graph space — the wall
    looked sparse and "too small" (operator, 2026-10-23). At step 0, grow the rows instead:
    spread the slack across the rows, which stretches the trace (.ktrace flex:1) and every
    proportional size with it, and the wall fills the screen at the largest type.
    When no step fits, the one that paints largest after its shrink wins (step size x
    shrink): the full-size step overflows the most, so it is rarely that one. */
 let best=null;const eff=m=>(m.s?KF[m.s]:1)*(m.scale||1);
 for(const s of[0,1,2]){
  const m=solve(s);if(!m)continue;
  if(m.scale===undefined){if(!best||eff(m)>=eff(best))best=m;break}
  if(!best||eff(m)>eff(best))best=m}
 probe.remove();
 if(!best){kfStyle(0);return}
 /* a tick that re-solves the same page a pixel or two differently rewrites every card's
    height: the whole wall reflows and it reads as a flash (operator, 2026-10-22). Hysteresis:
    keep the last fit when the new one lands within 6px of it on the same step and columns. */
 best.vw=innerWidth;best.vh=innerHeight;
 if(_lastFit&&_lastFit.pg==pgKey()&&_lastFit.vw==innerWidth&&_lastFit.vh==innerHeight&&_lastFit.s==best.s&&_lastFit.cols==best.cols&&
    _lastFit.scale===best.scale&&Math.abs(_lastFit.rh-best.rh)<=6)best=_lastFit;
 _lastFit=best;
 kfStyle(best.s);                               /* settle the step before measuring the live tree */
 nodes.style.gridTemplateColumns=`repeat(${best.cols},minmax(0,1fr))`;
 /* shrunk: the enlarged box has room to spare below the measured rows; share it out */
 nodes.style.gridAutoRows=best.scale?'minmax(0,1fr)':best.rh.toFixed(1)+'px';
 const S=T*(best.scale?Math.min(1,Math.max(.3,best.scale)):1);
 kioskBox(wrap,S);
 /* settle: if the applied tree's content still overflows its rows (a late theme class, a font
    that loaded mid-solve, stretched heights lying to the probe), refit from the true state —
    guard-bounded, the fit converges in one or two extra passes */
 const box=nodes.firstElementChild.getBoundingClientRect().height/S;
 if(++_fitPass<5&&[...nodes.children].some(c=>c.scrollHeight>box+2))kioskFit();else _fitPass=0}
if(VIEW=='kiosk'){addEventListener('resize',kioskFit);new MutationObserver(kioskFit).observe($('desk'),{childList:true,subtree:true})}

/* ── renderers (pure: data -> html) ── */
/* live tile body from Prometheus (fleet_metrics) */
function tileLive(M,up){const sp=M.spark||{};
 const memP=M.mem_total?100*M.mem_used/M.mem_total:0;
 const fl=[M.up===0?'<span class="tag bad">exporter down</span>':'',
  M.failed_units?`<span class="tag warn" title="failed systemd units — see Health tab">${M.failed_units} failed unit${M.failed_units>1?'s':''}</span>`:'',
  M.reboot?'<span class="tag warn" title="/var/run/reboot-required">reboot needed</span>':'',
  M.containers?`<span class=tag title="running Docker containers">${M.containers} ctr</span>`:''].join('');
 return `<div class=mgrid>
  <div class=mcell data-k=cpu><span>cpu</span><span class=mv>${pct(M.cpu)} · load ${M.load1??'—'}</span>${spark([{pts:sp.cpu}],{max:100})}</div>
  <div class=mcell data-k=gpu><span>gpu</span><span class=mv>${pct(M.gpu_util)}${M.gpu_temp!=null?' · '+fmtT(M.gpu_temp):''}${M.gpu_power!=null?' · '+Math.round(M.gpu_power)+'W':''}</span>${spark([{pts:sp.gpu_util,cls:'a'}],{max:100})}</div>
  <div class=mcell data-k=net><span>net</span><span class=mv>↑${fmtBits(M.net_tx)} ↓${fmtBits(M.net_rx)}</span>${spark([{pts:sp.net_tx,cls:'g'},{pts:sp.net_rx,cls:'c0'}])}</div>
 </div>
 <div class=gauges>
  <div class=gauge data-k=mem><span>mem</span>${bar(memP,true,'mem')}<span>${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}</span></div>
  <div class=gauge data-k=disk><span>disk</span>${bar(M.root_pct,false,'disk')}<span>${pct(M.root_pct)}</span></div>
 </div>
 ${llmLine(M.llm)}
 <div class=flags><span class=tnext>up ${esc(up.trim().slice(0,12))}</span>${fl}</div>`}
function llmLine(L){if(!L)return '';
 const sv=(L.servers||[]).filter(s=>s.model);
 if(!sv.length)return L.worker_for?`<div class=mcell><span>llm</span><span class="mv wk">TP worker for ${esc(L.worker_for)} · ${esc(L.worker_model||'')}</span><span></span></div>`:'';
 return sv.map(s=>`<div class=mcell data-k=llm><span>llm</span><span class=mv title="${esc(s.model)}">${esc(s.model)} · ${s.running??0} run${s.waiting?' / '+s.waiting+' wait':''} · ${Math.round(s.gen_tps||0)} tok/s${s.kv_pct!=null?' · kv '+Math.round(s.kv_pct)+'%':''}</span>${spark([{pts:(s.spark||{}).gen_tps,cls:'a'}])}</div>`).join('')}
/* the host's primary scheduled profile (system if scheduled) */
function schedRow(s,host){const rs=((s&&s.schedule&&s.schedule.rows)||[]).filter(r=>r.host==host&&r.enabled);
 return rs.find(r=>r.profile=='system')||rs[0]||null}
function rpoBadge(r,sn,h){
 if(!r&&h&&h.monitored_only)return `<span class=snapbadge title="monitored only: metrics and facts, no backup by design (site config backup:false)">monitored</span>`;
 const go=h&&h.name?`onclick="event.stopPropagation();goTo(${esc(JSON.stringify({host:h.name,panel:'detail',tab:'backups'}))})"`:'';
 if(!r){const n=(sn&&sn.count)||0;return `<span class="snapbadge go ${n?'warn':'bad'}" title="not scheduled — open backups" ${go}>unscheduled</span>`}
 const cls=HCLS[r.health]||'warn';
 const txt=r.last_ts?`${esc(r.profile)} ${fmtDur(Date.now()/1000-r.last_ts).split(' ')[0]}`:`no ${esc(r.profile)}`;
 return `<span class="snapbadge go ${cls}" title="${esc(r.profile)}: last ${esc(fmtTs(r.last_ts))} · ${esc(r.health)} · every ${esc(fmtDur(r.every))} — open backups" ${go}>${txt}</span>`}
function rFleet(s){
 if(!s)return OVR?'':'<div class=empty>loading…</div>';
 OVR=s;
 if(SEL&&!(s.hosts||[]).some(h=>h.name==SEL)){SEL=null;localStorage.removeItem(LS_SEL);history.replaceState(null,'',location.pathname);tick(PANELS.find(x=>x.id=='detail'))}
 $('hsrv').textContent=`${s.server} · ${s.hosts.length} nodes`;
 $('hauth').textContent=s.demo?'demo · sample data':s.auth_required?'auth on':'dev mode';
 $('hauth').className='hpill'+(s.auth_required?'':' warn');
 const rows=((s.schedule&&s.schedule.rows)||[]).filter(r=>r.enabled),good=rows.filter(r=>r.health=='ok').length;
 $('hrpo').innerHTML=rows.length?`backups ${good}/${rows.length}<span class=hlong> on schedule</span>`:'no schedule';
 $('hrpo').className='hpill go '+(rows.length&&good==rows.length?'ok':good<rows.length/2?'bad':'warn');
 return `<div class=tiles>`+s.hosts.map(h=>{
  const f=(s.facts&&s.facts[h.name])||{},sn=(s.snapshots&&s.snapshots.hosts||{})[h.name]||{};
  const M=((s.metrics&&s.metrics.hosts)||{})[h.name];
  const n=sn.count||0,last=sn.last||'';
  const ok=!f.error,up=(f.uptime_lines&&f.uptime_lines[0]||'').replace('up','')||'—';
  const memP=f.mem_total_gb?100*f.mem_used_gb/f.mem_total_gb:0;
  const rootd=(f.disks||[]).filter(d=>d.mnt=='/');
  const diskP=rootd.length?parseInt(rootd[0].pct)||0:0;
  const g=f.gpu_stats,gg=g&&g.temp_c!=null?g:null,tmp=gg?clamp(Math.round(gg.temp_c*100/95),0,100):0;
  return `<div class="tile${SEL==h.name?' sel':''}${runningOn(h.name)?' busy':''}" onclick="selectHost('${esc(h.name)}')">
   ${rpoBadge(schedRow(s,h.name),sn,h)}
   <div class=nm><span class="dot ${ok?'ok':''}"></span>${esc(h.name)}<span class="tjob go" data-jb="${esc(h.name)}" title="open Runs" onclick="event.stopPropagation();goTo(${esc(JSON.stringify({host:h.name,panel:'runs'}))})">${jobBadge(h.name)}</span></div>
   <div class=sub>${esc(f.address||'')} · ${esc((f.os_lines&&f.os_lines[0]||'—').replace('Ubuntu ','Ubu '))} · ${esc(h.arch)}</div>
   ${M&&M.up!=null?tileLive(M,up):`<div class=mrow>
    <div class=metric><span class=v>${esc(up.slice(0,8))}</span><span class=u>up</span></div>
    <div class=metric><span class=v>${g&&g.util_pct!=null?esc(g.util_pct)+'%':'—'}</span><span class=u>gpu</span></div>
   </div>
   <div class=gauges>
    <div class=gauge data-k=mem><span>mem</span>${bar(memP,true,'mem')}<span>${f.mem_used_gb??'—'}G</span></div>
    <div class=gauge data-k=disk><span>disk</span>${bar(diskP,false,'disk')}<span>${diskP||'—'}%</span></div>
    <div class=gauge><span>gpu</span>${bar(tmp)}<span>${gg?fmtT(gg.temp_c):'—'}</span></div>
   </div><div class=tnext>live metrics unavailable — SSH snapshot</div>`}
   <div class=tline data-jl="${esc(h.name)}">${jobLine(h.name)||nextLine(schedRow(s,h.name),f)}</div></div>`}).join('')+`</div>`;
}
/* ── live job state (client mirror of the server's job table) ──
   act() registers jobs; pollAll() refreshes running ones every 2 s; boot
   re-adopts in-flight jobs so a page reload mid-backup still shows them. */
const JOBS={};
function actHost(a,p){return (p.host||'') || (['backup','verify'].includes(a)?'*':'')}
function runningOn(host){return Object.values(JOBS).some(j=>!j.done&&j.running&&(j.params.host===host||(j.params.host===undefined&&['backup','verify'].includes(j.action))))}
function jobFor(host){
 const mine=Object.values(JOBS).filter(j=>j.params.host===host||(j.params.host===undefined&&['backup','verify','copy','prune'].includes(j.action)));
 mine.sort((a,b)=>(b.started_ts||0)-(a.started_ts||0));return mine[0]}
function jobBadge(host){const j=jobFor(host);if(!j)return '';
 if(!j.done&&j.running)return `<span class=spin>◌</span>`;
 if(j.done){const o=jobOutcome(j);return o.cls=='ok'?`<span class=okj>✓</span>`:o.cls=='warn'?`<span class=qj>–</span>`:`<span class=badj>✗</span>`}
 return `<span class=qj>…</span>`}
function nextLine(r,f){
 if(f&&f.agent_busy)return `<div class="jline run">job running on host${f.progress?' · '+esc(f.progress.uploaded_mb)+' MB up':''}</div>`;
 if(!r)return '';
 return `<div class=tnext>${r.due?'<span class=warn>due — next tick</span>':'next '+esc(r.profile)+' '+esc(fmtTs(r.next_ts))}</div>`}
/* what a finished job MEANT: a SKIP for busy is not a failure */
function jobOutcome(j){const out=j.output||'';
 if(j.rc===0)return {cls:'ok',txt:'ok'};
 if(/SKIP .*(busy|already running)/.test(out))return {cls:'warn',txt:'busy — skipped'};
 if(/SKIP .*(floor|probe failed|unmeasurable)/.test(out))return {cls:'warn',txt:'skipped — network'};
 if(j.rc===-2)return {cls:'warn',txt:'lost track (UI restart)'};
 return {cls:'bad',txt:'rc='+j.rc}}
function jobLine(host){const j=jobFor(host);if(!j)return '';
 const el=j.started?` · ${fmtDur((j.done?(j._end||Date.now()/1000):Date.now()/1000)-j.started_ts)}`:'';
 if(!j.done&&j.running)return `<div class="jline run">${esc(j.action)} running${el}</div>`;
 if(j.done){const last=(j.output||'').trim().split('\n').filter(Boolean).pop()||'',o=jobOutcome(j);
  return `<div class="jline ${o.cls}">${esc(j.action)} ${esc(o.txt)}${el}${last?' · '+esc(last.slice(0,60)):''}</div>`}
 const qb=j.queued_behind;
 return `<div class="jline warn">${esc(j.action)} queued${qb?' behind '+esc(qb.action)+(qb.host?' '+esc(qb.host):''):''}</div>`}
function fmtDur(s){s=Math.max(0,Math.round(s));
 return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s'
  :s<172800?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m'
  :Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h'}
function refreshJobDom(){
 // patch badges/lines in place — no full re-render, no flicker
 document.querySelectorAll('[data-jb]').forEach(el=>{el.innerHTML=jobBadge(el.dataset.jb)});
 document.querySelectorAll('[data-jl]').forEach(el=>{el.innerHTML=jobLine(el.dataset.jl)});
 const out=$('cout'),act=Object.values(JOBS).find(j=>!j.done);
 if(out&&act){out.textContent=`$ ${(act.argv||[]).join(' ')}\n…running\n`+
   ((act.output||'').trim().split('\n').slice(-12).join('\n'))}
 // while a job is active on the selected machine, keep its report live
 if(SEL&&jobFor(SEL)&&!jobFor(SEL).done)repaint('detail',PANELS.find(p=>p.id=='detail')._data)}
async function pollAll(){
 const ids=Object.keys(JOBS).filter(id=>!JOBS[id].done);
 let dirty=false,hyDone=false;
 for(const id of ids){try{const j=await fetchJSON('/api/v1/job/'+id);
  const was=j.done;Object.assign(JOBS[id],j);dirty=true;
  if(was){JOBS[id]._end=Date.now()/1000;
   tick(PANELS.find(x=>x.id=='jobs'));tick(PANELS.find(x=>x.id=='fleet'));
   tick(PANELS.find(x=>x.id=='storage'));tick(PANELS.find(x=>x.id=='network'));
   tick(PANELS.find(x=>x.id=='schedule'));tick(PANELS.find(x=>x.id=='runs'));
   if(j.action=='hygiene'||j.action=='hygieneFix')hyDone=true}}
  catch(e){}}
 if(ids.length||dirty)refreshJobDom();
 if(hySync())hyRepaint();
 if(hyDone)hyRefresh()}
/* re-adopt in-flight jobs after a reload, so a running backup is still visible */
async function adoptRunning(){
 try{const js=await fetchJSON('/api/v1/jobs?active=1');
  for(const j of js){if(!JOBS[j.id])JOBS[j.id]=Object.assign({started_ts:Date.now()/1000},j);
   else Object.assign(JOBS[j.id],j)}
  refreshJobDom()}catch(e){}}
function selectHost(n){SEL=n;localStorage.setItem(LS_SEL,n);
 history.replaceState(null,'',location.pathname+hashOf({host:n}));
 if(OVR)repaint('fleet',OVR);
 tick(PANELS.find(x=>x.id=='detail'))}
function rDetail(f){
 if(!SEL)return '<div class=empty>click a machine in the Fleet panel →</div>';
 if(!f)return '<div class=empty>select a machine</div>';
 if(f.error)return `<div class="empty bad">facts unavailable: ${esc(f.error)}</div>`;
 const o=OVR||{snapshots:{hosts:{}},hosts:[]},
  h=(o.hosts||[]).find(x=>x.name==SEL)||{name:SEL},
  sn=(o.snapshots&&o.snapshots.hosts||{})[SEL]||{},g=f.gpu_stats;
 // monitored only (a Mac, or backup:false): its own backups are reported, never run from here
 const vs=o.versions||{},CMARK={ok:'ok',upgrade:'warn',behind:'bad',missing:'bad',ahead:'mut',unknown:'mut',skipped:'mut'};
 const comps=((vs.hosts||{})[SEL]||[]).map(r=>`<tr><td>${esc(r.component)}</td><td>${esc(r.installed||'—')}</td><td>${esc(r.pinned||'—')}</td><td>${esc(r.latest||'—')}</td>
  <td class=${CMARK[r.state]||'mut'}>${esc(r.detail)}${r.fix||r.state=='skipped'?`<div class=compbtns>${compBtns(SEL,r)}</div>`:''}</td></tr>`).join('');
 const mon=h.os=='macos'||!!h.monitored_only,tm=f.timemachine||{},tmOld=mon&&h.os=='macos'&&(!tm.latest_ts||Date.now()/1000-tm.latest_ts>2*86400);
 const rows=(f.last_jobs||[]).map(j=>
  `<tr><td>${esc(fmtTs(j.ts))}</td><td>${esc(j.event||'')}</td><td class="${(j.rc??0)==0?'ok':'bad'}">${j.rc??''}</td><td>${esc(j.snapshot||'')}</td></tr>`).join('');
 const srows=((o.schedule&&o.schedule.rows)||[]).filter(r=>r.host==SEL);
 const bk=srows.map(r=>r.enabled?
  `<tr><td>${esc(r.profile)}${r.override?'<span class="tag info">override</span>':''}</td>
   <td class="${HCLS[r.health]||''}">${r.last_ts?esc(agoS(r.last_ts)):'never'}</td>
   <td>${esc(fmtDur(r.every))}${r.window?' · '+esc(r.window):''}</td>
   <td>${r.due?'<span class=warn>due now</span>':esc(fmtTs(r.next_ts))}</td>
   <td><span class="tag ${HCLS[r.health]||''}">${esc(r.health)}</span></td></tr>`
  :`<tr><td>${esc(r.profile)}<span class="tag info">override</span></td><td colspan=4 class=mut>disabled for this host</td></tr>`).join('');
 const other=Object.entries(sn.profiles||{}).filter(([p])=>!srows.some(r=>r.profile==p))
  .map(([p,v])=>`<tr><td class=mut>${esc(p)}</td><td class=mut>${esc(agoS(v.last_ts))}</td><td class=mut colspan=3>${v.count} snapshot${v.count==1?'':'s'} · not scheduled</td></tr>`).join('');
 const snaps=(sn.recent||[]).slice().reverse().map(r=>
  `<tr class="snaprow${BR.snap==r.id?' sel':''}" onclick="browseTo('${esc(SEL)}','${esc(r.id)}','/')" title="browse this snapshot"><td>${esc(r.id)}</td><td>${esc(fmtTs(r.ts))}</td><td>${esc(r.profile)}</td>
   <td>${r.recorded===false?'<span class="tag warn" title="in the repo, but no backup run was seen to finish: its driver died (UI restart / lost ssh). The data is real; check it with verify.">no run record</span>':'<span class=ok>✓</span>'}</td></tr>`).join('');
 const pr=f.progress,act=f.agent_busy
  ?`<div class=sline><span class="tag warn">running</span> job holds the agent lock on this host${pr?` · <b>${esc(pr.profile)}</b> ${esc(fmtDur(+pr.elapsed||0))} · uploaded <b>${esc(pr.uploaded_mb)}</b> MB · scanned <b>${esc(pr.scanned_mb)}</b> MB · at ${esc(fmtTs(pr.at))}`:''}</div>`
  :`<div class=sline><span class="tag ok">idle</span> no Noctyx job on this host</div>`;
 const blog=(f.backuplog||[]).length?`<pre class=jl-t>${esc(f.backuplog.join('\n'))}</pre>
  <div class=mut style="font:10.5px var(--mono);margin-top:3px">last-backup.log · updated ${esc(agoS(f.backuplog_mtime))} — whoever started the job (tick, UI or CLI)</div>`:'';
 const disks=(f.disks||[]).map(d=>
  `<tr><td>${esc(d.mnt)}</td><td class=mut>${esc(d.dev.replace('/dev/',''))}</td><td>${d.used_gb}/${d.size_gb} GB</td><td style=width:34%>${bar(d.pct)}</td></tr>`).join('');
 const devs=(f.blockdevs||[]).map(d=>
  `<tr><td>${esc(d.name)}</td><td>${d.size_gb} GB</td><td class=mut>${esc(d.serial)}</td></tr>`).join('');
 const jj=jobFor(SEL);
 const jtail=jj?((jj.output||'').trim().split('\n').filter(Boolean).slice(-6).join('\n')):'';
 return `<div class=rep>
 <div class=hero><span class=big>${esc(SEL)}</span>
  <span class=mut>${esc(f.address||'')}${f.ip&&f.ip!=f.address?' · '+esc(f.ip):''}</span></div>
 ${mon?'':`<div style=margin-top:8px>
  <button class=act onclick="act('backupHost',{host:'${esc(SEL)}'})">backup now</button>
  <button class=act onclick="act('verifyHost',{host:'${esc(SEL)}'})">verify now</button></div>`}
 ${jj?`<div class="jlive ${jj.done?(jj.rc===0?'ok':'bad'):'run'}" id=jlive>
  <div class=jl-h>${esc(jj.action)} <span class=jl-d>${fmtDur((jj.done?(jj._end||Date.now()/1000):Date.now()/1000)-(jj.started_ts||0))}</span>
   ${jj.done?(jj.rc===0?'· done':'· rc='+jj.rc):'· running…'}</div>
  ${jtail?`<pre class=jl-t>${esc(jtail)}</pre>`:''}</div>`:''}
 <div class=mut style="font:11px var(--mono);margin-top:6px">${esc((f.os_lines||[]).join(' · '))}</div>
 <div class=dtabs>${DTABS.map(t=>`<button class="dtab${t==DTAB?' on':''}" onclick="setTab('${t}')">${t}</button>`).join('')}</div>
 ${DTAB=='overview'?`
  <h3>Vitals</h3>
  <div class=cards>
   <div class=card data-k=cpu><div class=l>load</div><div class=v>${esc((f.loadavg||'—').split(' ')[0])}</div><div class=s>${esc(f.loadavg||'')}</div></div>
   <div class=card data-k=mem><div class=l>memory</div><div class=v>${f.mem_used_gb??'—'}<span class=u>/${f.mem_total_gb??'—'}G</span></div>${bar(f.mem_total_gb?100*f.mem_used_gb/f.mem_total_gb:0,true,'mem')}</div>
   <div class=card data-k=cpu><div class=l>cpu</div><div class=v>${esc((f.cpu&&f.cpu[0])||'—')}</div><div class=s>${esc(((f.cpu&&f.cpu[1])||'').slice(0,22))}</div></div>
   <div class=card><div class=l>uptime</div><div class=v style=font-size:13px>${esc((f.uptime_lines&&f.uptime_lines[0]||'—'))}</div></div>
   ${g&&g.temp_c!=null?`<div class=card data-k=gpu><div class=l>gpu</div><div class=v>${Math.round(tval(g.temp_c))}<span class=u>°${TU}</span></div><div class=s>${g.util_pct??'—'}% · ${g.mem_used_mib??'—'} MiB</div></div>`:''}
   ${mon?`<div class=card data-k=${tmOld?'warn':'ok'}><div class=l>time machine</div><div class=v style=font-size:13px>${tm.latest_ts?esc(fmtTs(tm.latest_ts)):(tm.dest?'no backup yet':'off')}</div><div class=s>${esc(tm.dest||'no destination')}${tm.running=='1'?' · backing up now':''}</div></div>`
   :`<div class=card data-k=ok><div class=l>snapshots</div><div class=v>${sn.count||0}</div><div class=s>${sn.last_ts?'last '+esc(fmtTs(sn.last_ts)):'none'}${sn.stale?' · stale':''}</div></div>`}
  </div>
  ${(f.gpu&&f.gpu[0])?`<div class=mut style="font:11px var(--mono);margin-top:8px">${esc(f.gpu.join(' / '))}</div>`:''}
  ${tabOverview(f._m)}
  <h3>Filesystems</h3>
  <table><tr><th>mount</th><th>dev</th><th>used</th><th></th></tr>${disks||'<tr><td class=mut>no data</td></tr>'}</table>
  <h3>Components</h3>
  <table><tr><th>component</th><th>installed</th><th>pin</th><th>latest</th><th></th></tr>${comps||'<tr><td class=mut colspan=5>not checked yet (noctyx versions)</td></tr>'}</table>
  ${vs.ts?`<div class=mut style="font:10.5px var(--mono)">checked ${esc(fmtTs(vs.ts))} · nightly, or <code>noctyx versions --host ${esc(SEL)}</code></div>`:''}
  ${mon?`<h3>Backups</h3>
  <div class=mut style="font:11.5px var(--mono)">monitored only: ${h.os=='macos'?'Time Machine backs this Mac up'+(tm.dest?' to '+esc(tm.dest):' (no destination set)')+(tm.latest_ts?', last '+esc(fmtTs(tm.latest_ts)):''):'not backed up by Noctyx (backup: false, or no repository)'}</div>`
  :`<h3>Noctyx agent</h3>
  <div class=mut style="font:11.5px var(--mono)">${f.agent&&f.agent[0]?'agent '+esc(f.agent[0])+' · profiles '+esc(f.agent[1]||'—'):'<span class=bad>not installed</span>'}
   ${h.used_gb?` · pinned source ${h.used_gb} GB`:''}</div>`}`
 :DTAB=='llm'?tabLLM(f._m)
 :DTAB=='network'?tabNetwork(f._m)
 :DTAB=='storage'?tabStorage(f._m)+`<h3>Block devices (inventory)</h3>
  <table><tr><th>name</th><th>size</th><th>serial</th></tr>${devs||'<tr><td class=mut>no data</td></tr>'}</table>`
 :DTAB=='gpu'?tabGpu(f._m,f)
 :DTAB=='health'?tabHealth(f._m)
 :`
  <h3>Backups</h3>
  <table><tr><th>profile</th><th>last</th><th>every</th><th>next</th><th></th></tr>${bk+other||'<tr><td class=mut>not scheduled — see Schedule panel</td></tr>'}</table>
  <h3>Agent activity</h3>
  ${act}${blog}
  <h3>Snapshots</h3>
  <table><tr><th>id</th><th>when</th><th>profile</th><th></th></tr>${snaps||'<tr><td class=mut>none</td></tr>'}</table>
  <div class=mut style="font:10.5px var(--mono)">click a snapshot to browse its files · <button class=lnk onclick="browseTo('${esc(SEL)}',null,'/')">browse latest</button></div>
  <h3>Recent agent jobs</h3>
  <table><tr><th>when</th><th>event</th><th>rc</th><th>snapshot</th></tr>${rows||'<tr><td class=mut>none recorded</td></tr>'}</table>`}
 </div>`;
}
/* ── report tabs fed by /api/v1/metrics/<host>?tab=… (Prometheus) ── */
function mGuard(m){if(!m||m.loading)return '<div class=empty>loading…</div>';
 if(m.error)return `<div class="empty warn">live metrics unavailable: ${esc(m.error)}</div>`;return ''}
/* ── Cluster: every machine on one screen (Prometheus fleet metrics) ── */
function rCluster(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},ord=m.order||(OVR&&OVR.hosts||[]).map(h=>h.name),names=ord.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!ord.includes(n)));
 let tps=0,run=0,wait=0,models=0,gpuW=0;
 const rows=names.map(n=>{const M=H[n]||{},L=M.llm||{},sv=(L.servers||[]).filter(s=>s.model);
  sv.forEach(s=>{tps+=s.gen_tps||0;run+=s.running||0;wait+=s.waiting||0;models++});gpuW+=M.gpu_power||0;
  const llm=sv.length?sv.map(s=>`<div class=md title="${esc(s.model)}">${esc(s.model)}${L.up===0?' <span class="tag bad">down</span>':''}</div>`).join('')
   :L.worker_for?`<div class="md wk">worker for ${esc(L.worker_for)}</div>`:'<div class="md wk">—</div>';
  const s0=sv[0]||{},sp=M.spark||{};
  const cell=(v,f,cls)=>`<td class="n ${cls||''}">${v==null?'<span class=u>—</span>':f(v)}</td>`;
  const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u>ms</span>':v.toFixed(1)+'<span class=u>s</span>';   /* null-safe: engines without latency histograms (tensorfold) */
  return `<tr><td><span class="dot ${M.up?'ok':''}"></span> <span class=hn>${esc(n)}</span></td><td>${llm}</td>
   <td class=n data-k=llm>${sv.length?`${s0.running??0}<span class=u> run</span>${s0.waiting?' <span class=warn>'+s0.waiting+' wait</span>':''} · ${Math.round(s0.gen_tps||0)}<span class=u> tok/s</span> ${spark([{pts:(s0.spark||{}).gen_tps,cls:'a'}])}${s0.decode_tps!=null?`<br><span class=u>decode</span> ${Math.round(s0.decode_tps)}<span class=u> tok/s per req</span>`:''}`:'<span class=u>—</span>'}</td>
   <td class="n lp">${sv.length?(s0.prefill_tps!=null?`${esc(fmtK(s0.prefill_tps))}<span class=u> tok/s</span>${s0.prefill_p50!=null?` · ${ms(s0.prefill_p50)}<span class=u> p50</span>`:''}${s0.prefill_share!=null?`<br><span class=u>${Math.round(s0.prefill_share)}% of busy time</span>`:''}`:'<span class=u>idle</span>'):'<span class=u>—</span>'}</td>
   <td class="n lp">${sv.length?`<span class="${s0.kv_pct>90?'bad':s0.kv_pct>70?'warn':''}">kv ${Math.round(s0.kv_pct||0)}%</span>${s0.ttft_p50!=null?' · ttft '+ms(s0.ttft_p50):''}${s0.queue_p50!=null&&s0.queue_p50>0.05?`<br><span class=u>queue</span> ${ms(s0.queue_p50)}`:''}`:'<span class=u>—</span>'}</td>
   <td class=n data-k=gpu>${M.gpu_util!=null?`${Math.round(M.gpu_util)}% · <span class="${M.gpu_temp>85?'bad':M.gpu_temp>75?'warn':''}">${fmtT(M.gpu_temp)}</span> · ${Math.round(M.gpu_power)}W`:'<span class=u>—</span>'}</td>
   <td class="n lp2" data-k=cpu>${M.cpu!=null?Math.round(M.cpu)+'%':'—'} ${spark([{pts:sp.cpu}],{max:100})}</td>
   <td class="n lp2">${M.mem_used!=null?Math.round(M.mem_used/1e9)+'<span class=u>/'+Math.round((M.mem_total||0)/1e9)+'G</span>':'<span class=u>—</span>'}</td>
   <td class=n data-k=net>↑${esc(fmtBits(M.net_tx))} ↓${esc(fmtBits(M.net_rx))}${M.fabric_tx!=null?`<br><span class=u>fabric</span> ↑${esc(fmtBits(M.fabric_tx))} ↓${esc(fmtBits(M.fabric_rx))}`:''}</td>
   <td class="n lp"><span class="${M.root_pct>90?'bad':M.root_pct>80?'warn':''}">${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}</span>${M.disk_io!=null?' · '+esc(fmtBytes(M.disk_io))+'<span class=u>/s</span>':''}</td>
   <td>${M.failed_units?goA({host:n,panel:'hygiene'},'warn',M.failed_units+' failed','failed units — fix in Hygiene'):''}${M.reboot?goA({host:n,panel:'hygiene'},'warn','reboot','reboot required — Hygiene'):''}${M.up===0?goA({host:n,panel:'detail',tab:'health'},'bad','no metrics','exporter down — Machine Report › health'):''}</td></tr>`}).join('');
 const F=m.fleet||{},S=F.series||{},I=F.infra||{};
 const tokSeries=Object.entries(S.tokens||{}).sort().map(([mo,pts])=>({name:mo.length>18?mo.slice(0,16)+'…':mo,pts,cls:slotOf(mo)}));
 const tokAll=Object.values(S.tokens||{}).reduce((acc,pts)=>{pts.forEach(([t,v])=>{acc[t]=(acc[t]||0)+(v||0)});return acc},{});
 const tokPts=Object.entries(tokAll).map(([t,v])=>[+t,v]).sort((a,b)=>a[0]-b[0]);
 const roster=(F.roster||[]).map(r=>`<tr><td><b>${esc(r.model||'?')}</b></td><td>${esc(r.host)}${r.workers&&r.workers.length?' <span class=u>+ '+esc(r.workers.join(', '))+'</span>':''}</td>
   <td class=n>${r.tp?'tp'+r.tp:'—'}</td><td class=n>${r.max_model_len?esc(fmtK(r.max_model_len))+'<span class=u> ctx</span>':'—'}</td>
   <td class=n>${r.kv_tokens!=null?esc(fmtK(r.kv_tokens))+'<span class=u> kv tok</span>':'—'}</td>
   <td class=n>${r.gpu_mem_util!=null?Math.round(r.gpu_mem_util*100)+'<span class=u>% gpu mem</span>':'—'}</td>
   <td class=n>${r.started_ts?'<span class=u>up</span> '+esc(fmtDur(Date.now()/1000-r.started_ts)):'—'}</td></tr>`).join('');
 const raid=(I.raid||[]).filter(r=>r.array=='md2'||(I.raid||[]).length==1).map(r=>{const t=(I.raid_total||{})[r.array],sy=(I.raid_sync||{})[r.array];
   return `${esc(r.level)} <b class="${r.active<t?'bad':''}">${r.active}/${t}</b>${sy!=null?' <span class=warn>rebuilding '+sy.toFixed(1)+'%</span>':''}`}).join(' · ');
 const upN=names.filter(n=>H[n].up).length;
 return `<div class=hero>
  <div class=hv data-ghost="${ghost(Math.round(tps))}">${Math.round(tps)}</div>
  <div class=hl>tokens / second across the fleet<br><b>${models}</b> model${models==1?'':'s'} serving on <b>${upN}</b> of ${names.length} nodes${I.llm_total&&I.llm_up<I.llm_total?` · <span class=bad>${I.llm_total-I.llm_up} endpoint down</span>`:''}</div>
  <div class=stats>
   ${statTile('requests',run,' running',`${wait} waiting`,S.requests)}
   ${statTile('gpu power',Math.round(gpuW),' W',I.gpu_wh_24h!=null?(I.gpu_wh_24h/1000).toFixed(1)+' kWh in 24 h':'',S.gpu_power)}
   ${statTile('generated, 24 h',esc(fmtK(I.tokens_24h)),' tok',`${Math.round(I.requests_24h||0)} requests`,tokPts)}
   ${statTile('network',esc(fmtBits((S.net_tx&&S.net_tx.length?S.net_tx[S.net_tx.length-1][1]:0))),'',`↓ ${esc(fmtBits(S.net_rx&&S.net_rx.length?S.net_rx[S.net_rx.length-1][1]:0))}`,S.net_tx)}
   ${statTile('health',`${I.backups_ok??'?'}<span class=u>/${I.backups_total??'?'}</span>`,'',`backups on schedule · ${I.alerts?'<span class=warn>'+I.alerts+' alert'+(I.alerts>1?'s':'')+'</span>':'no alerts'}`)}
  </div></div>
 <div class=clwrap><div>
  <table class=cl><tr><th>node</th><th>model</th><th>llm</th><th class=lp>prefill</th><th class=lp>cache · latency</th><th>gpu</th><th class=lp2>cpu</th><th class=lp2>mem</th><th>net</th><th class=lp>root · io</th><th></th></tr>${rows}</table>
  <h3>Models</h3>
  <table class="cl roster"><tr><th>model</th><th>on</th><th class=n>tp</th><th class=n>context</th><th class=n>kv capacity</th><th class=n>gpu mem</th><th class=n>served</th></tr>${roster||'<tr><td class=mut colspan=7>no model serving</td></tr>'}</table>
  <div class=strip>
   <span>__NAS__ <b>${I.nas_free!=null?esc(fmtBytes(I.nas_free)):'—'}</b> free${I.nas_used!=null?' · '+esc(fmtBytes(I.nas_used))+' used':''}</span>
   <span>raid ${raid||'—'}</span>
   <span>repo endpoint <b class="${I.rest_up?'ok':'bad'}">${I.rest_up?'up':'DOWN'}</b></span>
   <span>exporters <b>${I.exporters_up??'?'}/${I.exporters_total??'?'}</b></span>
   <span>backups on schedule <b class="${I.backups_ok==I.backups_total?'ok':'warn'}">${I.backups_ok??'?'}/${I.backups_total??'?'}</b></span>
   <span>alerts <b class="${I.alerts?'warn':'ok'}">${I.alerts??0}</b></span>
  </div></div>
  <div class=charts>
   ${chart('tokens/s by model',tokSeries.length?tokSeries:[{pts:[]}],v=>Math.round(v)+' tok/s')}
   ${chart('requests',[{name:'run',pts:S.requests,cls:'c2'},{name:'wait',pts:S.waiting,cls:'c4'}],v=>Math.round(v))}
   ${chart('gpu power (fleet)',[{pts:S.gpu_power,cls:'a'}],v=>Math.round(v)+' W')}
   ${chart('network (fleet)',[{name:'↑',pts:S.net_tx,cls:'g'},{name:'↓',pts:S.net_rx,cls:'c0'}],fmtBits)}
  </div></div>
 <div class=mut style="font:10.5px var(--mono);margin-top:6px">llm = requests running (+waiting) · tok/s = generated tokens per second averaged over the last minute (idle time included) · decode = tokens/s a single request streams at during decode · prefill = prompt tokens processed per second of prefill, median prefill time, share of busy time spent prefilling · kv = KV-cache use · ttft = median time to first token (5 min) · fabric = the 200G links (tensor-parallel traffic) · kv capacity = tokens the KV cache can hold at once</div>`}
function tabLLM(m){const g=mGuard(m);if(g)return g;
 const S=m.series||{},N=m.now||{},topo=m.topology||[];
 const models=Object.keys(S.gen_tps||{});
 const head=topo.map(t=>t.role=='worker'?`<div class=sline><span class="tag info">TP worker</span> ${esc(t.engine)} <b>${esc(t.model||'')}</b> — headless rank of <b>${esc(t.worker_for||'?')}</b> (tp=${esc(t.tp)}); metrics live on that node</div>`
  :`<div class=sline><span class="tag ${(m.up||{})[String(t.port)]===0?'bad':'ok'}">${esc(t.engine)}</span> <b>${esc(t.model||'')}</b> on :${esc(t.port)}${t.tp>1?' · tensor-parallel '+esc(t.tp):''}${t.container?' · '+esc(t.container):''}</div>`).join('');
 if(!models.length)return head||'<div class=empty>no LLM server on this node</div>';
 const nm=k=>(N[k]||{});
 const cards=models.map(mo=>`<h3>${esc(mo)}</h3><div class=cards>
  <div class=card data-k=llm><div class=l>running</div><div class=v>${nm('running')[mo]??'—'}</div><div class=s>${nm('waiting')[mo]??0} waiting</div></div>
  <div class=card data-k=llm><div class=l>throughput</div><div class=v>${Math.round(nm('gen_tps')[mo]||0)}<span class=u> tok/s</span></div><div class=s>1-min average, idle included</div></div>
  <div class=card><div class=l>decode speed</div><div class=v>${nm('decode_tps')[mo]!=null?Math.round(nm('decode_tps')[mo]):'—'}<span class=u> tok/s</span></div><div class=s>per request, while streaming</div></div>
  <div class=card><div class=l>prefill</div><div class=v>${nm('prefill_tps')[mo]!=null?esc(fmtK(nm('prefill_tps')[mo])):'—'}<span class=u> tok/s</span></div><div class=s>${nm('prefill_p50')[mo]!=null?nm('prefill_p50')[mo].toFixed(2)+' s p50 · ':''}${nm('prefill_share')[mo]!=null?Math.round(nm('prefill_share')[mo])+'% of busy time':''}</div></div>
  <div class=card><div class=l>per request</div><div class=v>${nm('prompt_tok_p50')[mo]!=null?esc(fmtK(nm('prompt_tok_p50')[mo])):'—'}<span class=u> in</span></div><div class=s>→ ${nm('gen_tok_p50')[mo]!=null?esc(fmtK(nm('gen_tok_p50')[mo])):'—'} out (p50) · queue ${nm('queue_p50')[mo]!=null?Math.round(nm('queue_p50')[mo]*1000)+' ms':'—'}</div></div>
  <div class=card><div class=l>kv cache</div><div class=v>${nm('kv_pct')[mo]!=null?Math.round(nm('kv_pct')[mo]):'—'}<span class=u>%</span></div>${bar(nm('kv_pct')[mo])}</div>
  <div class=card><div class=l>ttft p50</div><div class=v>${nm('ttft_p50')[mo]!=null?(nm('ttft_p50')[mo]<1?Math.round(nm('ttft_p50')[mo]*1000)+'<span class=u> ms</span>':nm('ttft_p50')[mo].toFixed(1)+'<span class=u> s</span>'):'—'}</div><div class=s>p95 ${nm('ttft_p95')[mo]!=null?nm('ttft_p95')[mo].toFixed(2)+' s':'—'}</div></div>
  <div class=card><div class=l>e2e p50</div><div class=v>${nm('e2e_p50')[mo]!=null?nm('e2e_p50')[mo].toFixed(1)+'<span class=u> s</span>':'—'}</div><div class=s>itl ${nm('itl_p50')[mo]!=null?Math.round(nm('itl_p50')[mo]*1000)+' ms':'—'}</div></div>
  <div class=card><div class=l>last hour</div><div class=v>${Math.round(nm('requests_1h')[mo]||0)}<span class=u> req</span></div><div class=s>${esc(fmtK(nm('tokens_1h')[mo]||0))} tokens</div></div>
  <div class=card><div class=l>prefix cache</div><div class=v>${nm('prefix_hit')[mo]!=null?Math.round(nm('prefix_hit')[mo]):'—'}<span class=u>% hit</span></div><div class=s>${(nm('preempt_rate')[mo]||0)>0?'<span class=warn>'+(nm('preempt_rate')[mo]*60).toFixed(1)+' preempt/min</span>':'no preemptions'}</div></div></div>
  <div class=charts>
   ${chart('throughput (1 m avg)',[{name:'generated',pts:(S.gen_tps||{})[mo],cls:'c1'},{name:'prompt',pts:(S.prompt_tps||{})[mo],cls:'c0'}],v=>Math.round(v)+' tok/s')}
   ${chart('speed per phase',[{name:'decode',pts:(S.decode_tps||{})[mo],cls:'c1'},{name:'prefill ÷100',pts:((S.prefill_tps||{})[mo]||[]).map(p=>[p[0],p[1]==null?null:p[1]/100]),cls:'c0'}],v=>Math.round(v)+' tok/s')}
   ${chart('prefill share of busy time',[{pts:(S.prefill_share||{})[mo],cls:'c3'}],pct,{max:100})}
   ${chart('prefill / queue time p50',[{name:'prefill',pts:(S.prefill_p50||{})[mo],cls:'c3'},{name:'queue',pts:(S.queue_p50||{})[mo],cls:'c4'}],v=>v.toFixed(2)+' s')}
   ${chart('requests',[{name:'run',pts:(S.running||{})[mo],cls:'c2'},{name:'wait',pts:(S.waiting||{})[mo],cls:'c4'}],v=>Math.round(v))}
   ${chart('kv cache',[{pts:(S.kv_pct||{})[mo]}],pct,{max:100})}
   ${chart('ttft',[{name:'p50',pts:(S.ttft_p50||{})[mo],cls:'c0'},{name:'p95',pts:(S.ttft_p95||{})[mo],cls:'c7'}],v=>v.toFixed(2)+' s')}
   ${chart('e2e latency p50',[{pts:(S.e2e_p50||{})[mo]}],v=>v.toFixed(1)+' s')}
   ${chart('prefix-cache hit',[{pts:(S.prefix_hit||{})[mo],cls:'g'}],pct,{max:100})}</div>`).join('');
 return head+cards}
function tabOverview(m){
 if(!m||m.loading||m.error)return mGuard(m);const S=m.series||{};
 return `<h3>Last hour</h3><div class=charts>
  ${chart('cpu',[{pts:S.cpu}],pct,{max:100})}
  ${chart('memory',[{pts:S.mem_used}],fmtBytes,{max:m.mem_total||undefined})}
  ${chart('network',[{name:'↑',pts:S.net_tx,cls:'g'},{name:'↓',pts:S.net_rx,cls:'c0'}],fmtBits)}
  ${chart('disk i/o',[{pts:S.disk_io}],b=>fmtBytes(b)+'/s')}
  ${chart('gpu',[{pts:S.gpu_util,cls:'a'}],pct,{max:100})}
  ${chart('load (1m)',[{pts:S.load1}],v=>v.toFixed(2))}</div>`}
function tabNetwork(m){const g=mGuard(m);if(g)return g;
 const rows=(m.nics||[]).map(n=>{const st=n.operstate;
  return `<tr><td>${esc(n.device)}</td>
   <td class="${st=='up'?'ok':st=='down'?'mut':''}">${esc(st||'—')}</td>
   <td>${n.speed_bps?esc(fmtBits(n.speed_bps)):'—'}</td><td class=mut>${esc(n.duplex||'')}</td>
   <td class="${n.errors_1h?'bad':''}">${n.errors_1h??'—'}</td><td class="${n.drops_1h?'warn':''}">${n.drops_1h!=null?Math.round(n.drops_1h):'—'}</td>
   <td>${spark([{pts:n.tx,cls:'g'},{pts:n.rx,cls:'c0'}])}</td><td class=mut>${esc(n.address||'')}</td></tr>`}).join('');
 const busy=(m.nics||[]).filter(n=>n.operstate=='up').map(n=>
  chart(n.device,[{name:'↑',pts:n.tx,cls:'g'},{name:'↓',pts:n.rx,cls:'c0'}],fmtBits)).join('');
 return `<h3>Interfaces</h3>
 <table><tr><th>nic</th><th>state</th><th>link</th><th>duplex</th><th>err 1h</th><th>drop 1h</th><th>↑/↓ 1h</th><th>mac</th></tr>${rows||'<tr><td class=mut>none</td></tr>'}</table>
 <div class=mut style="font:10.5px var(--mono);margin-top:4px">link = negotiated speed · a gigabit port showing 100 Mb/s, or rising err/drop, is a cable or port fault</div>
 <h3>Traffic (last hour)</h3><div class=charts>${busy||'<div class=mut>no interface up</div>'}</div>`}
function tabStorage(m){const g=mGuard(m);if(g)return g;
 const fs=(m.filesystems||[]).map(x=>{const used=x.size&&x.avail!=null?100*(1-x.avail/x.size):null,
   ino=x.files?100*(1-(x.files_free||0)/x.files):null;
  return `<tr><td>${esc(x.mount)}</td><td class=mut>${esc((x.device||'').replace('/dev/',''))} · ${esc(x.fstype||'')}</td>
   <td>${esc(fmtBytes(x.size-(x.avail||0)))} / ${esc(fmtBytes(x.size))}</td><td style=width:26%>${bar(used)}</td>
   <td class="${ino>90?'bad':ino>75?'warn':'mut'}">${ino!=null?Math.round(ino)+'% ino':''}</td></tr>`}).join('');
 const nv=(m.nvme||[]).map(d=>`<tr><td>${esc(d.device)}</td><td class=mut>${esc(d.model||'')}</td>
   <td class="${(d.wear||0)>=0.8?'bad':(d.wear||0)>=0.5?'warn':''}">${d.wear!=null?Math.round(d.wear*100)+'%':'—'}</td>
   <td>${fmtT(d.temp)}</td><td class="${d.media_errors?'bad':''}">${d.media_errors??'—'}</td>
   <td class="${d.critical?'bad':'ok'}">${d.critical?'WARN '+d.critical:'ok'}</td>
   <td>${d.spare!=null?Math.round(d.spare*100)+'%':'—'}</td><td>${d.hours!=null?Math.round(d.hours).toLocaleString()+' h':'—'}</td>
   <td class=mut>${d.unsafe??''}</td></tr>`).join('');
 const io=Object.entries(m.io||{}).map(([d,pts])=>chart(d,[{pts}],b=>fmtBytes(b)+'/s')).join('');
 return `<h3>Filesystems</h3>
 <table><tr><th>mount</th><th>dev</th><th>used</th><th></th><th>inodes</th></tr>${fs||'<tr><td class=mut>none</td></tr>'}</table>
 <h3>NVMe health</h3>
 <table><tr><th>dev</th><th>model</th><th>wear</th><th>temp</th><th>media err</th><th>critical</th><th>spare</th><th>powered</th><th>unsafe off</th></tr>${nv||'<tr><td class=mut>no NVMe data (nvme-cli collector)</td></tr>'}</table>
 <h3>Disk I/O (last hour)</h3><div class=charts>${io||'<div class=mut>no data</div>'}</div>`}
function tabGpu(m,f){const g=mGuard(m);if(g)return g;
 const S=m.series||{},gpus=(m.info||[]);
 if(!gpus.length)return '<div class=empty>no NVIDIA GPU reported</div>';
 return gpus.map(i=>{const k=i.gpu,mu=(m.mem_used||{})[k],mt=(m.mem_total||{})[k];
  return `<h3>GPU ${esc(k)} · ${esc(i.name||'')}</h3>
  <div class=kv><span class=k>driver</span><span>${esc(i.driver||'—')}</span>
   <span class=k>memory</span><span>${mu!=null&&mt?esc(fmtBytes(mu))+' / '+esc(fmtBytes(mt)):
    'unified memory — compute processes hold '+esc(fmtBytes(m.proc_mem))}</span></div>
  <div class=charts>
   ${chart('utilisation',[{pts:(S.util||{})[k],cls:'a'}],pct,{max:100})}
   ${chart('temperature',[{pts:((S.temp||{})[k]||[]).map(([t,v])=>[t,v==null?v:tval(v)]),cls:'w'}],v=>Math.round(v)+'°'+TU)}
   ${chart('power',[{pts:(S.power||{})[k]}],v=>Math.round(v)+' W')}
   ${chart('SM clock',[{pts:(S.clock||{})[k],cls:'g'}],v=>Math.round(v)+' MHz')}</div>`}).join('')}
function tabHealth(m){const g=mGuard(m);if(g)return g;
 const off=m.time_offset_s!=null?Math.abs(m.time_offset_s)*1000:null;
 const cs=(m.containers||[]).map(c=>`<tr><td>${esc(c.name)}</td><td class=mut>${esc(c.image)}</td></tr>`).join('');
 return `<h3>System</h3><div class=kv>
  <span class=k>exporter</span><span class="${m.exporter_up?'ok':'bad'}">${m.exporter_up?'up':'DOWN'}${m.textfile_age_s!=null?` · gpu/docker collector ran ${Math.round(m.textfile_age_s)}s ago`:''}</span>
  <span class=k>failed units</span><span class="${(m.failed_units||[]).length?'warn':'ok'}">${(m.failed_units||[]).length?esc(m.failed_units.join(', '))+` <span class=golnk onclick="goTo(${esc(JSON.stringify({host:SEL,panel:'hygiene'}))})">fix in Hygiene →</span>`:'none'}</span>
  <span class=k>reboot</span><span class="${m.reboot?'warn':'ok'}">${m.reboot?`required (/var/run/reboot-required) <span class=golnk onclick="goTo(${esc(JSON.stringify({host:SEL,panel:'hygiene'}))})">Hygiene →</span>`:'not required'}${rbEligible(SEL)?' '+rbBtn(SEL,'lnk'):''}</span>
  <span class=k>updates</span><span>${m.upgrades||0} pending${m.autoremove?` · ${m.autoremove} autoremovable`:''}</span>
  <span class=k>clock</span><span class="${m.time_synced?'ok':'warn'}">${m.time_synced?'synchronised':'NOT synchronised'}${off!=null?` · offset ${off.toFixed(off<1?2:0)} ms`:''}</span>
  <span class=k>booted</span><span>${m.boot_ts?esc(fmtTs(m.boot_ts))+' ('+esc(agoS(m.boot_ts))+')':'—'}</span></div>
 <h3>Containers (${(m.containers||[]).length})</h3>
 <table><tr><th>name</th><th>image</th></tr>${cs||'<tr><td class=mut>none running</td></tr>'}</table>${sigHealth(m)}`}
/* the early warnings' sources for this machine: sshd logins, its RoCE ports, its NFS mounts (rules.yml "signals") */
function sigHealth(m){
 const S=m.ssh||{},n=v=>v==null?'—':Math.round(v),FS={1:'down',2:'init',3:'armed',4:'active',5:'active defer'};
 const bps=v=>v==null?'—':fmtBytes(v)+'/s';
 const ssh=S.known==null?'':`<h3>SSH</h3><div class=kv>
  <span class=k>last 24 h</span><span class="${(S.failed_24h||0)+(S.invalid_24h||0)>50?'warn':''}">${n(S.accepted_24h)} logins · ${n(S.failed_24h)} failed · ${n(S.invalid_24h)} for unknown users</span>
  <span class=k>addresses</span><span>${n(S.known)} a login came from${(S.new||[]).length?` · <span class=warn>new this week: ${S.new.map(x=>esc(x.user+' from '+x.addr+' '+agoS(x.ts))).join('; ')}</span>`:''}</span></div>`;
 const fab=(m.fabric||[]).length?`<h3>RoCE fabric</h3><table><tr><th>port</th><th>state</th><th>rate</th><th>in / out</th><th>errors 1 h</th><th>retransmits</th></tr>${m.fabric.map(f=>`<tr><td>${esc(f.device)}</td><td class="${f.state==4?'ok':'mut'}">${esc(FS[f.state]||f.state)}</td><td>${f.state==4?esc(fmtBytes(f.rate*8).replace('B','b'))+'/s':'—'}</td><td>${f.state==4?esc(bps(f.rx_bps))+' / '+esc(bps(f.tx_bps)):'—'}</td><td class="${f.errors_1h>0?'warn':''}">${n(f.errors_1h)}</td><td class="${f.retrans_s>1?'warn':''}">${f.retrans_s==null?'—':esc(f.retrans_s.toFixed(2))+'/s'}</td></tr>`).join('')}</table>`:'';
 const nfs=(m.nfs||[]).length||m.nfs_running_s?`<h3>NFS mounts</h3>${m.nfs_running_s?`<div class=bad>the probe has been waiting ${esc(fmtDur(m.nfs_running_s))} for a mount (hung?)</div>`:''}<table><tr><th>mount</th><th>write</th><th>read</th><th>4 KiB round trip</th></tr>${(m.nfs||[]).map(x=>`<tr><td title="${esc(x.source||'')}">${esc(x.mountpoint)}</td>${x.ok?`<td>${esc(bps(x.write))}</td><td>${esc(bps(x.read))}</td><td>${esc(((x.small||0)*1000).toFixed(1))} ms</td>`:'<td colspan=3 class=bad>the probe failed</td>'}</tr>`).join('')}</table>${m.nfs_probe_ts?`<div class=mut>probed ${esc(agoS(m.nfs_probe_ts))}, every 5 min</div>`:''}`:'';
 return ssh+fab+nfs}
/* ── schedule: what runs when, and why; inline editing via schedule-set ── */
let EDIT=null;
function rSched(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const t=d.tick||{},h=(d.spindle&&d.spindle.holder)||{};
 const head=`<div class=sline>tick every <b>${esc(d.tick_minutes)}m</b> · next ${esc((t.next||'—').replace(/^\w+ /,'').replace(/:\d\d UTC$/,'Z'))}
  ${t.running?'· <span class=warn>tick running</span>':''}${t.timer&&t.timer!='active'?' · <span class=bad>timer '+esc(t.timer)+'</span>':''}
  · ${esc(d.spindle&&d.spindle.name)}: ${h.what?'<span class=warn>'+esc(h.what)+'</span> since '+esc(fmtTs(h.since)):'<span class=ok>free</span>'}
  · failed runs retry after ${esc(d.retry_after)}</div>`;
 const edRow=(host,prof,cur)=>`<tr class=ed><td colspan=7>
  <label>every</label><input id=ed-every value="${esc(cur.every||'')}" placeholder="24h">
  <label>window</label><input id=ed-window value="${esc(cur.window===null?'any':(cur.window||''))}" placeholder="01:00-07:00">
  <button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','set')">save</button>
  <button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','disable')">disable</button>
  ${host&&cur.override?`<button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','reset')">reset to default</button>`:''}
  <button class=lnk onclick="EDIT=null;repaint('schedule',PANELS.find(p=>p.id=='schedule')._data)">cancel</button>
  <div class=hint>every: 15m–90d (e.g. 6h, 1d, 2d) · window: HH:MM-HH:MM in UTC (when a backup may START; may wrap midnight) or "any".
  With a window and every ≥ 1d the host runs once per window. ${host?'Blank fields keep the default.':'Applies to every host without an override.'}</div></td></tr>`;
 const isEd=(host,prof)=>EDIT&&EDIT.host===host&&EDIT.profile===prof;
 let rows=Object.entries(d.defaults||{}).map(([prof,r])=>
  `<tr><td><b>all hosts</b></td><td>${esc(prof)}</td><td colspan=2 class=mut>fleet default</td><td>${esc(r.every)}</td><td>${esc(r.window||'any')}</td>
   <td><button class=lnk onclick="editSched('','${esc(prof)}')">edit</button></td></tr>`
  +(isEd('',prof)?edRow('',prof,r):'')).join('');
 rows+=(d.rows||[]).map(r=>{
  const main=r.enabled?
   `<tr class="${hlRow('schedule',r.host)}"><td><span class=golnk onclick="goTo(${esc(JSON.stringify({host:r.host,panel:'detail',tab:'backups'}))})">${esc(r.host)}</span></td><td>${esc(r.profile)}${r.override?'<span class="tag info">override</span>':''}</td>
    <td class="${HCLS[r.health]||''}">${r.last_ts?esc(agoS(r.last_ts)):'never'}</td>
    <td>${r.due?'<span class=warn>due now</span>':esc(fmtTs(r.next_ts))}</td>
    <td>${esc(fmtDur(r.every))}</td><td>${esc(r.window||'any')}</td>
    <td><button class=lnk onclick="editSched('${esc(r.host)}','${esc(r.profile)}')">edit</button></td></tr>
    ${r.failed_at?`<tr><td></td><td colspan=6 class=warn style="font-size:11px">last attempt failed ${esc(agoS(r.failed_at))} — ${esc(r.why)}</td></tr>`:''}`
   :`<tr><td>${esc(r.host)}</td><td>${esc(r.profile)}<span class="tag info">override</span></td><td colspan=4 class=mut>disabled for this host</td>
    <td><button class=lnk onclick="editSched('${esc(r.host)}','${esc(r.profile)}')">edit</button></td></tr>`;
  return main+(isEd(r.host,r.profile)?edRow(r.host,r.profile,{override:r.override,window:''}):'')}).join('');
 return head+`<table><tr><th>host</th><th>profile</th><th>last</th><th>next</th><th>every</th><th>window</th><th></th></tr>${rows}</table>`}
function editSched(host,profile){EDIT={host,profile};repaint('schedule',PANELS.find(p=>p.id=='schedule')._data)}
async function saveSched(host,profile,mode){
 const p={profile,mode};if(host)p.host=host;
 if(mode=='set'){const e=($('ed-every').value||'').trim(),w=($('ed-window').value||'').trim();
  if(e)p.every=e;if(w)p.window=w;
  if(!e&&!w){alert('give an interval and/or a window');return}}
 if(mode=='disable'&&!confirm(`Stop scheduling ${profile} ${host?'on '+host:'for ALL hosts'}?`))return;
 EDIT=null;await act('setSchedule',p);
 setTimeout(()=>{tick(PANELS.find(x=>x.id=='schedule'));tick(PANELS.find(x=>x.id=='fleet'))},1500)}
/* ── config (/config): what the backups cover — docker volumes, database dumps,
   extra excludes, retention. Saved through `noctyx policy-set` (validates,
   writes the site config, audits) which then pushes the result to every host. ── */
let CFGDIRTY=false,CFGJOB=null;
const lines=v=>(v||'').split('\n').map(x=>x.trim()).filter(Boolean);
function cfgDirty(){CFGDIRTY=true;const b=$('cf-state');if(b){b.textContent='unsaved changes';b.className='warn'}}
/* tri-state as a segmented control: the fleet default, or on / off for this host (data-v holds it) */
function triSel(id,val,eff){const v=val===true?'on':val===false?'off':'';
 return `<div class=seg id="${id}" data-v="${v}">${[['',`default (${eff?'on':'off'})`],['on','on'],['off','off']].map(([k,l])=>`<button type=button class="${k==v?'on':''}" onclick="segPick(this,'${k}')">${l}</button>`).join('')}</div>`}
function segPick(btn,v){const s=btn.parentElement;s.dataset.v=v;s.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b===btn));cfgDirty()}
/* ── /heal: the self-repair loop (heal.py). What it found, each decision and why, every command it ran
   with its output: heal-log.jsonl, newest run first. ── */
const SRF={host:'',check:'',all:false,q:'',open:new Set()};
const SRST={repaired:'ok',failed:'bad',partial:'warn',needs_person:'bad',waiting:'warn',shadow:'warn',would:'info'};
const SRWORD={repaired:'repaired',failed:'failed',partial:'did what it could',needs_person:'needs a person',waiting:'waiting',shadow:'shadow',would:'would repair'};
function srApi(){const q=new URLSearchParams({n:'60'});if(SRF.host)q.set('host',SRF.host);if(SRF.check)q.set('check',SRF.check);
 if(SRF.all)q.set('all','1');if(SRF.q)q.set('q',SRF.q);
 return Promise.all([fetchJSON('/api/v1/heal'),fetchJSON('/api/v1/heal/log?'+q),fetchJSON('/api/v1/changes').catch(()=>({})),
  fetchJSON('/api/v1/self-checkpoints').catch(()=>({}))]).then(([s,l,c,sc])=>{s._log=l.runs||[];s._changes=c.changes||[];s._self=sc;return s})}
/* checkpoints: an ssh and a restic listing per machine, so only on request */
let SRCP=null;
async function srCheckpoints(refresh){SRCP={loading:true};srReload();
 SRCP=await fetchJSON('/api/v1/checkpoints'+(refresh?'?refresh=1':'')).catch(e=>({error:String(e)}));srReload()}
async function srRevert(id,what,data,btn){
 const keep=data?!confirm(`Undo change ${id}?\n\n${what}\n\nOK: also put back the data it kept (what was recorded since is moved aside).\nCancel: choose to keep today's data instead.`):false;
 if(keep&&!confirm(`Undo change ${id}, keeping today's data (the binaries and drop-in go back)?`))return;
 if(!data&&!confirm(`Undo change ${id}?\n\n${what}\n\nWhat ran before it goes back; it is checked, and put back as it is now if it does not work.`))return;
 btn.disabled=true;btn.textContent='undoing…';const r=await act('revertChange',{id,keep_data:keep});
 if(r&&r.error){btn.disabled=false;btn.textContent='undo';alert(r.error)}setTimeout(srReload,4000)}
async function srCheckpointNow(host,btn){if(!confirm(`Take a checkpoint of ${host} now (a system snapshot)?`))return;
 btn.disabled=true;btn.textContent='taking…';const r=await act('takeCheckpoint',{host});if(r&&r.error){btn.disabled=false;alert(r.error)}}
async function srReload(){const p=PANELS.find(x=>x.id=='heal');if(!p)return;const d=await p.api().catch(e=>({error:String(e)}));p._data=d;repaint('heal',d)}
function srSet(k,v){SRF[k]=v;srReload()}
function srToggle(id,open){if(open)SRF.open.add(id);else SRF.open.delete(id)}
async function srUnfreeze(btn){if(!confirm('Let self-repair act again? It froze because several repairs in a row failed.'))return;
 btn.disabled=true;const r=await act('healUnfreeze');if(r&&r.error){btn.disabled=false;alert(r.error)}setTimeout(srReload,2500)}
function srStep(r,s,i){const id=r.run+':'+i,k=s.kind;
 const kc={found:'warn',repair:'info',result:SRST[s.outcome]||'',decide:SRST[s.state]||'',error:'bad',freeze:'bad',cmd:'',change:'',note:'',skip:'warn'}[k]||'';
 const who=[s.host,s.check].filter(Boolean).map(esc).join(' · ');
 const body=k=='cmd'?`<details${SRF.open.has(id)?' open':''} ontoggle="srToggle('${esc(id)}',this.open)"><summary><code>${esc(s.text.length>160?s.text.slice(0,160)+'…':s.text)}</code> <span class="${s.rc===0?'ok':'bad'}">rc ${esc(s.rc)}</span></summary>${s.text.length>160?`<pre>${esc(s.text)}</pre>`:''}<pre>${esc(s.out||'(no output)')}</pre></details>`
  :esc(s.text)+(s.seconds!=null?` <span class=mut>(${esc(s.seconds)} s)</span>`:'');
 return `<tr><td>+${esc(Number(s.t).toFixed(1))}s</td><td class=k><span class="tag ${kc}">${esc(k=='result'?SRWORD[s.outcome]||k:k=='decide'?SRWORD[s.state]||k:k)}</span></td><td class=k>${who}</td><td>${body}</td></tr>`}
function srRun(r){const S=r.summary||{},id=r.run;
 const chips=S.skipped?`<span class="tag warn">${esc(S.skipped)}</span>`:['repaired','failed','partial','needs_person','waiting','shadow'].filter(k=>S[k]).map(k=>`<span class="tag ${SRST[k]}">${S[k]} ${esc(SRWORD[k])}</span>`).join('')||(S.found?`<span class="tag info">${S.found} found</span>`:'<span class="tag ok">all as expected</span>');
 return `<details class=srrun${SRF.open.has(id)?' open':''} ontoggle="srToggle('${esc(id)}',this.open)"><summary><b>${esc(fmtTs(r.ts_epoch))}</b><span class=mut>${esc(agoS(r.ts_epoch))}</span><span>${esc(r.trigger||'')}${r.host?' · '+esc(r.host):''}${r.dry?' · dry run':''}</span><span class=mut>${esc(r.seconds??'?')} s</span>${chips}</summary>
  <table class=srsteps>${(r.steps||[]).map((s,i)=>srStep(r,s,i)).join('')||'<tr><td></td><td colspan=3 class=mut>no steps</td></tr>'}
  <tr><td></td><td colspan=3 class=mut>checks: ${Object.entries(r.checks||{}).map(([k,v])=>esc(k)+' '+esc(v)+'s').join(' · ')||'—'}</td></tr></table></details>`}
const CHST={'in effect':'ok',undone:'mut',rolled_back:'warn',rollback_failed:'bad',refused:'mut','under a newer change':'mut'};
function srChanges(d){
 const rows=(d._changes||[]).slice(0,30).map(r=>{const cp=r.checkpoint||{},data=['prometheus','alertmanager'].includes(r.component);
  const cps=cp.snapshot?`<span title="${esc(cp.taken?'taken for it':'a snapshot from '+Math.round((cp.age||0)/60)+' min before')}">${esc(cp.snapshot)}</span>`:cp.error||cp.skipped?`<span class=mut>${esc(cp.error||cp.skipped)}</span>`:'<span class=mut>—</span>';
  return `<tr><td class="mut nowrap">${esc(agoS(r.ts_epoch||r.ts))}</td><td class=nowrap><b>${esc(r.host)}</b></td><td>${esc(r.what)}${(r.problems||[]).length?`<div class=mut>${esc(r.problems.join('; '))}</div>`:''}</td>
   <td><span class="tag ${CHST[r.state]||(String(r.state).startsWith('undid')?'info':'')}">${esc(r.state)}</span></td><td class=nowrap>${cps}</td>
   <td class=nowrap>${r.revertible?`<button class=silbtn onclick="srRevert('${esc(r.id)}','${esc(String(r.what).replace(/'/g,''))}',${data},this)" title="undo it: what ran before goes back (checked, and put back as it is now if that does not work)">undo</button>`:''}</td></tr>`}).join('');
 return `<div><h4>Changes</h4><p class="mut" style="margin:0 0 4px">What Noctyx changed on each machine, newest first, with the checkpoint it waited for. <b>undo</b> puts back what ran before the newest change in effect (<code>noctyx revert ID</code>).</p>
  ${rows?`<table class=srt><tr><th>when</th><th>machine</th><th>change</th><th>state</th><th>checkpoint</th><th></th></tr>${rows}</table>`:'<div class=mut>none recorded yet</div>'}</div>`}
function srCpSection(d){
 let body;
 if(!SRCP)body=`<button class=act onclick="srCheckpoints(false)">show checkpoints</button> <span class=mut>(asks every machine's repository)</span>`;
 else if(SRCP.loading)body='<div class=mut>asking each machine…</div>';
 else if(SRCP.error)body=`<div class=bad>${esc(SRCP.error)}</div>`;
 else body=Object.entries(SRCP.hosts||{}).map(([h,rows])=>`<div style="margin:6px 0"><b>${esc(h)}</b> <button class=silbtn onclick="srCheckpointNow('${esc(h)}',this)">checkpoint now</button>
   ${Array.isArray(rows)?(rows.length?`<table class=srt>${rows.slice(0,8).map(r=>`<tr><td class=nowrap><code>${esc(r.id)}</code></td><td class="mut nowrap">${esc(fmtTs(r.time))}</td><td>before ${esc((r.reasons||[]).join(', ')||'?')}${(r.changes||[]).length?' · '+r.changes.map(x=>esc(x.id+' '+x.what)).join('; '):''}</td></tr>`).join('')}</table>`:'<span class=mut>none yet</span>'):`<span class=bad>${esc(rows.error)}</span>`}</div>`).join('')
   +`<div class=mut>listed ${esc(agoS(SRCP.ts))} · <a class=lnk onclick="srCheckpoints(true)">ask again</a> · put a path back: Snapshot Browser, or <code>noctyx restore --host H --snapshot ID --path …</code></div>`;
 return `<div><h4>Checkpoints</h4><p class=mut style="margin:0 0 4px">Restic snapshots of a machine kept before Noctyx or apt changed it (a scheduled one from the last hours counts).</p>${body}</div>`}
function srSelf(S){
 const cur=S.current||{},g=cur.git||{},R=S.rollback||{};
 const rows=(S.checkpoints||[]).slice(0,6).map(x=>{const i=x.identity||{},gi=i.git||{};return `<tr><td class="mut nowrap">${esc(fmtTs(x.ts))}</td><td>${esc(i.version||'')} <code>${esc((gi.sha||'').slice(0,12))}</code>${gi.dirty?' <span class=warn>dirty</span>':''}</td><td>${esc(x.note||'')}</td></tr>`}).join('');
 return `<div><h4>Noctyx itself</h4><p class=mut style="margin:0 0 4px">Runs ${esc(cur.version||'?')} ${g.sha?'<code>'+esc(g.sha.slice(0,12))+'</code> on '+esc(g.branch||''):''} (${esc(cur.mode||'?')}). A self-checkpoint is kept when it starts on new code.</p>
  ${rows?`<table class=srt>${rows}</table>`:'<div class=mut>none yet</div>'}
  <div class=mut style="margin-top:4px">${R.target?`Going back to ${esc(R.target)}: <code>sudo noctyx self-rollback --yes</code> on the head (it restarts this console, so it is not a button).`:esc((R.problems||[]).join(' '))}</div></div>`}
function rHeal(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const P=d.policy||{},U=P.upgrades||{},fr=d.frozen,pb=P.playbooks||{};
 const bar=`<div class=srbar><span class="tag ${P.auto?'ok':'warn'}">${P.auto?'repairs on':'report only'}</span>
  ${fr?`<span class="tag bad">frozen</span><span class=bad>${esc(fr.why)}</span><button class=act onclick="srUnfreeze(this)">unfreeze</button>`:''}
  <span>${esc(d.actions_1h||0)} of ${esc(P.budget_per_hour)} repairs this hour</span>
  <span>automatic upgrades ${U.auto?'<b>on</b>, '+esc(U.window):'off'}</span>
  <span class=mut>last run ${esc(agoS(d.ts))}${d.skipped?' · '+esc(d.skipped):''}</span><span class=hspacer></span>
  <button class=act onclick="healNow(this);setTimeout(srReload,4000)" title="run noctyx heal now">repair now</button><a class=lnk href="/config#sec-heal">policy →</a></div>`;
 const probs=(d.problems||[]).map(p=>`<tr><td class=nowrap><b>${esc(p.host)}</b></td><td>${esc(p.what)}${p.detail?`<div class=mut>${esc(p.detail)}</div>`:''}</td><td><span class="tag ${SRST[p.state]||(p.needs_person?'bad':'warn')}">${esc(SRWORD[p.state]||(p.needs_person?'needs a person':'next try'))}</span></td><td>${esc(p.why||p.how||'')}</td><td class=mut>${esc(agoS(p.since))}</td></tr>`).join('');
 const reps=(d.repairs||[]).slice(-12).reverse().map(r=>`<tr><td class="mut nowrap">${esc(agoS(r.ts_epoch||r.ts))}</td><td class=nowrap><b>${esc(r.host)}</b></td><td>${esc(r.how)}</td><td class="${r.ok?'ok':r.ok===null?'warn':'bad'}">${r.ok?'✓':r.ok===null?'·':'✗'} ${esc(r.detail||'')}</td></tr>`).join('');
 const pbs=Object.entries(d.playbooks||{}).map(([k,t])=>`<span title="${esc(t)}" class="${pb[k]=='off'?'mut':pb[k]=='shadow'?'warn':''}">${esc(k)}: ${esc(pb[k]||'on')}</span>`).join('');
 const opt=(v,l,cur)=>`<option value="${esc(v)}"${v==cur?' selected':''}>${esc(l)}</option>`;
 const filt=`<div class=srf><select onchange="srSet('host',this.value)">${opt('','every machine',SRF.host)}${(d.hosts||[]).map(h=>opt(h,h,SRF.host)).join('')}</select>
  <select onchange="srSet('check',this.value)">${opt('','every playbook',SRF.check)}${Object.keys(d.playbooks||{}).map(k=>opt(k,k,SRF.check)).join('')}</select>
  <label><input type=checkbox${SRF.all?' checked':''} onchange="srSet('all',this.checked)"> also the runs that found nothing</label>
  <input type=search placeholder="search the log" value="${esc(SRF.q)}" onchange="srSet('q',this.value.trim())"></div>`;
 const log=(d._log||[]).map(srRun).join('')||`<div class=mut>${SRF.all||SRF.host||SRF.check||SRF.q?'no run matches':'no run found anything yet (tick “also the runs that found nothing” to see every run)'}</div>`;
 return `<div class=sr>${bar}
  <div><h4>Open problems</h4>${probs?`<table class=srt><tr><th>machine</th><th>what</th><th>state</th><th>why</th><th>since</th></tr>${probs}</table>`:'<div class=ok>none: everything Noctyx manages is as expected</div>'}</div>
  <div><h4>Repairs, last 24 h</h4>${reps?`<table class=srt>${reps}</table>`:'<div class=mut>none</div>'}</div>
  <div><h4>Playbooks</h4><div class=srpb>${pbs}</div></div>
  ${srChanges(d)}
  ${srCpSection(d)}
  ${srSelf(d._self||{})}
  <div><h4>Log</h4>${filt}<div class=sr style="gap:6px;margin-top:8px">${log}</div></div></div>`}
function rConfig(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const P=d.policy||{},R=d.retention||{},HP=P.hosts||{};
 const dbChips=n=>{const D=(d.dumps||{})[n]||{},last={};((D.last||{}).results||[]).forEach(r=>last[r.id]=r);
  if(D.error)return `<span class=bad>scan failed: ${esc(D.error)}</span>`;
  if(!D.discovered)return '<span class=mut>not scanned yet — Apply</span>';
  if(!D.discovered.length)return '<span class=mut>none found</span>';
  const ts=(D.last||{}).ts;
  return D.discovered.map(x=>{const r=last[x.id]||{},nm=x.id.replace(/^[a-z]+:/,'');
   const st=r.ok===true?`<span class=ok>✓ ${r.bytes?esc(fmtBytes(r.bytes)):'saved'}</span>`:r.ok===false?`<span class=bad title="${esc(r.error||'')}">✗ failed</span>`:r.skipped?'<span class=mut>skipped</span>':'<span class=mut>not run</span>';
   return `<label class=dbc title="${esc(x.label)}${r.error?' — '+esc(r.error):''}"><input type=checkbox data-host="${esc(n)}" data-db="${esc(x.id)}"${x.enabled?' checked':''} onchange="cfgDirty()"><b>${esc(nm)}</b> <span class=mut>${esc(x.kind)}</span> ${st}</label>`}).join('')
   +(ts?`<span class="mut dbts">last run ${esc(agoS(ts))}</span>`:'')};
 const rows=(d.hosts||[]).map(n=>{const o=HP[n]||{},e=(d.effective||{})[n]||{},fe={docker_volumes:P.docker_volumes,db_dumps:P.db_dumps};
  return `<tr><td><b>${esc(n)}</b><div class=mut style="font-size:10px">${esc(((d.profiles||{})[n]||[]).join(' · '))}</div></td>
   <td>${triSel('cf-dv-'+n,o.docker_volumes,fe.docker_volumes)}</td>
   <td>${triSel('cf-db-'+n,o.db_dumps,fe.db_dumps)}</td>
   <td class=dbs>${dbChips(n)}</td>
   <td><textarea id="cf-ex-${esc(n)}" rows=2 placeholder="host-only patterns" oninput="cfgDirty()">${esc((o.exclude||[]).join('\n'))}</textarea></td></tr>`}).join('');
 const state=CFGDIRTY?'<span id=cf-state class=warn>unsaved changes</span>':`<span id=cf-state class=mut>${d.stored?'saved in the site config':'defaults (nothing saved yet)'}</span>`;
 return `<div class=cfg>
 <div class=cfgbar>${state}<span class=hspacer></span>
  <button class=act onclick="cfgSave()">save &amp; apply</button>
  <button class=act onclick="cfgRun('policyApply',{},'apply')" title="push the saved policy to every host again and rescan databases">re-apply</button>
  <button class=act onclick="cfgRun('dumpsNow',{},'dump')" title="dump every enabled database now, without a backup">dump databases now</button>
  <button class=lnk onclick="CFGDIRTY=false;tick(PANELS.find(x=>x.id=='config'))">discard / reload</button></div>
 <div class=cfgsec id=sec-backups><h4>All hosts</h4><p class=gnote>What every host's backup covers, unless a host below says otherwise.</p>
  <div class=setrow><label>Docker volumes</label><div><input type=checkbox class=sw id=cf-dv${P.docker_volumes?' checked':''} onchange="cfgDirty()"></div>
   <div class=hint>Back up /var/lib/docker/volumes. Image layers and container filesystems stay excluded: they are rebuilt, not restored.</div></div>
  <div class=setrow><label>Database dumps</label><div><input type=checkbox class=sw id=cf-db${P.db_dumps?' checked':''} onchange="cfgDirty()"></div>
   <div class=hint>Before each <b>system</b> backup, dump every database found (postgres, mariadb/mysql, redis/valkey SAVE, sqlite) into __NODE_LIB__/dumps, so the snapshot holds a consistent copy.</div></div>
  <div class=setrow><label>Extra excludes</label><textarea id=cf-ex rows=3 oninput="cfgDirty()" placeholder="/root/scratch/**&#10;!/var/cache/apt/archives">${esc((P.exclude||[]).join('\n'))}</textarea>
   <div class=hint>One restic pattern per line, on every host. <code>!pattern</code> re-includes something the baseline excludes.</div></div>
  <div class=setrow><label>Extra SQLite files</label><textarea id=cf-sq rows=2 oninput="cfgDirty()" placeholder="/opt/app/data/app.db">${esc((P.sqlite||[]).join('\n'))}</textarea>
   <div class=hint>Copied safely (online backup API) before each backup. Gitea's and Grafana's are found automatically.</div></div></div>
 <div class=cfgsec><h4>Per host</h4>
  <table class=cfgt><tr><th>host</th><th>docker volumes</th><th>db dumps</th><th>databases <span class=mut>(untick to skip)</span></th><th>extra excludes</th></tr>${rows}</table>
  ${(d.not_covered||[]).length?`<div class=hint>not backed up by Noctyx: ${esc(d.not_covered.join(', '))} (no restic profile; see the site config)</div>`:''}</div>
 <div class=cfgsec id=sec-retention><h4>Retention</h4><p class=gnote>Snapshots kept per host and profile. The monthly prune applies it, gated by a repository check; the weekly run is a dry run you can read in Runs.</p>
  <div class=setrow><label>Daily</label><div><input id=cf-rd type=number min=0 max=999 value="${esc(R.daily??'')}" oninput="cfgDirty()"></div><div class=hint>The newest snapshot of each of this many days.</div></div>
  <div class=setrow><label>Weekly</label><div><input id=cf-rw type=number min=0 max=999 value="${esc(R.weekly??'')}" oninput="cfgDirty()"></div><div class=hint>Then one a week for this many weeks.</div></div>
  <div class=setrow><label>Monthly</label><div><input id=cf-rm type=number min=0 max=999 value="${esc(R.monthly??'')}" oninput="cfgDirty()"></div><div class=hint>Then one a month for this many months. How often backups run: the <a class=lnk href="/#panel=schedule">Schedule panel</a>.</div></div></div>
 ${hfSection(d.hf||{})}
 ${asSection(d.assistant||{})}
 ${srSection(d.heal||{})}
 ${cpSection(d.checkpoints||{})}
 <div class=cfgsec><details><summary>baseline exclusions — always applied (exclusions-common.conf; a profile with its own list in the site's etc dir uses that instead)</summary>
  <div class=cfgcols><pre class=cfgpre>${esc(d.baseline&&d.baseline.common||'')}</pre>${Object.entries((d.baseline||{}).profiles||{}).map(([k,v])=>`<pre class=cfgpre><b>${esc(k)}</b>\n${esc(v)}</pre>`).join('')}</div></details></div>
 <pre id=cf-out class="cfgpre${CFGJOB?'':' hide'}">${esc(CFGJOB?CFGJOB.text:'')}</pre>
 </div>`}
function hfSection(F){
 const S=F.status||{},T=S.totals||{},HS=S.hosts||{},ex=new Set(F.exclude_hosts||[]);
 const hosts=(F.sources||[]).map(n=>{const h=HS[n]||{};
  const last=h.error?`<span class=bad title="${esc(h.error)}">failed</span>`:h.ts?`${esc(agoS(h.ts))} · +${h.new_blobs||0} files (${esc(fmtBytes(h.new_bytes||0))})${h.bad_hash?` · <span class=bad>${h.bad_hash} bad hash</span>`:''}`:'<span class=mut>never</span>';
  return `<label class=dbc title="harvest model files from ${esc(n)}"><input type=checkbox data-hfhost="${esc(n)}"${ex.has(n)?'':' checked'} onchange="cfgDirty()"><b>${esc(n)}</b> <span class=mut>${last}</span></label>`}).join('');
 return `<div class=cfgsec id=sec-mirror><h4>Hugging Face model mirror</h4><p class=gnote>One copy of every model on the cluster, on __NAS__. Hosts download from it instead of huggingface.co; anything it lacks passes through, and when huggingface.co is unreachable the stored copy is served.</p>
  <div class=setrow><label>Status</label><div class=hfline>server <b class="${F.service=='active'?'ok':'bad'}">${esc(F.service||'?')}</b> · store <b>${T.repos??0}</b> repos, <b>${T.blobs??0}</b> files, <b>${esc(fmtBytes(T.bytes||0))}</b>${S.ts?` · harvested ${esc(agoS(S.ts))}`:''}${F.harvesting?' · <span class=warn>harvest running</span>':''}
   <button class=act onclick="cfgRun('hfHarvest',{},'harvest')" title="copy new model files from the hosts now (yields to backups)">harvest now</button></div></div>
  <div class=setrow><label>Endpoint</label><div><code>HF_ENDPOINT=${esc(F.endpoint||'')}</code></div><div class=hint>Per process: <code>export HF_ENDPOINT=…</code>; a container: <code>-e HF_ENDPOINT=…</code>. Recipe deploys use it by themselves.</div></div>
  <div class=setrow><label>Nightly harvest</label><div><input type=checkbox class=sw id=cf-hf${F.harvest?' checked':''} onchange="cfgDirty()"></div><div class=hint>After the backups: copies only files the store lacks, checks each against its hash, pauses in the backup window.</div></div>
  <div class=setrow><label>Harvest from</label><div class=dbs>${hosts}</div><div class=hint>Untick a host to leave its caches out.</div></div>
  <div class=setrow><label>Bandwidth limit</label><div><input id=cf-hfbw type=number min=0 max=10000 value="${esc(F.bwlimit_mb??0)}" oninput="cfgDirty()"> MB/s</div><div class=hint>Per copy; 0 = no limit.</div></div></div>`}
function asSection(A){
 const eps=(A.endpoints||[]).map((e,i)=>`<option value="${i}">${esc(e.host)} · ${esc(e.model||'?')} · ${esc(e.engine||'')} — ${esc(e.url)}</option>`).join('');
 return `<div class=cfgsec id=sec-assistant><h4>Assistant LLM</h4><p class=gnote>The model Noctyx asks to explain findings, triage alerts and write the morning brief. Text only: it never runs anything.</p>
  <div class=setrow><label>Use the assistant</label><div><input type=checkbox class=sw id=as-on${A.enabled===true?' checked':''} onchange="cfgDirty()"></div></div>
  <div class=setrow><label>Endpoint</label><div><input id=as-url class=asin value="${esc(A.url||'')}" placeholder="http://host:port/v1" oninput="cfgDirty()">
    ${eps?`<select id=as-pick onchange="asPick(this.value)" style="margin-top:6px"><option value="">or pick one Noctyx sees serving…</option>${eps}</select>`:''}</div>
   <div class=hint>An OpenAI-compatible base URL, ending in /v1.</div></div>
  <div class=setrow><label>Model</label><div><input id=as-model class=asin value="${esc(A.model||'')}" placeholder="(as served)" oninput="cfgDirty()" list=as-models><datalist id=as-models></datalist></div><div class=hint>The served model id; empty = the endpoint's first. <b>Test</b> lists what it serves.</div></div>
  <div class=setrow><label>Timeout</label><div><input id=as-to type=number min=5 max=600 value="${esc(A.timeout_seconds??60)}" oninput="cfgDirty()"> s</div></div>
  <div class=setrow><label>Fallback</label><div><input type=checkbox class=sw id=as-fb${A.fallback!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>When this one does not answer, ask any other model Noctyx sees serving.</div></div>
  <div class=setrow><label>API key</label><div class=hfline><b class="${A.has_key?'ok':'mut'}">${A.has_key?'set':'none'}</b>
   <input id=as-key type=password autocomplete=off placeholder="new key">
   <button class=act onclick="asKey(false)">set key</button>${A.has_key?'<button class=lnk onclick="asKey(true)">clear key</button>':''}</div><div class=hint>Stored root-only on the head node; never shown or logged.</div></div>
  <div class=setrow><label>Ask: site notes</label><div><textarea id=as-notes class=asin rows=4 maxlength=4000 placeholder="e.g. gpu-03 is a test box: its alerts can wait. Backups to the NAS pause on Sundays." oninput="cfgDirty()">${esc(A.ask_notes||'')}</textarea></div>
   <div class=hint>Added to the end of Ask Noctyx's system prompt, under “Notes from this site's admins”. At most 4,000 characters.</div></div>
  <div class=setrow><label>Ask: system prompt</label><div><textarea id=as-prompt class=asin rows=8 maxlength=16000 placeholder="(empty: the built-in prompt, below)" oninput="cfgDirty()" >${esc(A.ask_prompt||'')}</textarea>
    <div class=hfline style="margin-top:6px"><button class=lnk onclick="asBuiltin()" title="put the built-in prompt above, to edit">start from the built-in</button><button class=lnk onclick="$('as-prompt').value='';cfgDirty()">use the built-in</button></div>
    <details style="margin-top:6px"><summary class=mut>the built-in prompt</summary><pre class=asbuiltin>${esc(A.ask_builtin||'')}</pre></details></div>
   <div class=hint>Your own replaces the built-in one. Noctyx fills in ${(A.ask_fields||[]).map(f=>'<code>{'+esc(f)+'}</code>').join(' ')}; write a literal brace as <code>{{</code> or <code>}}</code>. It changes only what Ask is told: its tools stay read-only and your role's. A new conversation uses it.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="asTest()" title="one short request with the values above, before saving">test</button>
   <button class=act onclick="asSave()">save assistant</button> <span id=as-res class=mut></span></div></div></div>`}
function srSection(H){
 const P=H.policy||{},U=P.upgrades||{},pb=P.playbooks||{};
 const rows=Object.entries(H.playbooks||{}).map(([k,x])=>`<tr><td><b>${esc(k)}</b></td><td><select id="sr-pb-${esc(k)}" onchange="cfgDirty()">${['on','shadow','off'].map(m=>`<option${(pb[k]||x.default)==m?' selected':''}>${m}</option>`).join('')}</select></td><td class=mut>${esc(x.does)}</td></tr>`).join('');
 const slots=(H.slots||[]).map(n=>`<label class=dbc><input type=checkbox data-srslot="${esc(n)}"${(P.restart_slots||[]).includes(n)?' checked':''} onchange="cfgDirty()"><b>${esc(n)}</b></label>`).join('')||'<span class=mut>no recipe slots</span>';
 const comps=(H.upgradable||[]).map(n=>`<label class=dbc><input type=checkbox data-srcomp="${esc(n)}"${(U.components||[]).includes(n)?' checked':''} onchange="cfgDirty()"><b>${esc(n)}</b></label>`).join('');
 return `<div class=cfgsec id=sec-heal><h4>Self-repair</h4><p class=gnote>What <code>noctyx heal</code> may repair on its own: every 5 minutes, and at once when an alert it has a playbook for fires. Each repair is checked and bounded; the <a class=lnk href="/heal">Self-repair page</a> shows every run, decision and command.</p>
  <div class=setrow><label>Repair on its own</label><div><input type=checkbox class=sw id=sr-auto${P.auto!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>Off: it only reports what it would do.</div></div>
  <div class=setrow><label>Repairs per hour</label><div><input id=sr-budget type=number min=1 max=100 value="${esc(P.budget_per_hour??10)}" oninput="cfgDirty()"></div><div class=hint>Across the fleet. More wait for the next hour.</div></div>
  <div class=setrow><label>Freeze after</label><div><input id=sr-freeze type=number min=1 max=20 value="${esc(P.freeze_after??3)}" oninput="cfgDirty()"> failed repairs in a row</div><div class=hint>Then no repair runs until someone unfreezes it (the Self-repair page).</div></div>
  <div class=setrow><label>Disk full at</label><div><input id=sr-disk type=number min=50 max=99 value="${esc(P.disk_pct??92)}" oninput="cfgDirty()"> %</div><div class=hint>A root filesystem over this: the journal is vacuumed to 1 GiB and apt's package cache cleared. Docker is never pruned (a prune removes stopped containers someone means to start again); what is still full goes to a person with its largest directories.</div></div>
  <div class=setrow><label>Playbooks</label><div class=srscroll><table class=cfgt><tr><th>playbook</th><th>mode</th><th>what it does</th></tr>${rows}</table></div><div class=hint><b>shadow</b>: logs what it would do and leaves it to a person; <b>off</b>: not even checked.</div></div>
  <div class=setrow><label>Units it may restart</label><div><textarea id=sr-units rows=3 oninput="cfgDirty()" placeholder="nginx.service&#10;myapp-*.service">${esc((P.restart_units||[]).join('\n'))}</textarea></div><div class=hint>One unit name or pattern per line. A failed unit not listed is left to a person (with its last log lines). Never restarted, whatever is listed: ${(H.never_units||[]).map(x=>'<code>'+esc(x)+'</code>').join(' ')}.</div></div>
  <div class=setrow><label>Model servers it may restart</label><div class=dbs>${slots}</div><div class=hint>A ticked slot that stops answering for 15 minutes is started again with <code>noctyx recipe-start</code>, but only when Noctyx has seen that slot's own unit serve in the last 7 days. Stop a slot through Noctyx (Recipes, or <code>noctyx recipe-stop</code>) and it stays stopped.</div></div>
  <div class=setrow><label>Automatic upgrades</label><div><input type=checkbox class=sw id=sr-up${U.auto?' checked':''} onchange="cfgDirty()"></div><div class=hint><b>Off by default.</b> On: a component older than Noctyx's pin is upgraded inside the window, one machine per run, nodes before the head, as a change that checks itself and rolls back. A release that fails anywhere is not tried again on its own.</div></div>
  <div class=setrow><label>Upgrade window</label><div><input id=sr-win value="${esc(U.window||'03:00-05:00')}" placeholder="03:00-05:00" oninput="cfgDirty()"></div><div class=hint>HH:MM-HH:MM, the head's local time.</div></div>
  <div class=setrow><label>Upgrade</label><div class=dbs>${comps}</div></div>
  <div class=setrow><label>Between machines</label><div><input id=sr-soak type=number min=0 max=10080 value="${esc(U.soak_minutes??60)}" oninput="cfgDirty()"> min</div><div class=hint>After one machine's upgrade, the next waits this long.</div></div>
  <div class=setrow><label>Major versions</label><div><input type=checkbox class=sw id=sr-major${U.major?' checked':''} onchange="cfgDirty()"></div><div class=hint>Off: a pin in a new major version (Prometheus 2 → 3) waits for a person.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="srSave()">save self-repair</button></div></div></div>`}
function cpSection(K){
 const P=K.policy||{};
 const hist=(K.history||[]).slice(0,15).map(h=>`<tr><td class="mut nowrap">${esc(fmtTs(h.ts))}</td><td>${h.why.startsWith('before')?esc(h.why.replace(/-/g,' ')):`before a <b>${esc(h.why)}</b> change`}</td><td class=nowrap><button class=lnk onclick="cpDiff('${esc(h.name)}')">what restoring changes</button> <button class=silbtn onclick="cpRestore('${esc(h.name)}')">restore</button></td></tr>`).join('');
 return `<div class=cfgsec id=sec-checkpoints><h4>Checkpoints and history</h4><p class=gnote>A checkpoint is a restic snapshot of a machine kept before something changes it, to go back to (<a class=lnk href="/heal">Self-repair › Changes</a> undoes a change).</p>
  <div class=setrow><label>Before Noctyx changes</label><div><input type=checkbox class=sw id=cp-ch${P.before_changes!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>An upgrade or undo waits for a checkpoint of its machine.</div></div>
  <div class=setrow><label>Before apt</label><div><input type=checkbox class=sw id=cp-apt${P.before_apt!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>A hook in each node's apt takes one before packages change (a failed one never stops apt), and marks when apt finished.</div></div>
  <div class=setrow><label>A snapshot counts for</label><div><input id=cp-age type=number min=0 max=168 value="${esc(P.max_age_hours??6)}" oninput="cfgDirty()"> h</div><div class=hint>A system snapshot this recent is the checkpoint as it is; else a new one is taken (an incremental: minutes).</div></div>
  <div class=setrow><label>Wait at most</label><div><input id=cp-to type=number min=1 max=240 value="${esc(P.timeout_minutes??45)}" oninput="cfgDirty()"> min, apt <input id=cp-ato type=number min=1 max=120 value="${esc(P.apt_timeout_minutes??20)}" oninput="cfgDirty()"> min</div></div>
  <div class=setrow><label>Required</label><div><input type=checkbox class=sw id=cp-req${P.require?' checked':''} onchange="cfgDirty()"></div><div class=hint>On: a change whose checkpoint cannot be made does not run. Off: it runs and says why there is none.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="cpSave()">save checkpoints</button></div></div>
  <div class=setrow><label>Site config history</label><div>${hist?`<table class=srt>${hist}</table>`:'<span class=mut>a copy is kept before each rewrite; none yet</span>'}<pre id=cp-diff class="cfgpre hide"></pre></div><div class=hint>The last 50 copies. Restoring keeps the current file first.</div></div></div>`}
function cpSave(){cfgRun('setCheckpoints',{checkpoints:{before_changes:$('cp-ch').checked,before_apt:$('cp-apt').checked,max_age_hours:Number($('cp-age').value),
 timeout_minutes:Number($('cp-to').value),apt_timeout_minutes:Number($('cp-ato').value),require:$('cp-req').checked}},'save checkpoints')}
async function cpDiff(name){const o=$('cp-diff');o.classList.remove('hide');o.textContent='…';
 const j=await fetchJSON('/api/v1/config-history?diff='+encodeURIComponent(name)).catch(e=>({error:String(e)}));o.textContent=j.error||j.diff||'(the same as now)'}
function cpRestore(name){if(!confirm(`Put the site config back as it was (${name})? The current one is kept first.`))return;cfgRun('configRestore',{name},'restore the site config')}
function srVals(){const H=(PANELS.find(x=>x.id=='config')._data||{}).heal||{},pb={};
 Object.keys(H.playbooks||{}).forEach(k=>{const e=$('sr-pb-'+k);if(e)pb[k]=e.value});
 const n=id=>Number($(id).value);
 return {auto:$('sr-auto').checked,budget_per_hour:n('sr-budget'),freeze_after:n('sr-freeze'),disk_pct:n('sr-disk'),playbooks:pb,
  restart_units:lines($('sr-units').value),restart_slots:[...document.querySelectorAll('input[data-srslot]')].filter(c=>c.checked).map(c=>c.dataset.srslot),
  upgrades:{auto:$('sr-up').checked,window:$('sr-win').value.trim(),components:[...document.querySelectorAll('input[data-srcomp]')].filter(c=>c.checked).map(c=>c.dataset.srcomp),
   soak_minutes:n('sr-soak'),major:$('sr-major').checked}}}
function srSave(){const v=srVals();
 if(v.upgrades.auto&&!confirm('Turn on automatic upgrades? Noctyx then upgrades '+(v.upgrades.components.join(', ')||'nothing')+' on its own inside '+v.upgrades.window+'.'))return;
 cfgRun('setHeal',{heal:v},'save self-repair')}
function asPick(i){const A=(PANELS.find(x=>x.id=='config')._data||{}).assistant||{},e=(A.endpoints||[])[i];if(!e)return;
 $('as-url').value=e.url;$('as-model').value=e.model||'';cfgDirty()}
function asVals(){return {enabled:$('as-on').checked,url:$('as-url').value.trim(),model:$('as-model').value.trim(),
 timeout_seconds:Number($('as-to').value)||60,fallback:$('as-fb').checked,ask_notes:$('as-notes').value,ask_prompt:$('as-prompt').value}}
function asBuiltin(){const A=(PANELS.find(x=>x.id=='config')._data||{}).assistant||{};$('as-prompt').value=A.ask_builtin||'';cfgDirty()}
/* downloads need an operator. Signed in (or open access): the link just works. Token mode: a browser
   link cannot carry the bearer token, so ask for a 60 s signed ticket and follow the link with it. */
function dlGo(ev,a){
 if(!ME||ME.mode!='token')return true;
 ev.preventDefault();const tok=localStorage.getItem(LS_TOK)||'';
 fetch('/api/v1/download-ticket',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
  body:JSON.stringify({host:a.dataset.h,snapshot:a.dataset.s,path:a.dataset.p,dir:!!a.dataset.d})})
  .then(r=>r.json().then(j=>{if(r.ok&&j.t)location.href=a.href+'&t='+encodeURIComponent(j.t);else alert('download refused: '+(j.error||r.status)+(tok?'':' (set the token first)'))}));
 return false}
async function asPost(path,body){const tok=localStorage.getItem(LS_TOK)||'';
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify(body)});
 return r.json().catch(()=>({error:'bad response'}))}
async function asTest(){const v=asVals(),o=$('as-res');o.className='mut';o.textContent='testing…';
 const j=await asPost('/api/v1/assistant-test',v).catch(e=>({error:String(e)}));
 const dl=$('as-models');if(dl&&j.models)dl.innerHTML=j.models.map(m=>`<option value="${esc(m)}">`).join('');
 o.className=j.ok?'ok':'bad';
 o.textContent=j.ok?`✓ ${j.model} answered in ${j.ms} ms: “${j.text}”${j.models?' · serves '+j.models.join(', '):''}`:`✗ ${j.error||'no answer'}${j.models?' · serves '+j.models.join(', '):''}`}
function asSave(){const v=asVals();if(!v.url&&!v.fallback){alert('set an endpoint, or allow the fallback');return}
 cfgRun('setAssistant',{assistant:v},'save assistant')}
async function asKey(clear){const k=($('as-key')||{}).value||'';if(!clear&&k.trim().length<8){alert('the key is shorter than 8 characters');return}
 if(clear&&!confirm('Remove the assistant API key?'))return;
 const j=await asPost('/api/v1/assistant-key',{key:clear?null:k.trim()}).catch(e=>({error:String(e)}));
 $('as-key').value='';const o=$('as-res');o.className=j.ok?'ok':'bad';o.textContent=j.ok?(clear?'key removed':'key saved'):'✗ '+(j.error||'failed');
 const p=PANELS.find(x=>x.id=='config');const data=await p.api().catch(()=>null);if(data){p._data=data;if(!CFGDIRTY)repaint('config',data)}}
function cfgCollect(){
 const d=PANELS.find(x=>x.id=='config')._data||{},P=d.policy||{},HP=P.hosts||{};
 const tri=v=>v=='on'?true:v=='off'?false:undefined;
 const pol={docker_volumes:$('cf-dv').checked,db_dumps:$('cf-db').checked,exclude:lines($('cf-ex').value),
  sqlite:lines($('cf-sq').value),skip_dumps:P.skip_dumps||[],hosts:{}};
 for(const n of d.hosts||[]){const o={},old=HP[n]||{};
  const dv=tri($(`cf-dv-${n}`).dataset.v),db=tri($(`cf-db-${n}`).dataset.v);if(dv!==undefined)o.docker_volumes=dv;if(db!==undefined)o.db_dumps=db;
  const ex=lines($(`cf-ex-${n}`).value);if(ex.length)o.exclude=ex;
  const seen=new Set(),skip=[];document.querySelectorAll(`input[data-host="${CSS.escape(n)}"][data-db]`).forEach(c=>{seen.add(c.dataset.db);if(!c.checked)skip.push(c.dataset.db)});
  (old.skip_dumps||[]).forEach(x=>{if(!seen.has(x))skip.push(x)});   /* keep skips for databases not running right now */
  if(skip.length)o.skip_dumps=skip;if((old.sqlite||[]).length)o.sqlite=old.sqlite;
  if(Object.keys(o).length)pol.hosts[n]=o}
 const num=id=>{const v=$(id).value.trim();return v===''?null:Number(v)};
 const ret={daily:num('cf-rd'),weekly:num('cf-rw'),monthly:num('cf-rm')};
 const F=d.hf||{},bw=Number(($('cf-hfbw')||{}).value||0);
 const hf=$('cf-hf')?{harvest:$('cf-hf').checked,bwlimit_mb:Number.isInteger(bw)?bw:0,
  exclude_hosts:[...document.querySelectorAll('input[data-hfhost]')].filter(c=>!c.checked).map(c=>c.dataset.hfhost)}:undefined;
 return {policy:pol,retention:Object.values(ret).every(v=>Number.isInteger(v))?ret:null,hf_mirror:hf}}
async function cfgSave(){
 const b=cfgCollect();
 if(b.retention===null){alert('retention: daily, weekly and monthly must be whole numbers');return}
 const old=(PANELS.find(x=>x.id=='config')._data||{}).retention||{};
 if(['daily','weekly','monthly'].some(k=>b.retention[k]<(old[k]||0))&&!confirm('Retention goes DOWN: the next prune deletes snapshots that are kept today. Save?'))return;
 await cfgRun('setPolicy',b,'save')}
async function cfgRun(action,params,what){
 const out=$('cf-out');CFGJOB={text:what+': starting…'};if(out){out.classList.remove('hide');out.textContent=CFGJOB.text}
 const tok=localStorage.getItem(LS_TOK)||'';
 let r;try{r=await fetch('/api/v1/action',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({action,params})})}
 catch(e){CFGJOB.text=what+': '+e;if(out)out.textContent=CFGJOB.text;return}
 const j=await r.json().catch(()=>({error:'bad response'}));
 if(!r.ok||!j.id){CFGJOB.text=`${what} refused: ${j.error||r.status}`;if(out){out.textContent=CFGJOB.text;out.classList.add('bad')}return}
 if(out)out.classList.remove('bad');
 for(;;){await new Promise(z=>setTimeout(z,1200));
  let s;try{s=await fetchJSON('/api/v1/job/'+j.id)}catch(e){continue}
  CFGJOB.text=`${what}${s.done?(s.rc===0?' — done':' — FAILED (rc='+s.rc+')'):' — running…'}\n${(s.output||'').trim()}`;
  const o=$('cf-out');if(o){o.textContent=CFGJOB.text;o.classList.toggle('bad',s.done&&s.rc!==0)}
  if(s.done){if(s.rc===0&&(action=='setPolicy'||action=='setAssistant'||action=='setHeal'||action=='setCheckpoints'||action=='configRestore'))CFGDIRTY=false;
   const p=PANELS.find(x=>x.id=='config');const data=await p.api().catch(()=>null);if(data){p._data=data;if(!CFGDIRTY)repaint('config',data)}
   return}}}
/* ── recipes (/recipes): each GPU slot serves one recipe; deploy builds the next one while the
   current keeps serving, then switches under the slot's GPU lock and switches back on failure. ── */
let RCJOB=null;const RSEL={};
const rcLabel=r=>!r?'nothing':r.adopted?r.name:`${r.recipe}@${(r.sha||'').slice(0,12)}`;
function rRecipes(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const cards=Object.entries(d.slots||{}).map(([n,s])=>{const L=s.live||{},c=s.current,pv=s.previous,sel=RSEL[n]||{};
  const opts=(s.recipes||[]).map(r=>`<option${(sel.recipe||'')==r?' selected':''}>${esc(r)}</option>`).join('');
  const prep=Object.entries(s.prepared||{}).map(([r,x])=>`${esc(r)}@${esc((x.sha||'').slice(0,12))} <span class=mut>${esc(agoS(x.prepared_ts))}</span>`).join(' · ');
  const hist=(s.history||[]).map(h=>`<tr><td>${esc(agoS(h.ts))}</td><td>${esc(h.event)}</td><td>${esc(h.to||'')}</td><td class="${h.rc===0?'ok':'bad'}">${h.rc===0?'ok':'rc '+esc(h.rc)}${h.rolled_back?' · rolled back':''}${h.down_s!=null?' · down '+esc(h.down_s)+'s':''}</td></tr>`).join('');
  return `<div class=rslot>
  <div class=rh><span class="dot ${L.ok?'ok':'bad'}"></span><b>${esc(n)}</b><span class=mut>${esc(s.head)}${s.worker?' + '+esc(s.worker):''} · :${esc(s.port)}</span><span class=hspacer></span>${s.busy?`<span class="tag warn" title="${esc(s.busy)}">busy</span>`:''}</div>
  <div>deployment <b>${esc(rcLabel(c))}</b>${c&&c.deployed_ts?` <span class=mut>${esc(agoS(c.deployed_ts))}</span>`:''}${c&&c.adopted?' <span class=mut>(adopted: from before Noctyx; '+esc(c.unit)+')</span>':''}${c&&c.state=='stopped'?' <span class=warn>stopped</span>':''}</div>
  <div class=rm>${L.ok?`<span class=ok>serving</span> ${esc((L.models||[]).join(', '))}${L.connections!=null?` <span class=mut>· ${esc(L.connections)} connected</span>`:''}`:`<span class=bad>not serving</span> <span class=mut>${esc(L.error||L.status||'')}</span>`}</div>
  ${s.drift?`<div class=drift>⚠ ${esc(s.drift)}</div>`:''}
  ${prep?`<div class=mut style="font-size:11px">prepared: ${prep}</div>`:''}
  <div class=ract>
   <select id="rs-r-${esc(n)}" onchange="RSEL['${esc(n)}']={...(RSEL['${esc(n)}']||{}),recipe:this.value}">${opts}</select>
   <input id="rs-f-${esc(n)}" placeholder="ref (catalog's)" value="${esc(sel.ref||'')}" oninput="RSEL['${esc(n)}']={...(RSEL['${esc(n)}']||{}),ref:this.value}" title="branch, tag or commit; empty = the catalog's ref">
   <button class=act onclick="rcDeploy('${esc(n)}',false)" title="build + download while the current one serves, then switch">deploy</button>
   <button class=act onclick="rcDeploy('${esc(n)}',true)" title="check out, build and download only — nothing stops">prepare</button>
  </div>
  <div class=ract>
   <button class=act onclick="rcOp('recipeStop','${esc(n)}')"${L.ok?'':' disabled'}>stop</button>
   <button class=act onclick="rcOp('recipeStart','${esc(n)}')"${L.ok||!c?' disabled':''}>start</button>
   <button class=act onclick="rcOp('recipeRollback','${esc(n)}')"${pv?'':' disabled'} title="${pv?'switch back to '+esc(rcLabel(pv)):'nothing to roll back to'}">roll back${pv?' → '+esc(rcLabel(pv)):''}</button>
  </div>
  ${hist?`<details><summary>history</summary><table>${hist}</table></details>`:''}
 </div>`}).join('');
 const C=d.catalog||{};
 const crow=Object.entries(C).map(([k,v])=>`<tr><td><b>${esc(k)}</b></td><td>${esc(v.kind)}</td><td>${esc(v.ref||'main')}</td><td>${esc((v.slots||[]).join(', '))}</td><td class=mut>${esc(v.repo)}</td><td><button class=lnk onclick="rcCatRemove('${esc(k)}')">remove</button></td></tr>`).join('');
 const slotBoxes=Object.keys(d.slots||{}).map(n=>`<label><input type=checkbox name=rcs value="${esc(n)}"> ${esc(n)}</label>`).join(' ');
 return `<div class=cfg>
 <div class=rslots>${cards}</div>
 <pre id=rc-out class="cfgpre${RCJOB?'':' hide'}${RCJOB&&RCJOB.done&&RCJOB.rc!==0?' bad':''}">${esc(RCJOB?RCJOB.text:'')}</pre>
 ${rcDiscover(d)}
 <div class="cfgsec rcat"><h4>Catalog <span class=mut>— recipe repos and the slots each may run on (site config › recipes)</span></h4>
  <table>${crow}</table>
  <div class=radd><input name=name id=rca-n placeholder="name" size=18><input name=repo id=rca-r placeholder="https://github.com/…/recipe.git">
   <select id=rca-k>${(d.kinds||[]).map(k=>`<option>${esc(k)}</option>`).join('')}</select><input id=rca-f placeholder="ref" value="main" size=10> ${slotBoxes}
   <button class=act onclick="rcCatAdd()">add</button></div>
  <div class=hint><b>spark</b>: a TP-pair recipe run with <code>python3 -m spark up|down</code>; the slot's link settings are written into its <code>cluster.toml</code>. <b>systemd</b>: a one-GPU recipe with <code>setup.sh</code> + <code>serve.sh</code>. Models download through the Hugging Face mirror on __NAS__.</div></div>
 </div>`}
let RDSORT='fastest',RDALL=false;
function rcDiscover(d){
 const D=d.discover||{},have=new Set(Object.values(d.catalog||{}).map(v=>(v.repo||'').toLowerCase().replace(/(\.git)?\/*$/,'')));
 const rank=x=>{const sp=(x.speed||{}).tps;return RDSORT=='newest'?[0,x.pushed_at||'']:RDSORT=='stars'?[x.stars||0,x.pushed_at||'']:[sp?2:x.claimed_tps?1:0,sp||x.claimed_tps||0,x.pushed_at||'']};
 const cmp=(a,b)=>{const A=rank(a),B=rank(b);for(let i=0;i<A.length;i++){if(A[i]<B[i])return 1;if(A[i]>B[i])return -1}return 0};
 const items=(D.items||[]).filter(x=>RDALL||(x.slots||[]).length).sort(cmp);
 const hwl={gb10:'GB10',rtx5090:'RTX 5090',mac:'Mac'};
 const rows=items.map(x=>{const sp=x.speed,inCat=x.in_catalog||have.has(('https://github.com/'+x.repo).toLowerCase());
  const speed=sp&&sp.tps?`<b>${esc(sp.tps)}</b> tok/s <span class="tag ok" title="${esc(sp.source)} · ${esc(sp.hw||'')}">measured</span>${sp.peak?` <span class=mut>peak ${esc(Math.round(sp.peak))} @${esc(sp.streams)}</span>`:''}`
   :x.claimed_tps?`${esc(x.claimed_tps)} tok/s <span class="tag warn" title="the first tok/s figure in its README: unverified, may be an aggregate">claim</span>`:'<span class=mut>—</span>';
  const fit=(x.slots||[]).length?`<span class=ok>→ ${esc(x.slots.join(', '))}</span> <span class=mut>${esc(x.kind)}</span>`:`<span class=mut>${esc(x.why||'')}</span>`;
  const add=inCat?'<span class=mut>in catalog</span>':(x.slots||[]).length?`<button class=lnk onclick="rcAddFound('${esc(x.repo)}')">add</button>`:'';
  return `<tr><td><a class=lnk href="${esc(x.url)}" target=_blank rel=noopener>${esc(x.repo)}</a>${x.fork?' <span class=mut>fork</span>':''}<div class=mut style="font:10.5px var(--sans);white-space:normal;max-width:520px">${esc(x.description||'')}</div></td>
   <td>${esc(hwl[x.hw]||x.hw)}${x.units>1?' ×'+esc(x.units):''}</td><td>${speed}</td><td class=mut>${esc((x.pushed_at||'').slice(0,10))}</td><td class=mut>★${esc(x.stars||0)}</td><td>${fit}</td><td>${add}</td></tr>`}).join('');
 return `<div class="cfgsec rcat"><h4>Discover on GitHub <span class=mut>— public recipes for this cluster's hardware; speed is measured (our harness, tensorfold.dev) where anyone measured it</span></h4>
  <div class=radd>sort <select onchange="RDSORT=this.value;repaint('recipes',PANELS.find(x=>x.id=='recipes')._data)">${['fastest','newest','stars'].map(s=>`<option${s==RDSORT?' selected':''}>${s}</option>`).join('')}</select>
   <label><input type=checkbox${RDALL?' checked':''} onchange="RDALL=this.checked;repaint('recipes',PANELS.find(x=>x.id=='recipes')._data)"> include ones Noctyx cannot deploy here</label>
   <span class=mut>${D.ts?'searched '+esc(agoS(D.ts))+' · '+esc((D.items||[]).length)+' found':'not searched yet'}</span>
   <button class=act onclick="rcRun('recipeDiscover',{},'search GitHub')">search now</button></div>
  ${rows?`<table>${rows}</table>`:'<div class=empty>nothing yet: search now</div>'}
  <div class=hint>“claim” is the first tok/s figure in a README: unverified, often an aggregate over many streams. Adding a recipe changes nothing that runs: deploy it from its slot above (it builds first, while the current one keeps serving).${(D.items||[]).some(x=>x.units==4)?' Recipes for 4 Sparks would need one slot across both pairs.':''}</div></div>`}
function rcAddFound(full){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},x=((d.discover||{}).items||[]).find(i=>i.repo==full);if(!x)return;
 let name=full.split('/')[1].toLowerCase().replace(/[^a-z0-9.-]+/g,'-').replace(/^[^a-z0-9]+/,'').slice(0,48);
 if((d.catalog||{})[name])name=(full.split('/')[0].toLowerCase().replace(/[^a-z0-9-]+/g,'')+'-'+name).slice(0,48);
 name=prompt(`Catalog name for ${full}?\n(${x.kind}, slots ${x.slots.join(', ')}, branch ${x.branch})`+(x.kind=='script'?'\n\nstart.sh recipe: it reads the slot\'s script_env (WORKER, PORT) from .env — check its README for other settings it needs.':''),name);
 if(!name)return;
 rcRun('recipeCatalog',{name:name.trim(),entry:{repo:x.clone||('https://github.com/'+full+'.git'),kind:x.kind,ref:x.branch||'main',slots:x.slots}},'catalog add '+name)}
function rcDeploy(n,prep){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},s=(d.slots||{})[n]||{},L=s.live||{};
 const recipe=($('rs-r-'+n)||{}).value,ref=(($('rs-f-'+n)||{}).value||'').trim();
 if(!recipe)return;
 if(!prep&&!confirm(`Deploy ${recipe}${ref?' @ '+ref:''} to ${n} (${s.head}${s.worker?' + '+s.worker:''})?\n\n`
   +(L.ok?`${(L.models||[]).join(', ')} keeps serving while the new one is checked out, built and downloaded, then STOPS for the switch.`:'Nothing is serving there now.')
   +`\nIf the new one does not come up, ${n} switches back to ${rcLabel(s.current)}.`))return;
 rcRun('recipeDeploy',{slot:n,recipe,...(ref?{ref}:{}),...(prep?{prepare_only:true}:{})},(prep?'prepare ':'deploy ')+recipe+' → '+n)}
function rcOp(action,n){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},s=(d.slots||{})[n]||{},L=s.live||{};
 const what={recipeStop:'stop',recipeStart:'start',recipeRollback:'roll back'}[action];
 const m={recipeStop:`Stop ${n}? ${(L.models||[]).join(', ')||'it'} goes offline until you start or deploy again.`,
  recipeStart:`Start ${rcLabel(s.current)} on ${n}?`,
  recipeRollback:`Roll ${n} back to ${rcLabel(s.previous)}?${L.ok?'\n'+(L.models||[]).join(', ')+' stops for the switch.':''}`}[action];
 if(confirm(m))rcRun(action,{slot:n},what+' '+n)}
function rcCatAdd(){
 const slots=[...document.querySelectorAll('input[name=rcs]:checked')].map(x=>x.value);
 const entry={repo:$('rca-r').value.trim(),kind:$('rca-k').value,ref:$('rca-f').value.trim()||'main',slots};
 rcRun('recipeCatalog',{name:$('rca-n').value.trim(),entry},'catalog add')}
function rcCatRemove(k){if(confirm(`Remove ${k} from the catalog? (Nothing is stopped; refused while it is deployed.)`))rcRun('recipeCatalog',{name:k,remove:true},'catalog remove')}
async function rcRun(action,params,what){
 RCJOB={text:what+': starting…',done:false};const show=()=>{const o=$('rc-out');if(o){o.classList.remove('hide');o.textContent=RCJOB.text;o.classList.toggle('bad',RCJOB.done&&RCJOB.rc!==0)}};show();
 const tok=localStorage.getItem(LS_TOK)||'';
 let r;try{r=await fetch('/api/v1/action',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({action,params})})}
 catch(e){Object.assign(RCJOB,{text:what+': '+e,done:true,rc:1});show();return}
 const j=await r.json().catch(()=>({error:'bad response'}));
 if(!r.ok||!j.id){Object.assign(RCJOB,{text:`${what} refused: ${j.error||r.status}`,done:true,rc:1});show();return}
 for(;;){await new Promise(z=>setTimeout(z,1500));
  let s;try{s=await fetchJSON('/api/v1/job/'+j.id)}catch(e){continue}
  Object.assign(RCJOB,{done:!!s.done,rc:s.rc,text:`${what}${s.done?(s.rc===0?' — done':' — FAILED (rc='+s.rc+')'):' — running…'}\n${(s.output||'').trim()}`});show();
  if(s.done){const p=PANELS.find(x=>x.id=='recipes');const data=await p.api().catch(()=>null);if(data){p._data=data;repaint('recipes',data)}return}}}
function rBrowse(b){
 if(!b||!b.host)return '<div class=empty>select a snapshot: open a machine → Backups tab → click a snapshot (or “browse” below)</div>';
 const snapOpts=(b.snaps||[]).map(s=>`<option value="${esc(s.short_id)}"${s.short_id==b.snap?' selected':''}>${esc(fmtTs(s.ts))} · ${esc(s.profile)} · ${esc(s.short_id)}</option>`).join('');
 const crumbs=['/'].concat(b.path.split('/').filter(Boolean)).map((seg,i,arr)=>{
  const to=i==0?'/':'/'+arr.slice(1,i+1).join('/');
  return `${i?'<span class=sep>/</span>':''}<button onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(to)}')">${esc(seg)}</button>`}).join('');
 const head=`<div class=brow><b>${esc(b.host)}</b>
  <select onchange="browseTo('${esc(b.host)}',this.value,'/')">${snapOpts}</select>
  <input id=bq placeholder="find files across all snapshots, e.g. *.conf or */notes.md" value="${esc((b.find&&b.find.q)||'')}" onkeydown="if(event.key=='Enter')browseFind()">
  <button class=act onclick="browseFind()">find</button></div>`;
 if(b.busy)return head+'<div class=empty>loading…</div>';
 if(b.find){const f=b.find;
  if(f.error)return head+`<div class="empty bad">${esc(f.error)}</div>`;
  const rows=(f.matches||[]).map(m=>{const dir=m.path.replace(/\/[^/]*$/,'')||'/';
   return `<tr><td class=mut>${esc(m.snapshot)}</td><td>${esc(m.path)}</td><td>${m.type=='file'?esc(fmtBytes(m.size)):esc(m.type||'')}</td><td class=mut>${esc(fmtTs(m.mtime))}</td>
    <td class=acts><button class=lnk onclick="browseTo('${esc(b.host)}','${esc(m.snapshot)}','${esc(dir)}')">open</button>
    ${m.type=='file'?`<a class=lnk href="/api/v1/download/${encodeURIComponent(b.host)}/${esc(m.snapshot)}?path=${encodeURIComponent(m.path)}" data-h="${esc(b.host)}" data-s="${esc(m.snapshot)}" data-p="${esc(m.path)}" onclick="return dlGo(event,this)">download</a>`:''}</td></tr>`}).join('');
  return head+`<div class=sline>${(f.matches||[]).length} match${(f.matches||[]).length==1?'':'es'} for <b>${esc(f.q)}</b>${f.truncated?' (first 200)':''} · <button class=lnk onclick="BR.find=null;repaint('browse',BR)">back to browsing</button></div>
   <table><tr><th>snap</th><th>path</th><th>size</th><th>modified</th><th></th></tr>${rows||'<tr><td class=mut colspan=5>nothing matched</td></tr>'}</table>`}
 const d=b.data;if(!d)return head+'<div class=empty>no data</div>';
 if(d.error)return head+`<div class="empty bad">${esc(d.error)}</div>`;
 const rows=(d.entries||[]).map(e=>{const full=(b.path.replace(/\/$/,'')||'')+'/'+e.name;
  const dl=`/api/v1/download/${encodeURIComponent(b.host)}/${esc(b.snap)}?path=${encodeURIComponent(full)}${e.type=='dir'?'&dir=1':''}`;
  return `<tr class="${e.type=='dir'?'dir':''}"><td ${e.type=='dir'?`onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(full)}')"`:''}>${e.type=='dir'?'▸ ':''}${esc(e.name)}${e.type=='symlink'?' <span class=mut>→ '+esc(e.target||'')+'</span>':''}</td>
   <td>${e.type=='file'?esc(fmtBytes(e.size)):'<span class=mut>'+esc(e.type)+'</span>'}</td><td class=mut>${esc(fmtTs(e.mtime))}</td><td class=mut>${esc(e.mode||'')}</td>
   <td class=acts>${e.type!='symlink'?`<a class=lnk href="${dl}" data-h="${esc(b.host)}" data-s="${esc(b.snap)}" data-p="${esc(full)}" data-d="${e.type=='dir'?1:''}" onclick="return dlGo(event,this)">${e.type=='dir'?'tar':'download'}</a>`:''}
    <button class=lnk onclick="browseRestore('${esc(full)}','staging')">→ staging</button>
    <button class=lnk onclick="browseRestore('${esc(full)}','host')">→ host</button></td></tr>`}).join('');
 const up=b.path!='/'?`<tr class=dir><td onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(b.path.replace(/\/[^/]*\/?$/,'')||'/')}')">▴ ..</td><td colspan=4></td></tr>`:'';
 return head+`<div class=crumb>${crumbs}</div>
  <table><tr><th>name</th><th>size</th><th>modified</th><th>mode</th><th></th></tr>${up}${rows||'<tr><td class=mut colspan=5>empty directory</td></tr>'}</table>
  <div class=mut style="font:10.5px var(--mono);margin-top:6px">download = the file (dirs as .tar) · → staging = restore onto __NAS__ under backups/restores/${esc(b.host)}/&lt;time&gt;/ · → host = restore into __NODE_LIB__/restore/ on the machine. Nothing is ever overwritten from here.</div>`}
/* ── hygiene: per-host housekeeping findings; manual-tier items get a fix button ── */
/* hygiene fix feedback. Every click has a visible life: sending → queued → running → ✓/✗.
   State lives here (key host\0id) and the row is re-rendered from it; when the job ends the
   report is re-fetched and the entry settles against it (item fixed → gone, else ✗ + reason). */
const HY={};let HYSCAN=null;const hyKey=(h,id)=>h+'\0'+id;
function hyRepaint(){const p=PANELS.find(x=>x.id=='hygiene');if(p&&p._data!==undefined)repaint('hygiene',p._data)}
function hyErr(t){try{t=JSON.parse(t).error||t}catch(e){}return /bad token/.test(t)?'rejected: set the UI token in Console first':'rejected: '+t}
async function hySend(host,ids){for(const id of ids)HY[hyKey(host,id)]={st:'sending'};hyRepaint();
 const r=await act('hygieneFix',{host,items:ids});
 for(const id of ids)HY[hyKey(host,id)]=r.error?{st:'failed',msg:hyErr(r.error)}:{st:'queued',job:r.id};hyRepaint()}
function hyFix(btn){const {h,id,what}=btn.dataset;if(confirm(`${what} on ${h}?`))hySend(h,[id])}
function hyFixAll(btn){const jobs=JSON.parse(btn.dataset.jobs);const n=jobs.reduce((a,[,ids])=>a+ids.length,0);
 const fb=Number(btn.dataset.bytes||0);
 if(confirm(`Apply all ${n} space-reclaim fixes (package removal, old kernels, journal vacuum, docker prune) on every host?${fb?`\n\nFrees about ${fmtBytes(fb)}.`:''}`))for(const [h,ids] of jobs)hySend(h,ids)}
async function hyScan(){if(HYSCAN&&!['failed'].includes(HYSCAN.st))return;HYSCAN={st:'sending'};hyRepaint();
 const r=await act('hygiene');HYSCAN=r.error?{st:'failed',msg:hyErr(r.error)}:{st:'queued',job:r.id};hyRepaint()}
function hySync(){let ch=false;const upd=e=>{if(!e||!e.job||e.st=='done'||e.st=='failed')return;const j=JOBS[e.job];if(!j)return;
  const st=j.done?'done':j.running?'running':'queued';if(e.st!=st){e.st=st;ch=true;if(j.done){e.rc=j.rc;e.msg=(j.output||'').trim().split('\n').filter(Boolean).pop()||''}}};
 for(const k in HY)upd(HY[k]);upd(HYSCAN);return ch}
async function hyRefresh(){const p=PANELS.find(x=>x.id=='hygiene');if(!p)return;await tick(p);
 const H=(p._data&&p._data.hosts)||{};
 for(const k in HY){const e=HY[k];if(e.st!='done')continue;const [h,id]=k.split('\0');const it=((H[h]||{}).items||[]).find(i=>i.id==id);
  if(!it||it.fixed)delete HY[k];else{e.st='failed';e.msg=(e.rc===0?'ran, but not fixed':'failed (rc='+e.rc+')')+(e.msg?' · '+e.msg.slice(0,90):'')}}
 if(HYSCAN&&HYSCAN.st=='done')HYSCAN=HYSCAN.rc===0?null:{st:'failed',msg:'scan failed (rc='+HYSCAN.rc+')'+(HYSCAN.msg?' · '+HYSCAN.msg.slice(0,90):'')};
 hyRepaint()}
function hyCell(n,i){const e=HY[hyKey(n,i.id)],what=i.fix||'fix',
 btn=(label)=>`<button class="${label=='retry'?'lnk':'act'}" data-h="${esc(n)}" data-id="${esc(i.id)}" data-what="${esc(what)}: ${esc(i.title)}" onclick="hyFix(this)">${esc(label)}</button>`;
 if(e)switch(e.st){
  case 'sending':return '<span class=qj>…</span> <span class=mut>sending</span>';
  case 'queued':return '<span class=qj>…</span> <span class=mut>queued behind another job</span>';
  case 'running':return `<span class=spin>◌</span> <span class=mut>${esc(what)}…</span>`;
  case 'done':return '<span class=spin>◌</span> <span class=mut>checking…</span>';
  default:return `<span class=badj>✗</span> <span class=warn style="font:10.5px var(--mono);white-space:normal">${esc(e.msg||'failed')}</span> ${btn('retry')}`}
 if(i.fixed)return '<span class=okj>✓ fixed</span>';
 if(i.id=='reboot-needed'&&rbEligible(n))return rbBtn(n);
 if(i.tier=='manual')return btn(what);
 if(i.tier=='auto')return '<span class=mut>auto-fixes at 06:45</span>';
 return '<span class=mut>report only</span>'}
function rHygiene(d){
 if(!d)return '<div class=empty>loading…</div>';
 const H=d.hosts||{},names=Object.keys(H);
 if(!names.length)return `<div class=empty>no report yet — <button class=lnk onclick="hyScan()">run hygiene now</button></div>`;
 let rows='',open=0;
 // what the open space fixes would free (measured by each host's agent the way the fix works; ~ = estimate)
 const freeOf=items=>items.filter(i=>!i.fixed&&i.bytes>0).reduce((a,i)=>a+i.bytes,0);
 const approxOf=items=>items.some(i=>!i.fixed&&i.bytes>0&&i.bytes_approx);
 const frees=i=>i.bytes>0&&!i.fixed?` <span class="tag ok" title="${i.bytes_approx?'an estimate: ':''}what this fix frees on disk">frees ${i.bytes_approx?'~':''}${esc(fmtBytes(i.bytes))}</span>`:'';
 let fleet=0,fleetApprox=false;
 for(const n of names){const items=H[n].items||[];open+=items.filter(i=>!i.fixed).length;
  const hf=freeOf(items),ha=approxOf(items);fleet+=hf;fleetApprox=fleetApprox||ha;
  const hcell=`${esc(n)}${hf?`<div class=mut style="font:10px var(--mono)">${ha?'~':''}${esc(fmtBytes(hf))} reclaimable</div>`:''}`;
  if(H[n].error){rows+=`<tr><td>${esc(n)}</td><td colspan=3 class=bad>${esc(H[n].error)}</td></tr>`;continue}
  if(!items.length){rows+=`<tr class="${hlRow('hygiene',n)}"><td>${esc(n)}</td><td colspan=3 class=mut>clean${FOCUS.panel=='hygiene'&&FOCUS.host==n?' — nothing to fix here; the finding may be older than the last scan':''}</td></tr>`;continue}
  rows+=items.map((i,k)=>`<tr class="${i.fixed?'mut':''}${hlRow('hygiene',n)}"><td>${k?'':hcell}</td><td><span class="tag ${i.severity=='warning'&&!i.fixed?'warn':''}">${esc(i.severity)}</span></td>
   <td><b>${esc(i.title)}</b>${frees(i)}${i.detail?`<div class=mut style="font:10.5px var(--mono);white-space:normal">${esc(i.detail)}</div>`:''}
    ${i.fixed?'':i.explain?`<div class=xpl style="font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:6px;white-space:normal;border-left:2px solid var(--line);padding-left:8px">${esc(i.explain)}<div class=mut style="font:10px var(--mono);margin-top:2px">— ${esc(i.explain_model||'assistant')}</div></div>`
     :`<button class=lnk data-h="${esc(n)}" data-id="${esc(i.id)}" onclick="explainItem(this.dataset.h,this.dataset.id,this)">explain</button>`}</td>
   <td class=acts>${hyCell(n,i)}</td></tr>`).join('')}
 // bulk = space reclaim only; unit restarts/clears (items with a named fix) are always one click each
 const bulk=names.map(n=>[n,(H[n].items||[]).filter(i=>!i.fixed&&i.tier=='manual'&&!i.fix&&!HY[hyKey(n,i.id)]).map(i=>i.id)]).filter(([,ids])=>ids.length);
 const nb=bulk.reduce((a,[,ids])=>a+ids.length,0);
 const nbBytes=bulk.reduce((a,[n,ids])=>a+(H[n].items||[]).filter(i=>ids.includes(i.id)&&i.bytes>0).reduce((s,i)=>s+i.bytes,0),0);
 const scan=!HYSCAN?`<button class=lnk onclick="hyScan()">re-scan</button>`
  :HYSCAN.st=='failed'?`<span class=badj>✗</span> <span class=warn>${esc(HYSCAN.msg)}</span> <button class=lnk onclick="hyScan()">retry</button>`
  :HYSCAN.st=='running'?`<span class=spin>◌</span> scanning every host…`:HYSCAN.st=='done'?`<span class=spin>◌</span> reading report…`:`<span class=qj>…</span> scan ${HYSCAN.st=='sending'?'sending':'queued'}`;
 return `<div class=sline><b>${open}</b> open item${open==1?'':'s'}${fleet?` · <b class=ok>${fleetApprox?'~':''}${esc(fmtBytes(fleet))}</b> can be freed`:''} · report ${esc(agoS(d.ts))} · ${scan}${nb?` · <button class=lnk data-jobs='${esc(JSON.stringify(bulk))}' data-bytes="${nbBytes}" onclick="hyFixAll(this)">fix all ${nb} space items${nbBytes?` (frees ${fleetApprox?'~':''}${esc(fmtBytes(nbBytes))})`:''}</button>`:''}</div>
  <table><tr><th>host</th><th></th><th>finding</th><th></th></tr>${rows}</table>`}
function rBrief(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(!d.text)return `<div class="empty warn">no brief: ${esc(d.error||'assistant unavailable')} · <button class=lnk onclick="refreshBrief()">retry</button></div>`;
 return `<div style="font:12.5px/1.55 var(--sans);white-space:pre-wrap">${esc(d.text)}</div>
  <div class=mut style="font:10.5px var(--mono);margin-top:8px">written ${esc(agoS(d.ts))} by ${esc(d.model||'the assistant')} from live data · <button class=lnk onclick="refreshBrief()">refresh</button>${d.stale_error?' · <span class=warn>refresh failed: '+esc(d.stale_error)+'</span>':''}</div>`}
async function refreshBrief(){const p=PANELS.find(x=>x.id=='brief');const b=$('b-brief');if(b)b.classList.add('stale');
 try{p._data=await fetchJSON('/api/v1/digest?refresh=1',true);repaint('brief',p._data)}catch(e){if(b)b.classList.remove('stale')}}
async function explainItem(host,id,btn){btn.disabled=true;btn.textContent='asking…';
 const tok=localStorage.getItem(LS_TOK)||'';
 try{const r=await fetch('/api/v1/explain',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({kind:'hygiene',host,id})});
  const d=await r.json();const box=btn.closest('td').querySelector('.xpl')||btn.closest('td').appendChild(Object.assign(document.createElement('div'),{className:'xpl'}));
  box.style.cssText='font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:6px;white-space:normal;border-left:2px solid var(--line);padding-left:8px';
  box.textContent=d.text||('assistant: '+(d.error||'no answer'));
  if(d.model){const m=document.createElement('div');m.className='mut';m.style.cssText='font:10px var(--mono);margin-top:2px';m.textContent='— '+d.model;box.appendChild(m)}
  btn.remove()}catch(e){btn.disabled=false;btn.textContent='explain'}}
/* dismissing one alert: an Alertmanager silence is the mechanism. The server rebuilds the
   matchers from the alert's own stable labels, so two alerts that share an alertname and host
   (agent carries two ComponentUpgradeAvailable, node_exporter and prometheus) stay
   independently dismissible instead of both vanishing on one click. */
async function silenceAlert(fp,mins,btn){
 const was=btn.textContent;btn.disabled=true;btn.textContent='…';
 try{
  const r=await fetch('/api/v1/alert-silence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:fp,minutes:mins})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||d.error)throw new Error(d.error||('http '+r.status));
  const p=PANELS.find(x=>x.id=='alerts');if(p){const data=await p.api();p._data=data;repaint('alerts',data)}
 }catch(e){btn.disabled=false;btn.textContent=was;btn.title='silence failed: '+e.message;btn.classList.add('bad')}
}
async function unsilenceAlert(id,btn){
 const was=btn.textContent;btn.disabled=true;btn.textContent='…';
 try{
  const r=await fetch('/api/v1/alert-unsilence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||d.error)throw new Error(d.error||('http '+r.status));
  const p=PANELS.find(x=>x.id=='alerts');if(p){const data=await p.api();p._data=data;repaint('alerts',data)}
 }catch(e){btn.disabled=false;btn.textContent=was;btn.title='unsilence failed: '+e.message;btn.classList.add('bad')}
}
/* a component's buttons (alerts and the Machine Report): Noctyx's own upgrade where it has one, or skip the release */
function compBtns(host,f){if(!f)return '';const x=f.fix||{},h=esc(JSON.stringify(host)),c=esc(JSON.stringify(f.component));let b='';
 if(x.argv)b+=`<button class=silbtn onclick="event.stopPropagation();compUpgrade(${h},${c},${esc(JSON.stringify(x.what||''))},this)" title="${esc(x.what||'')}">upgrade</button>`;
 if(x.change&&f.hosts>1)b+=`<button class=silbtn onclick="event.stopPropagation();compEverywhere(${c},${f.hosts},this)" title="every machine that needs it: one first, watched 5 min, then the rest one at a time; stops at the first failure">upgrade all ${f.hosts}</button>`;
 else if(x.why)b+=`<span class=mut style="font:10px var(--mono)" title="${esc(x.why)}">no upgrade here ⓘ</span>`;
 if(f.state=='upgrade'&&f.latest)b+=`<button class=silbtn onclick="event.stopPropagation();compSkip(${h},${c},${esc(JSON.stringify(f.latest))},false,this)" title="stay on ${esc(f.installed||'this version')}: no alert for ${esc(f.latest)}; a later release alerts again">skip ${esc(f.latest)}</button>`;
 if(f.state=='skipped')b+=`<button class=silbtn onclick="event.stopPropagation();compSkip(${h},${c},'',true,this)" title="alert on this release again">unskip</button>`;
 return b}
async function compUpgrade(host,component,what,btn){
 if(!confirm(`${what}?\n\nRuns as a job (Jobs / Audit shows it), then checks ${host}'s versions again: the alert clears when it is current.`))return;
 if(btn){btn.disabled=true;btn.textContent='upgrading…'}
 const r=await act('upgradeComponent',{host,component});if(r&&r.error&&btn){btn.disabled=false;btn.textContent='upgrade';alert(r.error)}}
async function compEverywhere(component,n,btn){
 if(!confirm(`Upgrade ${component} on all ${n} machines that need it?\n\nOne goes first and is watched for 5 minutes, then the rest one at a time. Each is checked and rolled back if it does not work, and the rollout stops at the first failure.`))return;
 if(btn){btn.disabled=true;btn.textContent='upgrading…'}
 const r=await act('upgradeEverywhere',{component});if(r&&r.error&&btn){btn.disabled=false;btn.textContent='upgrade all';alert(r.error)}}
async function healNow(btn){if(btn){btn.disabled=true;btn.textContent='repairing…'}
 const r=await act('healNow');if(r&&r.error&&btn){btn.disabled=false;btn.textContent='repair now';alert(r.error)}}
async function compSkip(host,component,release,undo,btn){
 if(!undo&&!confirm(`Skip ${component} ${release} on ${host}?\n\nThe alert resolves and stays quiet until a newer release than ${release} appears.`))return;
 if(btn){btn.disabled=true;btn.textContent=undo?'…':'skipping…'}
 const r=await act('skipRelease',undo?{host,component,undo:true}:{host,component,release});
 if(r&&r.error&&btn){btn.disabled=false;btn.textContent=undo?'unskip':'skip '+release;alert(r.error)}}
function rAlerts(d){
 if(!d)return '<div class=empty>loading…</div>';
 const act=d.active||[],rec=d.recent||[];
 const pill=$('halert');if(pill){const c=act.filter(a=>a.severity=='critical').length;
  pill.textContent=act.length?`${act.length} alert${act.length==1?'':'s'}`:'no alerts';
  pill.className='hpill go '+(c?'bad':act.length?'warn':'ok')}
 const rows=act.map(a=>{const t=alertTarget(a);const fp=a.fingerprint||'';const comp=(a.labels||{}).component||'';
  const sil=fp?`<button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',60,this)" title="silence this one alert for 1 hour">1h</button><button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',1440,this)" title="silence this one alert for 24 hours">24h</button><button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',10080,this)" title="silence this one alert for 7 days">7d</button>`:'';
  return `<tr ${goAttr({host:t.host,panel:t.panel,tab:t.tab})} title="open ${esc(t.what||'')}"><td><span class="tag ${a.severity=='critical'?'bad':'warn'}">${esc(a.severity||'')}</span></td>
  <td>${esc(a.name||'')}${comp?`<div class=mut style="font:10px var(--mono)">${esc(comp)}</div>`:''}</td><td>${esc(a.host||'')}</td><td>${esc(a.summary||'')}${a.fix?`<div class=compbtns>${compBtns(a.host,a.fix)}</div>`:''}${a.heal?`<div class=compbtns><button class=silbtn onclick="event.stopPropagation();healNow(this)" title="run noctyx heal now: it tries the safe repairs again">repair now</button></div>`:''}${a.selfrepair?`<div class=srnote>self-repair: <span class="${SRST[a.selfrepair.state]||''}">${esc(SRWORD[a.selfrepair.state]||a.selfrepair.state||'')}</span> — ${esc(a.selfrepair.why||a.selfrepair.how||'')} <a class=lnk href="/heal" onclick="event.stopPropagation()">log →</a></div>`:''}${a.triage?`<div style="font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:4px;white-space:normal;border-left:2px solid var(--line);padding-left:8px">${esc(a.triage)}<span class=mut style="font:10px var(--mono)"> — ${esc(a.triage_model||'assistant')}</span></div>`:''}</td><td class=mut>${esc(agoS(a.since))}</td><td class=golnk>${t.what?esc(t.what)+' →':''}</td><td class=nowrap>${sil}</td></tr>`}).join('');
 const sl=(d.silenced||[]).map(s=>{const m=(s.matchers||[]).map(x=>esc(x.name)+'='+esc(x.value)).join(' · ');
  const until=s.ends?fmtTs(new Date(s.ends).getTime()/1000):'—';
  return `<tr><td class=mut style="font:10.5px var(--mono)">${m}</td><td class=mut>${esc(until)}</td><td class=mut>${esc(s.by||'')}</td>
   <td class=nowrap><button class=silbtn onclick="unsilenceAlert('${esc(s.id)}',this)" title="end this silence now: the alert fires again if it is still true">unsilence</button></td></tr>`}).join('');
 const hist=rec.slice(0,8).map(r=>`<tr><td class="${r.status=='resolved'?'ok':'warn'}">${esc(r.status||'')}</td>
  <td>${esc(r.name||'')}</td><td>${esc(r.host||'')}</td><td class=mut>${esc(fmtTs(r.ts))}</td></tr>`).join('');
 return (d.error?`<div class="empty warn">${esc(d.error)}</div>`:'')+
  (rows?`<table><tr><th>sev</th><th>alert</th><th>host</th><th>what</th><th>since</th><th>act in</th><th>silence</th></tr>${rows}</table>`
       :'<div class=sline><span class="tag ok">quiet</span> nothing firing</div>')+
  (sl?`<h3>Silenced</h3><table><tr><th>matches</th><th>until</th><th>by</th><th></th></tr>${sl}</table>`:'')+
  (hist?`<h3>Recent</h3><table><tr><th>status</th><th>alert</th><th>host</th><th>when</th></tr>${hist}</table>`:'')}
function rRuns(d){
 const rs=(d&&d.runs)||[];
 if(!rs.length)return '<div class=empty>no detached runs yet</div>';
 const cls={running:'warn',ok:'ok',failed:'bad',died:'bad'};
 return `<table><tr><th>started</th><th>job</th><th>state</th><th>took</th><th>last line</th></tr>`+rs.map(r=>
  `<tr class="${r.host?'go':''}${hlRow('runs',r.host)}" title="${esc(r.unit)}${r.host?' — open the machine':''}"${r.host?` onclick="goTo(${esc(JSON.stringify({host:r.host,panel:'detail',tab:'backups'}))})"`:''}><td>${esc(fmtTs(r.started_ts))}</td><td>${esc(r.cmd)}${r.host?' '+esc(r.host):''}</td>
   <td class="${cls[r.state]||''}">${esc(r.state)}${r.rc!=null&&r.rc!==0?' rc='+r.rc:''}</td>
   <td>${r.started_ts?esc(fmtDur((r.ended_ts||Date.now()/1000)-r.started_ts)):''}</td>
   <td class=mut style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${esc(r.last)}</td></tr>`).join('')+'</table>'+
  (rs.some(r=>r.state=='died')?`<div class=mut style="font:10.5px var(--mono);margin-top:6px">died = the unit ended without an exit record (killed or crashed); its log is in __JOB_LOGS__/</div>`:'')}
function rStorage(tiers){
 const g=(tiers&&tiers._growth)||null;
 const gl=!g?'':g.rate_per_day==null?`<div class=mut style="font:10.5px var(--mono);margin-top:6px">growth: ${g.reset?'reset by a clean-up '+esc(ago(g.reset))+'; ':''}collecting (${g.hours||0} h of history; needs 2 h)</div>`
  :`<div class=sline style="margin-top:6px">__NAS__ ${g.rate_per_day>=0?'growing':'shrinking'} <b>${esc(fmtBytes(Math.abs(g.rate_per_day)))}/day</b> (over ${g.hours>48?Math.round(g.hours/24)+' days':g.hours+' h'}${g.reset?' since a clean-up':''}${g.settling?', still settling':''})${g.days_to_full!=null?` · full in <b class="${g.days_to_full<30?'bad':g.days_to_full<180?'warn':''}">${g.days_to_full>3650?'>10 years':Math.round(g.days_to_full)+' days'}</b> at this rate`:''}</div>`;
 return `<table><tr><th>tier</th><th>free</th><th>used</th><th style=width:30%></th></tr>`+
 (tiers||[]).map(t=>{const tot=t.free_gb+t.used_gb||1;
  const rp=t.repos_gb,cm=rp?Object.values(rp).reduce((a,b)=>a+b,0):null;
  return `<tr><td>${t.mounted?esc(t.tier):'<span class=bad>'+esc(t.tier)+' ↓</span>'}</td><td>${t.free_gb}G</td><td>${t.used_gb}G</td><td>${bar(100*t.used_gb/tot)}</td></tr>`+
   (rp?`<tr><td class=mut>&nbsp; backups</td><td></td><td>${cm.toFixed(0)}G</td><td class=mut style="font:10.5px var(--mono)">${Object.entries(rp).map(([k,v])=>esc(k)+' '+v.toFixed(0)+'G').join(' · ')}</td></tr>`+
    `<tr><td class=mut>&nbsp; other data</td><td></td><td>${Math.max(0,t.used_gb-cm).toFixed(0)}G</td><td class=mut style="font:10.5px var(--mono)">not Noctyx${t.repos_ts?' · sized '+esc(ago(t.repos_ts)):''}</td></tr>`:'')}).join('')+'</table>'+gl}
function rJobs(rows){
 return `<table><tr><th>when</th><th>host</th><th>event</th><th>rc</th></tr>`+
 (rows||[]).map(a=>`<tr><td>${esc(fmtTs(a.ts))}</td><td>${esc(a.host||'')}</td><td>${esc(a.event||'')}</td><td class="${(a.rc??0)==0?'ok':'bad'}">${a.rc??''}</td></tr>`).join('')+'</table>'}
function rNet(s){
 const mb=(s&&s.mbit)||{},at=(s&&s.at)||{},ks=Object.keys(mb);
 return ks.length?`<table><tr><th>host</th><th>Mbit/s</th><th>measured</th></tr>`+ks.map(k=>
  `<tr><td>${esc(k)}</td><td class="${mb[k]>200?'ok':'warn'}">${mb[k].toFixed(0)}</td><td class=mut>${at[k]?ago(at[k]):'—'}</td></tr>`).join('')+
  `</table><div class=mut style="font:11px var(--mono);margin-top:8px">measured before each backup · “probe all” in Console refreshes every host</div>`
  :'<div class=empty>never probed — press “probe all” in Console</div>'}
function rActions(){
 return `<input class=pw type=password id=token placeholder="UI token (kept in this browser)" value="${esc(localStorage.getItem(LS_TOK)||'')}">
 <div style=height:8px></div>
 <button class=act onclick="act('status')">status</button>
 <button class=act onclick="act('probe')">probe all</button>
 <button class=act onclick="act('snapshots')">snapshots</button>
 <button class=act onclick="act('schedule')">schedule</button>
 <button class=act onclick="act('capacity')">capacity</button>
 <button class=act onclick="act('backup')">backup ALL</button>
 <button class=act onclick="act('verify')">verify ALL</button>
 <button class=act onclick="act('copy')">copy → NAS</button>
 <button class=act onclick="act('prune')">prune (dry)</button>
 <button class=act onclick="act('nasRest')">repo endpoint restart</button>
 <button class=act onclick="act('directory')">regen directory</button>
 <pre id=cout>idle</pre>`}
function wireActions(){const t=$('token');
 if(t&&!t.__wired){t.__wired=1;t.addEventListener('input',()=>localStorage.setItem(LS_TOK,t.value))}}

/* ── reboot: plan → confirm → job. The orchestrator re-checks every blocker; a head-node reboot
   takes this server down, so the page shows a banner, waits for it to answer again, re-adopts jobs. ── */
let RB=null;
function rbEligible(n){const h=((OVR&&OVR.hosts)||[]).find(x=>x.name==n);return !!h&&!h.monitored_only}
function rbBtn(n,cls){return `<button class="${cls||'act'}" data-h="${esc(n)}" onclick="rebootHost(this.dataset.h,this)">reboot…</button>`}
async function rebootHost(n,btn){
 if(btn){btn.disabled=true;btn.textContent='checking…'}
 let p;try{p=await fetchJSON('/api/v1/reboot-plan/'+encodeURIComponent(n))}catch(e){p={allowed:false,blockers:['plan failed: '+e]}}
 if(btn){btn.disabled=false;btn.textContent='reboot…'}
 if(!p.allowed){alert(`Can't reboot ${n} now:\n\n`+(p.blockers||[]).map(b=>' • '+b).join('\n'));return}
 const imp=(p.impact||[]).length?'\n\nGoes down with it:\n'+p.impact.map(x=>' • '+x).join('\n'):'\n\nNothing Noctyx knows of is serving from it.';
 const w=(p.warnings||[]).length?'\n\nNote:\n'+p.warnings.map(x=>' • '+x).join('\n'):'';
 const how=p.head?'This is the head node: it reboots in 20 s and this page reconnects when it is back.'
  :'It reboots in 10 s; Noctyx waits for it, checks its models and rescans Hygiene (progress in Runs).';
 if(!confirm(`Reboot ${n}?${imp}${w}\n\n${how}`))return;
 const r=await act('reboot',{host:n});
 if(r&&r.error){alert('reboot rejected: '+r.error);return}
 if(r&&r.id&&p.head){RB={host:n,job:r.id,t0:Date.now(),st:'scheduled'};rbBanner();rbWatch()}}
function rbBanner(){let b=$('rbban');if(!RB){if(b)b.remove();return}
 if(!b){b=document.createElement('div');b.id='rbban';document.body.appendChild(b)}
 const s=Math.round((Date.now()-RB.t0)/1000),x=` <button class=lnk onclick="RB=null;rbBanner()">dismiss</button>`;
 b.innerHTML={scheduled:`<span class=spin>◌</span> ${esc(RB.host)} reboots in a few seconds…`,
  down:`<span class=spin>◌</span> ${esc(RB.host)} is rebooting — this page reconnects when it is back (${s}s)`,
  back:`<span class=okj>✓</span> ${esc(RB.host)} is back after ${s}s — the reboot job is finishing (models, Hygiene) in Runs`,
  failed:`<span class=badj>✗</span> ${esc(RB.msg||'reboot did not start')}${x}`,
  nodown:`<span class=warn>${esc(RB.host)} has not gone down after ${s}s — check Runs</span>${x}`}[RB.st]}
async function rbWatch(){
 while(RB&&['scheduled','down'].includes(RB.st)){
  await new Promise(r=>setTimeout(r,3000));
  let up=false,j=null;
  try{const c=new AbortController(),t=setTimeout(()=>c.abort(),2500);
   const r=await fetch('/api/v1/job/'+RB.job,{signal:c.signal});clearTimeout(t);
   up=r.ok||r.status==404;if(r.ok)j=await r.json()}catch(e){up=false}
  if(!RB)return;
  if(RB.st=='scheduled'){
   if(j&&j.done&&j.rc!==0){RB.st='failed';RB.msg=`reboot of ${RB.host} refused or failed (rc=${j.rc}): `+((j.output||'').trim().split('\n').pop()||'')}
   else if(!up)RB.st='down';
   else if(Date.now()-RB.t0>120000)RB.st='nodown'}
  else if(up){RB.st='back';rbBanner();adoptRunning();for(const p of PANELS)tick(p);
   setTimeout(()=>{if(RB&&RB.st=='back'){RB=null;rbBanner()}},20000);return}
  rbBanner()}}

/* ── actions + job polling (own loop, own DOM) ── */
async function act(a,params){
 let p=params||{};
 if((a=='backupHost'||a=='verifyHost')&&!p.host){p={host:SEL};if(!p.host){alert('select a machine first');return}}
 if(['backup','verify'].includes(a)&&!confirm(a+' ALL — run now?'))return;
 const tok=localStorage.getItem(LS_TOK)||'';
 const out=$('cout');if(out)out.textContent='triggering…';
 try{
  const r=await fetch('/api/v1/action',{method:'POST',
   headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
   body:JSON.stringify({action:a,params:p})});
  if(!r.ok){const t=await r.text();if(out)out.textContent='rejected: '+t;return {error:t}}
  const {id}=await r.json();
  JOBS[id]={id,action:a,params:p,argv:[],output:'',done:false,running:false,started_ts:Date.now()/1000};
  refreshJobDom();
  if(SEL)tick(PANELS.find(x=>x.id=='detail'));
  return {id}
 }catch(e){if(out)out.textContent='error: '+e;return {error:String(e)}}
}

/* ── boot ── */
function tickClock(){$('hclock').textContent=new Date().toISOString().slice(0,16).replace('T',' ')+'Z'}
const H0=parseHash(location.hash);
if(VIEW=='main'&&H0.host){SEL=H0.host;localStorage.setItem(LS_SEL,SEL)}
if(VIEW=='main'&&H0.tab&&DTABS.includes(H0.tab)){DTAB=H0.tab;try{localStorage.setItem(LS_TAB,DTAB)}catch(e){}}
buildPanels();initTip();
if(VIEW=='main')repaint('actions',null);
for(const p of PANELS)if(!p.api&&p.id!='actions')repaint(p.id,p._data);   /* on-demand panels (Snapshot Browser) show their empty state, not “loading…” */
tickClock();setInterval(tickClock,1000);
for(const p of PANELS){tick(p);if(p.int>0)setInterval(()=>tick(p),p.int*1000)}
kioskArm();                                        /* /kiosk rotation pager, once the panels exist */
async function kioskArmAfterSite(){                /* site defaults arrive async: re-arm once they do */
 if(VIEW=='kiosk'){await loadSiteSettings();kioskArm()}}
kioskArmAfterSite();
setInterval(pollAll,2000);
adoptRunning();
if(VIEW=='main'&&H0.panel)setTimeout(()=>goTo(H0),250);      /* a link from /cluster or an alert: land on the panel */
addEventListener('hashchange',()=>goTo(parseHash(location.hash)));
addEventListener('resize',()=>saveLayout());
/* the header drops whole items until it fits (see header.top.f1…f5); re-fit when its text or the window changes */
const FIT_STEPS=5;
function fitHeader(){const h=document.querySelector('header.top');if(!h)return;
 for(let i=1;i<=FIT_STEPS;i++)h.classList.remove('f'+i);
 for(let i=1;i<=FIT_STEPS&&h.scrollWidth>h.clientWidth+1;i++)h.classList.add('f'+i)}
addEventListener('resize',fitHeader);
new MutationObserver(fitHeader).observe(document.querySelector('header.top'),{childList:true,characterData:true,subtree:true});
fitHeader();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        """The request's JSON body ({} when there is none)."""
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) if n > 0 else b"{}")

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _who(self):
        """(name, role) of a signed-in user or an API token's user, or None. Never cached on self: one
        handler object serves every request of a keep-alive connection, and a cached "nobody" outlived
        the sign-in."""
        a = self.headers.get("Authorization") or ""
        if a.startswith("Bearer nx_"):
            return AC.check_token(a[7:])
        return AC.check_session(self._cookie("nx_session"))

    def _bearer_ok(self):
        tok = token()
        return bool(tok) and self.headers.get("Authorization") == f"Bearer {tok}"

    def _allowed(self, need):
        mode = console_mode()
        if mode == "open":
            return True
        if mode == "token":
            return need == "view" or self._bearer_ok()
        w = self._who()                            # a session, or a user's API token: never the site token,
                                                   # which would act as admin whatever its holder's role
        return bool(w) and AC.rank(w[1]) >= NEED_RANK[need]

    def _deny(self, need):
        w = self._who()
        if console_mode() == "accounts" and not w:
            return self._json({"error": "sign in first", "login": True}, 401)
        if console_mode() == "token":
            return self._json({"error": "bad token"}, 401)
        return self._json({"error": f"{w[1] if w else 'this session'} may not do this (needs {'admin' if need == 'admin' else 'operator'})"}, 403)

    def _ask_who(self):
        """(owner, role) for Ask Noctyx, or None: the signed-in person and their role (view, operate, admin);
        open access and the site token act as admin, as they do for actions."""
        mode, w = console_mode(), self._who()
        if mode == "accounts":
            return (w[0], ("view", "operate", "admin")[AC.rank(w[1])]) if w and AC.rank(w[1]) >= 0 else None
        if mode == "token":
            return None, "admin" if self._bearer_ok() else "view"
        return (w[0] if w else None), "admin"

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return not o or urllib.parse.urlparse(o).netloc == (self.headers.get("Host") or "")

    def _redirect(self, where, cookie=None):
        self.send_response(302)
        self.send_header("Location", where)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, body: bytes, ctype="application/json", code=200, cookie=None):
        self.send_response(code)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        self.send_header("X-CM-Build", UI_BUILD)   # the page reloads itself once when this changes
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_dump(self, host, snap, path, is_dir):
        """restic dump straight to the client: a file's bytes, or a tar of a
        directory. No Content-Length is known up front, so Connection: close."""
        repo, rpath = _host_repo(host)
        name = os.path.basename(path.rstrip("/")) or "root"
        if is_dir:
            name += ".tar"
        p = subprocess.Popen(["restic", "-r", rpath, "--password-file", f"{P['repo_keys']}/{repo}.key",
                              "--no-lock", "--cache-dir", str(RESTIC_CACHE), "--quiet", "dump", snap, path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = p.stdout.read(65536)
        if not first and p.wait() != 0:
            return self._json({"error": (p.stderr.read().decode("utf-8", "replace") or "dump failed")[-200:]}, 502)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar" if is_dir else "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(first)
            for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            p.kill()
        finally:
            p.wait()
            audit_line = {"ts": _utcnow(), "event": "download", "host": host, "snapshot": snap, "path": path, "rc": p.returncode}
            try:
                with AUDIT.open("a") as f:
                    f.write(json.dumps(audit_line) + "\n")
            except Exception:
                pass
        self.close_connection = True

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), code=code)

    def do_GET(self):
        _reload_cfg()
        p = self.path.split("?")[0]
        q = dict(kv.split("=", 1) for kv in self.path.split("?")[1].split("&") if "=" in kv) \
            if "?" in self.path else {}
        if p in ("/login", "/login/"):
            return self._send(login_page(), "text/html; charset=utf-8")
        if DEMO and p.startswith("/api/"):
            body = demo_answer(self.path)
            return self._send(body, code=200) if body is not None else self._json({"error": "not in the demo"}, 404)
        if p == "/api/v1/me":
            w = self._who()
            return self._json({"mode": console_mode(), "user": w[0] if w else None, "role": w[1] if w else None,
                               "accounts": bool(AC.users())})
        # accounts mode: everything below needs a session (Prometheus' /metrics and the assets excepted)
        if not (p.startswith("/assets/") or p in ("/favicon.ico", "/favicon.svg", "/metrics")) and not self._allowed("view"):
            if p.startswith("/api/"):
                return self._deny("view")
            return self._redirect("/login?next=" + urllib.parse.quote(self.path, safe=""))
        if p == "/api/v1/settings/mine":
            w = self._who()
            return self._json({"settings": AC.get_settings(w[0]) if w else {}, "user": w[0] if w else None})
        if p in ("/", "/index.html"):
            return self._send(page(), "text/html; charset=utf-8")
        if p in ("/cluster", "/cluster/", "/config", "/config/", "/recipes", "/recipes/", "/about", "/about/",
                 "/kiosk", "/kiosk/", "/heal", "/heal/"):
            # systems-only / config views: same page, different panel set (see VIEW in the JS)
            return self._send(page(p.strip("/")), "text/html; charset=utf-8")
        if p in ("/favicon.ico", "/favicon.svg"):
            return self._send(open(NX_HOME / "assets" / "favicon.svg", "rb").read(),
                              "image/svg+xml")
        if p in ("/api/v1/overview", "/api/v1/state"):
            try:
                return self._json(overview())
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p.startswith("/api/v1/facts/"):
            name = p.rsplit("/", 1)[-1]
            try:
                age = int(q.get("age", 120) or 120)
            except ValueError:
                age = 120
            return self._json(host_facts(name, max_age=max(age, 0)))
        if p == "/api/v1/capacity":
            return self._json(capacity())
        if p == "/api/v1/audit":
            try:
                n = int(q.get("n", 15) or 15)
            except ValueError:
                n = 15
            return self._json(audit_tail(min(max(n, 1), 200)))
        if p.startswith("/assets/") and p[8:] in ASSETS:
            f = NX_HOME / "assets" / p[8:]
            return self._send(f.read_bytes(), ASSETS[p[8:]])
        if p == "/api/v1/about":
            return self._json(about_info())
        if p == "/api/v1/console-settings":
            return self._json({"settings": (CM.cfg().get("console") or {}).get("settings") or {}})
        if p == "/api/v1/recipes":
            try:
                return self._json(CM.recipe_summary(CM.cfg()))
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p == "/api/v1/policy":
            try:
                return self._json(policy_summary())
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p == "/api/v1/schedule":
            try:
                return self._json(schedule_summary())
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p == "/api/v1/metrics":
            try:
                return self._json(fleet_metrics())
            except Exception as e:
                return self._json({"error": str(e)[:200], "hosts": {}}, 502)
        if p.startswith("/api/v1/metrics/"):
            name = p.rsplit("/", 1)[-1]
            if name not in VALID_HOSTS:
                return self._json({"error": "unknown host"}, 404)
            try:
                return self._json(host_metrics(name, q.get("tab", "overview")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"prometheus: {str(e)[:200]}"}, 502)
        if p == "/api/v1/growth":
            return self._json({"ts": time.time(), "growth": growth_forecast()})
        if p.startswith("/api/v1/snapshots/"):
            name = p.rsplit("/", 1)[-1]
            if name not in VALID_HOSTS:
                return self._json({"error": "unknown host"}, 404)
            try:
                return self._json({"host": name, "snapshots": host_snapshots(name)})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 502)
        if p.startswith("/api/v1/browse/"):
            parts = p.split("/")            # /api/v1/browse/<host>/<snap>
            if len(parts) != 6 or parts[4] not in VALID_HOSTS or not _SNAP_RE.match(parts[5]):
                return self._json({"error": "bad request"}, 400)
            path = urllib.parse.unquote(q.get("path", "/") or "/")
            if not _ok_path(path):
                return self._json({"error": "bad path"}, 400)
            try:
                return self._json(browse(parts[4], parts[5], path))
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 502)
        if p.startswith("/api/v1/find/"):
            name = p.rsplit("/", 1)[-1]
            pat = urllib.parse.unquote(q.get("q", "") or "")
            snap = q.get("snapshot") or None
            if name not in VALID_HOSTS or not pat or len(pat) > 300 or (snap and not _SNAP_RE.match(snap)):
                return self._json({"error": "bad request"}, 400)
            try:
                return self._json(find_in_snapshots(name, pat, snap))
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 502)
        if p.startswith("/api/v1/download/"):
            parts = p.split("/")
            if len(parts) != 6 or parts[4] not in VALID_HOSTS or not _SNAP_RE.match(parts[5]):
                return self._json({"error": "bad request"}, 400)
            path = urllib.parse.unquote(q.get("path", "") or "")
            if not _ok_path(path) or path == "/":
                return self._json({"error": "bad path"}, 400)
            # a file from a snapshot can be anything (keys, a database): operator and up. A browser
            # link cannot send the bearer token, so token mode gets a 60 s signed ticket first.
            if not (self._allowed("operate")
                    or AC.check_signed(f"dl|{parts[4]}|{parts[5]}|{path}|{q.get('dir') == '1'}", q.get("t"))):
                return self._deny("operate")
            return self._stream_dump(parts[4], parts[5], path, q.get("dir") == "1")
        if p == "/metrics":
            try:
                return self._send(metrics_text().encode(), "text/plain; version=0.0.4; charset=utf-8")
            except Exception as e:
                return self._send(f"# metrics failed: {e}\n".encode(), "text/plain", 500)
        if p == "/api/v1/alerts":
            return self._json(alerts_view())
        if p == "/api/v1/hygiene":
            return self._json(hygiene_report())
        if p.startswith("/api/v1/reboot-plan/"):
            host = urllib.parse.unquote(p.rsplit("/", 1)[-1])
            if host not in VALID_HOSTS:
                return self._json({"error": "unknown host"}, 404)
            try:
                return self._json(CM.reboot_plan(CM.cfg(), host))
            except Exception as e:
                return self._json({"host": host, "allowed": False, "blockers": [f"plan failed: {e}"[:200]],
                                   "warnings": [], "impact": []})
        if p == "/api/v1/digest":
            if q.get("refresh") == "1" and not self._allowed("operate"):     # a new brief costs GPU time
                return self._deny("operate")
            try:
                return self._json(digest(force=q.get("refresh") == "1"))
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p == "/api/v1/runs":
            return self._json({"ts": time.time(), "runs": runs()})
        if p == "/api/v1/versions":
            return self._json(_versions())
        if p == "/api/v1/heal":
            from noctyx import change, heal
            c = CM.cfg()
            return self._json({**heal.summary(), "changes": [{k: v for k, v in r.items() if k != "snap"}
                                                              for r in change.ledger(30)[::-1]],
                               "policy": heal.policy(c), "hosts": [h["name"] for h in CM.hosts(c)],
                               "playbooks": {k: d for k, (_, d) in heal.PLAYBOOKS.items()}})
        if p == "/api/v1/changes":
            return self._json(changes_view())
        if p == "/api/v1/checkpoints":
            try:
                return self._json(checkpoints_view(q.get("refresh") == "1"))
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if p == "/api/v1/config-history":
            from noctyx import selfstate
            if q.get("diff"):
                if not self._allowed("admin"):        # the site config's text: admins (it holds no secrets)
                    return self._deny("admin")
                try:
                    return self._json({"diff": selfstate.config_diff(q["diff"])})
                except ValueError as e:
                    return self._json({"error": str(e)}, 404)
            return self._json({"history": selfstate.config_history()})
        if p == "/api/v1/self-checkpoints":
            from noctyx import selfstate
            plan = selfstate.rollback_plan()
            return self._json({"checkpoints": selfstate.self_checkpoints(), "current": selfstate.identity(),
                               "rollback": {"target": (plan.get("target") or {}).get("id"), "steps": plan["steps"],
                                            "problems": plan["problems"]}})
        if p == "/api/v1/heal/log":
            from noctyx import heal
            try:
                n = max(1, min(int(q.get("n") or 50), 500))
            except ValueError:
                n = 50
            return self._json({"runs": heal.runs(n, host=q.get("host") or None, check=q.get("check") or None,
                                                  everything=q.get("all") == "1", q=(q.get("q") or "")[:100] or None,
                                                  run=q.get("run") or None)})
        if p == "/api/v1/probes":
            return self._json(probe_last())
        if p == "/api/v1/ask-prompt":
            # what Ask is told: the caller's conversation (sid), or one they would open now
            who = self._ask_who()
            if not who:
                return self._deny("view")
            try:
                if q.get("sid"):
                    s = COP.get(q["sid"], who[0])
                    return self._json({"system": s.system(), "custom": s.custom, "notes": bool(s.site_notes)})
                return self._json(COP.preview(*who, q.get("tunit") or "F", q.get("tz")))
            except COP.Bad as e:
                return self._json({"error": str(e)}, 404)
        if p.startswith("/api/v1/ask/"):
            who = self._ask_who()
            if not who:
                return self._deny("view")
            try:
                sess = COP.get(p.rsplit("/", 1)[-1], who[0])     # a poll keeps the role the last message had
                return self._json(sess.view(int(q.get("since") or 0)))
            except (COP.Bad, ValueError) as e:
                return self._json({"error": str(e), "ended": True}, 404)
        if p == "/api/v1/jobs":
            with JOBS_LOCK:
                allj = sorted(JOBS.values(), key=lambda j: j.get("started_ts", 0))
                if q.get("active") == "1":
                    # was: `q.get("active") in ("1","") and not j["done"]` — the
                    # `or` fell through to "last 30 jobs" whenever the filter was
                    # empty, and a job that only *dies* (server restart) never
                    # leaves done=False, freezing badges at "running" forever.
                    js = [j for j in allj if not j["done"]]
                else:
                    js = allj[-30:]
            return self._json(js)
        if p.startswith("/api/v1/job/"):
            jid = p.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            return self._json(j or {"error": "no such job"}, code=200 if j else 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if DEMO:
            return self._json({"error": "demo: read-only (nothing here acts on a real cluster)"}, 403)
        if self.path != "/api/v1/alert-hook" and not self._origin_ok():
            return self._json({"error": "cross-origin request refused"}, 403)
        if self.path in ("/api/v1/login", "/api/v1/logout", "/api/v1/settings/mine"):
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            ip = self.client_address[0]
            if self.path.endswith("/logout"):
                return self._send(b'{"ok": true}', cookie="nx_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0")
            if self.path.endswith("/login"):
                if _login_locked(ip):
                    return self._json({"error": "too many failed sign-ins from here: wait a little"}, 429)
                name, pw = str(body.get("user") or "").strip().lower(), str(body.get("password") or "")
                role = AC.verify(name, pw) if name and pw else None
                try:
                    with AUDIT.open("a") as f:
                        f.write(json.dumps({"ts": _utcnow(), "event": "login", "user": name[:32], "ok": bool(role), "ip": ip}) + "\n")
                except Exception:
                    pass
                if not role:
                    _login_failed(ip)
                    return self._json({"error": "wrong name or password"}, 401)
                _login_ok(ip)
                ttl = 30 * 86400 if body.get("remember") else 12 * 3600
                return self._send(json.dumps({"ok": True, "user": name, "role": role}).encode(),
                                  cookie=f"nx_session={AC.make_session(name, ttl)}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={ttl}")
            w = self._who()                                       # settings/mine
            if not w:
                return self._json({"error": "sign in first", "login": True}, 401)
            s, errs = CM.validate_console(body.get("settings"))
            if errs:
                return self._json({"error": "; ".join(errs)}, 400)
            AC.put_settings(w[0], s)
            return self._json({"ok": True})
        if self.path == "/api/v1/download-ticket":
            if not self._allowed("operate"):
                return self._deny("operate")
            try:
                b = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            h, s, path, d = str(b.get("host") or ""), str(b.get("snapshot") or ""), str(b.get("path") or ""), bool(b.get("dir"))
            if h not in VALID_HOSTS or not _SNAP_RE.match(s) or not _ok_path(path) or path == "/":
                return self._json({"error": "bad request"}, 400)
            return self._json({"t": AC.sign(f"dl|{h}|{s}|{path}|{d}", 60)})
        if self.path == "/api/v1/alert-hook":
            # Alertmanager on this host only; no token (it has none) — loopback is the auth
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return self._json({"error": "loopback only"}, 403)
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            return self._json({"recorded": record_alerts(body)})
        if self.path == "/api/v1/explain":
            # explanations change nothing but cost GPU time: operator and up
            if not self._allowed("operate"):
                return self._deny("operate")
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            if body.get("kind") == "hygiene" and body.get("host") in VALID_HOSTS and isinstance(body.get("id"), str):
                return self._json(explain_hygiene(body["host"], body["id"]))
            return self._json({"error": "unknown kind/host"}, 400)
        if self.path in ("/api/v1/assistant-test", "/api/v1/assistant-key"):
            need = "admin" if self.path.endswith("-key") else "operate"
            if not self._allowed(need):
                return self._deny(need)
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            if self.path.endswith("-test"):
                return self._json(assistant_test(body.get("url"), body.get("model"), body.get("timeout_seconds") or 60))
            key = body.get("key")
            if key is not None and (not isinstance(key, str) or not 8 <= len(key.strip()) <= 512 or "\n" in key.strip()):
                return self._json({"error": "key: 8-512 characters, one line"}, 400)
            return self._json(set_assistant_key(key))
        if self.path == "/api/v1/ask":
            who = self._ask_who()
            if not who:
                return self._deny("view")
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            text, tunit, tz = str(body.get("text") or "").strip(), body.get("tunit"), body.get("tz")
            if not text:
                return self._json({"error": "ask something"}, 400)
            try:
                sess = COP.get(body["sid"], *who, tunit, tz) if body.get("sid") else COP.start(*who, tunit, tz)
                sess.say(text[:4000])
            except COP.Bad as e:
                return self._json({"error": str(e)}, 409)
            return self._json({"sid": sess.sid})
        if self.path in ("/api/v1/alert-silence", "/api/v1/alert-unsilence"):
            # a silence changes what the whole cluster alerts on: operator and up
            if not self._allowed("operate"):
                return self._deny("operate")
            try:
                body = self._body()
            except Exception:
                return self._json({"error": "bad json"}, 400)
            w = self._who()
            who = (w[0] if w else "") or "noctyx-console"
            try:
                if self.path.endswith("-unsilence"):
                    sid = body.get("id")
                    if not isinstance(sid, str) or not sid.strip():
                        return self._json({"error": "id required"}, 400)
                    expire_silence(sid.strip())
                    return self._json({"ok": True})
                fp = body.get("fingerprint")
                if not isinstance(fp, str) or not fp.strip():
                    return self._json({"error": "fingerprint required"}, 400)
                try:
                    mins = int(body.get("minutes") or 24 * 60)
                except Exception:
                    return self._json({"error": "minutes must be a whole number"}, 400)
                mins = max(1, min(mins, SILENCE_MAX_MIN))
                sid, hit = create_silence(fp.strip(), mins, who)
                labels = hit.get("labels") or {}
                audit_write({"event": "alert-silence", "user": who,
                             "alert": labels.get("alertname"), "host": labels.get("host"),
                             "component": labels.get("component"), "minutes": mins, "silence": sid})
                return self._json({"ok": True, "id": sid, "minutes": mins})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 502)
        if self.path != "/api/v1/action":
            return self._json({"error": "not found"}, 404)
        # NOCTYX_CONSOLE_AUTH=0 disables the token gate (dev mode, systemd unit). The gate
        # stays in code — flip env back to 1 to restore. Whitelist + validation
        # apply either way: auth-off must not mean injection-on.
        try:
            body = self._body()
            name, params = body.get("action"), body.get("params") or {}
        except Exception:
            return self._json({"error": "bad json"}, 400)
        need = "admin" if name in ADMIN_ACTIONS else "operate"
        if not self._allowed(need):
            return self._deny(need)
        if name == "setAssistant":
            errs = CM.validate_assistant(params.get("assistant"))[1]
            if errs:
                return self._json({"error": "; ".join(errs)}, 400)
        if name == "setHeal":
            errs = heal_errors(params.get("heal"))
            if errs:
                return self._json({"error": "; ".join(errs)}, 400)
        if name == "revertChange":
            from noctyx import change
            why = change.revertible(str(params.get("id") or ""))[1]
            if why:
                return self._json({"error": f"change {params.get('id')}: {why}"}, 400)
        if name == "recipeCatalog" and not params.get("remove"):
            errs = CM.validate_catalog_entry(params.get("name"), params.get("entry"), CM.cfg())
            if errs:
                return self._json({"error": "; ".join(errs)}, 400)
        if name == "setPolicy":   # say WHAT is wrong: these are hand-typed patterns
            _, errs = CM.validate_policy(params.get("policy"))
            if errs:
                return self._json({"error": "; ".join(errs)}, 400)
        if name not in ACTIONS or not valid_params(name, params):
            return self._json({"error": "unknown action or invalid host/profile"}, 400)
        w = self._who()
        return self._json({"id": run_job(name, params, user=w[0] if w else None)})


def _finish_head_reboot():
    """A head-node reboot cannot watch itself: the orchestrator left a pending record and the
    UI, starting on the new boot, runs the post-boot half (LLMs back?, hygiene, audit) as a job."""
    try:
        c = CM.cfg()
        for name, rec in CM.reboot_pending().items():
            h = next((x for x in CM.hosts(c) if x["name"] == name), None)
            if h and rec.get("head") and CM.is_head(c, h) and CM.boot_id(c, h) != rec.get("boot_id"):
                print(f"noctyx-console: {name} rebooted — finishing the reboot job", flush=True)
                run_job("rebootFinish", {"host": name})
    except Exception as e:
        print(f"noctyx-console: reboot finish check failed: {e}", flush=True)


def ensure_token():
    if not os.path.exists(TOKEN_FILE):
        UI_STATE.mkdir(parents=True, exist_ok=True)
        t = secrets.token_hex(24)
        pathlib.Path(TOKEN_FILE).write_text(t + "\n")
        os.chmod(TOKEN_FILE, 0o600)
        print(f"noctyx-console: wrote new token to {TOKEN_FILE}", flush=True)


def main():
    ensure_token()
    global RUNNING, _ORPHAN_SWEEP_DONE, _GRACEFUL
    RUNNING = threading.Semaphore(1)
    _ORPHAN_SWEEP_DONE = True
    _reap_orphan_jobs()   # resolve last boot's orphans NOW, not on first action
    _finish_head_reboot()
    # graceful exit (systemctl restart/stop): let the in-flight job worker
    # finish writing its real outcome before we go, so the next boot doesn't
    # have to mark it LOST. SIGKILL still falls back to the reaper.
    import signal as _sig
    def _bye(signum, frame):
        global _GRACEFUL
        if _GRACEFUL:
            return
        _GRACEFUL = True
        for th in _JOB_THREADS:
            th.join(timeout=12)
        with JOBS_LOCK:
            _persist_jobs()
        os._exit(0)
    for s in (_sig.SIGTERM, _sig.SIGHUP, _sig.SIGINT):
        _sig.signal(s, _bye)
    if not DEMO:                               # a known state of Noctyx itself, when it starts on new code
        from noctyx import selfstate
        threading.Thread(target=selfstate.checkpoint_if_new, daemon=True).start()
    if DEMO:                                   # plain HTTP on loopback: nothing real behind it
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        print(f"noctyx demo: http://127.0.0.1:{PORT}", flush=True)
        return httpd.serve_forever()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"noctyx-console: https://0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
