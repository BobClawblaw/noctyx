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
  GET  /api/v1/machine/<h>    one machine, everything: summary and why, every report tab, backups, components,
                              hygiene, alerts, self-repair, changes (the /machine page)
  GET  /api/v1/hf-mirror/tree  what the model mirror holds: kind › owner › repo › revision › files, sizes, gaps
  GET  /api/v1/ssh-log/<h>    SSH logins there, read from its log: ?days=1|7|30, head=1 adds the head key's, refresh=1
  GET  /api/v1/machines       every machine on one page: health and why, vitals, what it serves, storage, backups, signals
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
import base64, hmac, json, os, re, secrets, signal, socket, ssl, subprocess, sys, threading, time
import datetime, pathlib, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from noctyx import orchestrator as CM                # noqa: E402  the console never disagrees with `tick`
from noctyx.paths import P, CONFIG                   # noqa: E402
from noctyx import accounts as AC                    # noqa: E402
from noctyx import hostos                            # noqa: E402
from noctyx import sshlog as SSHLOG                  # noqa: E402
from noctyx import copilot as COP                    # noqa: E402
from noctyx import llm as LLM                        # noqa: E402

NX_HOME = pathlib.Path(__file__).resolve().parent
CFG_PATH = CONFIG
CFG = json.load(open(CFG_PATH))
S = CFG["server"]
_CFG_MTIME = os.path.getmtime(CFG_PATH)


def _reload_cfg():
    """Pick up an edited site config (noctyx init, policy-set, a hand edit) without a restart. The TLS files
    and the port stay as they were at start: those need one."""
    global CFG, S, _CFG_MTIME, VALID_HOSTS, VALID_PROFILES
    try:
        m = os.path.getmtime(CFG_PATH)
        if m != _CFG_MTIME:
            CFG, _CFG_MTIME = json.load(open(CFG_PATH)), m
            S = CFG["server"]
            VALID_HOSTS, VALID_PROFILES = _valid_hosts(CFG), set(CFG.get("profiles", {}).keys())
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
    # take a release newer than Noctyx's pin: the site's own pin moves (its sums come from the release's sums
    # file), then the row reads "behind" and its upgrade button installs it
    "pinRelease": lambda p: [ORCH, "versions-pin", "--component", p["component"], "--version", p["release"], "--yes"],
    "skipRelease": lambda p: [ORCH, "versions-skip", "--host", p["host"], "--component", p["component"]]
                             + (["--undo"] if p.get("undo") else ["--release", p["release"]]),
    "dumpsNow":   lambda p: [ORCH, "dumps"] + (["--host", p["host"]] if p.get("host") else []),
    "setSchedule": lambda p: [ORCH, "schedule-set", "--profile", p["profile"]]
                             + (["--host", p["host"]] if p.get("host") else [])
                             + ({"disable": ["--disable"], "reset": ["--reset"]}.get(p.get("mode"))
                                or ((["--every", p["every"]] if p.get("every") else [])
                                    + (["--window", p["window"]] if p.get("window") else []))),
}
def _valid_hosts(c):
    return {h["name"] for h in c["hosts"] if h.get("enabled", True) is not False}


VALID_HOSTS = _valid_hosts(CFG)
VALID_PROFILES = set(CFG.get("profiles", {}).keys())
# host-REQUIRED actions: their builders index p["host"] directly; missing would KeyError
HOST_REQUIRED = {"backupHost", "verifyHost", "reboot", "rebootFinish"}
RECIPE_ACTIONS = {"recipeDeploy", "recipeStop", "recipeStart", "recipeRollback", "recipeCatalog"}
PARAM_ACTIONS = HOST_REQUIRED | RECIPE_ACTIONS | {"upgradeComponent", "upgradeEverywhere", "skipRelease", "pinRelease", "setAssistant", "setHeal", "revertChange", "takeCheckpoint", "setCheckpoints", "configRestore", "setConsoleDefaults", "setSchedule", "restore", "hygieneFix", "setPolicy", "dumpsNow"}
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
                 "setConsoleDefaults", "recipeCatalog", "upgradeComponent", "pinRelease",
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
    if name == "pinRelease":
        return any(r["component"] == p.get("component") and (r.get("fix") or {}).get("pin") == p.get("release")
                   for rows in (_versions().get("hosts") or {}).values() for r in rows)
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
                    # -sk, not GNU's -sb: a macOS head's du has no -b
                    o = subprocess.run(["du", "-sk", d], capture_output=True, text=True, timeout=1800)
                    if o.returncode == 0 and o.stdout.split():
                        sizes[r["id"]] = int(o.stdout.split()[0]) * 1024
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


_NETFS = ("nfs", "nfs4", "smbfs", "cifs", "afpfs", "9p", "davfs")
_SYSMNT = ("/boot", "/private/", "/System/Volumes/", "/Volumes/Recovery", "/Volumes/.timemachine", "/Volumes/com.apple.", "/snap/", "/var/lib/")


def fleet_disks():
    """{host: [{mount, size, avail}]}: each host's own disks for the kiosk, one per device
    (a bind mount counts once), / first then by size. Network shares, boot and system
    volumes, and anything under 5 GB are left out; on macOS the data volume is "/"."""
    size = _pq(f'node_filesystem_size_bytes{{{_FS},host!=""}}')
    avail = {(r["metric"].get("host"), r["metric"].get("mountpoint")): _num(r["value"][1])
             for r in _pq(f'node_filesystem_avail_bytes{{{_FS},host!=""}}')}
    out = {}
    for r in sorted(size, key=lambda r: (len(r["metric"].get("mountpoint", "")), r["metric"].get("mountpoint", ""))):
        m = r["metric"]
        host, mnt, sz = m.get("host", "?"), m.get("mountpoint", ""), _num(r["value"][1])
        if mnt == "/System/Volumes/Data":
            mnt = "/"
        elif mnt.startswith(_SYSMNT) or (mnt == "/" and m.get("fstype") == "apfs"):
            continue
        if m.get("fstype") in _NETFS or not sz or sz < 5e9:
            continue
        seen = out.setdefault(host, {})
        if m.get("device") in seen:
            continue
        seen[m.get("device")] = {"mount": mnt, "size": sz, "avail": avail.get((host, m.get("mountpoint")))}
    return {h: sorted(d.values(), key=lambda x: (x["mount"] != "/", -x["size"])) for h, d in out.items()}


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
    try:
        for h, d in fleet_disks().items():
            hosts.setdefault(h, {})["disks"] = d
    except Exception as e:
        errors.append(f"disks: {str(e)[:120]}")
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


# restic reads on request threads, bounded: each is a process holding the repository's index in memory, and a
# burst of clicks must not start dozens. Downloads get their own, so a long one never blocks browsing.
RESTIC_READS = threading.BoundedSemaphore(4)
RESTIC_DUMPS = threading.BoundedSemaphore(2)
RESTIC_WAIT = 60


def _restic_ro(host, args, timeout=120, text=True):
    repo, path = _host_repo(host)
    RESTIC_CACHE.mkdir(parents=True, exist_ok=True)
    argv = ["restic", "-r", path, "--password-file", f"{P['repo_keys']}/{repo}.key",
            "--no-lock", "--cache-dir", str(RESTIC_CACHE), "--quiet"] + args
    if not RESTIC_READS.acquire(timeout=RESTIC_WAIT):
        busy = "the console is running as many repository reads as it allows: try again in a moment"
        return subprocess.CompletedProcess(argv, 75, "" if text else b"", busy if text else busy.encode())
    try:
        return subprocess.run(argv, capture_output=True, text=text, timeout=timeout)
    finally:
        RESTIC_READS.release()


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
    return LLM.endpoints(a, llm_topology())


KEY_DIR = pathlib.Path(P["llm_keys"])


def assistant_summary():
    """UI › Config: the assistant's settings (never the key), and endpoints to pick from:
    every serving model discovery knows, plus each recipe slot's port."""
    a = dict(CM.cfg().get("assistant") or {})
    a = {k: v for k, v in a.items() if not k.startswith("_")}
    a["has_key"] = bool(a.get("key_host") and _api_key(a["key_host"]))
    h = LLM.health()               # which endpoint answered last, and how many answer now
    a["health"] = {k: h.get(k) for k in ("asked", "answered", "model", "fallback", "error", "probed", "up", "total",
                                         "preferred")}
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
    # the stored key goes to the configured endpoint only: a test of another url must not hand it over
    key = _api_key(a.get("key_host")) if url == (a.get("url") or "").rstrip("/") else None
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


def assistant_ask(user, max_tokens=350, system=SYSTEM_PROMPT):
    """-> (text, model) or (None, reason). Tries the preferred endpoint, then fallbacks."""
    a = assistant_cfg()
    if not a:
        return None, "assistant disabled"
    return LLM.ask_any(a, system, user, max_tokens=max_tokens, topo=llm_topology())


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
    from noctyx import logdigest
    newlog = logdigest.counts()            # numbers only: what the lines say is for an operator (noctyx log-digest)
    ctx = ("Write the operator's morning brief for the cluster as 5-8 short lines (no headings, no markdown tables). "
           "Lead with anything wrong; then backups, then the LLM servers, then housekeeping; end with one line on what deserves attention today, if anything.\n"
           "Rules: the backup 'health' field is authoritative (ok = on schedule; do not call an ok host late or behind). "
           "'reboot' on a node is an advisory package flag that may lag by 15 minutes and is NOT a reason to reboot a node serving a model; mention it at most as 'flagged'. "
           "Housekeeping 'info' items are routine; only 'warning' items deserve attention. "
           "Self-repairs are what Noctyx fixed on its own: mention them in one line; problems it could not repair need the operator. "
           "New log kinds are warning or error lines a machine logged last night and on no earlier night: if any, say which "
           "machines and how many in one line, and that `noctyx log-digest --show` lists them.\n"
           f"UTC now: {_utcnow()}\nBackups (health vs schedule): {json.dumps(rows)}\n"
           f"Active alerts: {json.dumps(al.get('active'))}\nNodes: {json.dumps(nodes)}\nLLM servers: {json.dumps(llm)}\n"
           f"Last 24 h: {json.dumps({k: I.get(k) for k in ('tokens_24h', 'requests_24h', 'gpu_wh_24h', 'nas_free', 'backups_ok', 'backups_total')})}\n"
           f"NAS RAID: {json.dumps(I.get('raid'))} sync: {json.dumps(I.get('raid_sync'))}\nOpen hygiene findings: {json.dumps(hyg)}\n"
           f"Self-repairs in the last 24 h: {json.dumps(healed)}\nProblems Noctyx could not repair: {json.dumps(open_)}\n"
           f"New log kinds per machine: {json.dumps(newlog)}")
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

    def rehearsal():
        c = CM.cfg()
        gap = CM.rehearsal_every(c)
        if gap is None:
            return
        seen = CM.rehearsals().get("hosts") or {}
        out.append("# HELP noctyx_restore_rehearsal_ok 1: the host's last restore rehearsal restored files that match the live ones.")
        for h in CM.backup_hosts(c):
            r = seen.get(h["name"])
            l = _lbl(host=h["name"])
            if r:
                out.append(f"noctyx_restore_rehearsal_ok{l} {int(bool(r.get('ok')))}")
                out.append(f"noctyx_restore_rehearsal_timestamp_seconds{l} {r.get('ts', 0)}")
            out.append(f"noctyx_restore_rehearsal_overdue{l} {int(now - (r or {}).get('ts', 0) > gap + 3 * 86400)}")

    def assistant():
        a = assistant_cfg()
        if not a:
            return
        h = LLM.probe(a, topo=llm_topology())
        out.append("# HELP noctyx_assistant_endpoints_up The assistant's endpoints (its own, then the fallbacks) that list a model.")
        out.append(f"noctyx_assistant_endpoints_up {h.get('up', 0)}")
        out.append(f"noctyx_assistant_endpoints {h.get('total', 0)}")
        if h.get("preferred") is not None:
            out.append(f"noctyx_assistant_preferred_up{_lbl(url=a.get('url') or '')} {int(bool(h['preferred']))}")
        if h.get("asked"):
            out.append(f"noctyx_assistant_last_asked_timestamp_seconds {h['asked']}")
            out.append(f"noctyx_assistant_last_ask_ok {int(bool(h.get('answered')))}")

    sources = [("schedule", schedule), ("snapshots", snapshots), ("storage", storage), ("endpoint", endpoint),
               ("runs", jobs), ("hygiene", hygiene), ("components", components), ("heal", repairs),
               ("assistant", assistant), ("rehearsal", rehearsal)]
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
                a["selfrepair"] = {k: p.get(k) for k in ("state", "why", "detail", "how", "check", "diagnosis")}
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


_GPU_USE = {"ts": 0, "slots": {}}


def recipes_view():
    """The slots and the catalog, each slot with who is on its GPU (the lock's holder and waiters, the processes
    using it): read on each slot's head at most every 20 s, all slots at once."""
    c = CM.cfg()
    d = CM.recipe_summary(c)
    if time.time() - _GPU_USE["ts"] > 20:
        names = list(d["slots"])
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as ex:
            _GPU_USE.update(ts=time.time(), slots=dict(zip(names, ex.map(lambda n: CM.slot_gpu_use(c, n), names))))
    for n, s in d["slots"].items():
        s["gpu_use"] = _GPU_USE["slots"].get(n)
    return d


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


_MACHINES = {"ts": 0, "data": None}


def _by_host(q, key=None):
    """{host: value} (or {host: [(labels, value)]} with key) of a fleet-wide query."""
    out = {}
    for r in _pq(q):
        h = r["metric"].get("host")
        if not h:
            continue
        v = _num(r["value"][1])
        if key:
            out.setdefault(h, []).append(({k: r["metric"].get(k) for k in key}, v))
        else:
            out[h] = v
    return out


def _signals():
    """The early warnings' numbers for every machine at once (one query each, grouped by host)."""
    fs = 'fstype=~"ext4|xfs|btrfs|zfs|apfs",mountpoint="/"'
    return {
        "ssh_failed": _by_host("sum by (host) (increase(noctyx_ssh_failed_logins_total[1d]) + increase(noctyx_ssh_invalid_user_attempts_total[1d]))"),
        "ssh_ok": _by_host("sum by (host) (increase(noctyx_ssh_accepted_logins_total[1d]))"),
        "ssh_new": _by_host("noctyx_ssh_new_source_login_timestamp_seconds", key=("addr", "user")),
        "fab_state": _by_host("node_infiniband_state_id", key=("device",)),
        "fab_rate": _by_host("node_infiniband_rate_bytes_per_second", key=("device",)),
        "fab_err": _by_host("sum by (host) (increase(node_infiniband_link_downed_total[1h]) + increase(node_infiniband_symbol_error_total[1h]) + increase(node_infiniband_port_errors_received_total[1h]) + increase(node_infiniband_link_error_recovery_total[1h]))"),
        "fab_retrans": _by_host("sum by (host) (rate(node_infiniband_roce_adp_retransmits_total[10m]) + rate(node_infiniband_local_ack_timeout_errors_total[10m]) + rate(node_infiniband_out_of_sequence_packets_received_total[10m]))"),
        "nfs_ok": _by_host("noctyx_nfs_probe_ok", key=("mountpoint",)),
        "nfs_w": _by_host("noctyx_nfs_probe_write_bytes_per_second", key=("mountpoint",)),
        "nfs_r": _by_host("noctyx_nfs_probe_read_bytes_per_second", key=("mountpoint",)),
        "nfs_hung": _by_host("noctyx_nfs_probe_running_seconds"),
        "root_free": _by_host(f"node_filesystem_avail_bytes{{{fs}}}"),
        "root_size": _by_host(f"node_filesystem_size_bytes{{{fs}}}"),
        "root_use_per_day": _by_host(f"-deriv(node_filesystem_avail_bytes{{{fs}}}[7d]) * 86400"),
        "failed_units": _by_host('count by (host) (node_systemd_unit_state{state="failed"} == 1)'),
    }


def machines_view():
    """/machines: everything about every machine on one page, worst first, each with why it needs a look."""
    if _MACHINES["data"] and time.time() - _MACHINES["ts"] < 15:
        return _MACHINES["data"]
    from noctyx import heal
    ov, fm = overview(), fleet_metrics()
    al = alerts_view().get("active") or []
    problems = heal.summary().get("problems") or []
    try:
        sig = _signals()
    except Exception:
        sig = {}
    c = CM.cfg()
    slots = CM.recipes_cfg(c)["slots"]
    rank = {"bad": 0, "warn": 1, "ok": 2}
    out = []
    for h in ov["hosts"]:
        n = h["name"]
        M = (fm.get("hosts") or {}).get(n) or {}
        F = (ov.get("facts") or {}).get(n) or {}
        why_bad, why_warn = [], []
        up = M.get("up")
        if up == 0:
            why_bad.append("no metrics: the exporter does not answer")
        hcfg = next((x for x in CFG["hosts"] if x["name"] == n), {})
        role = ["head node"] if CM.is_head(c, hcfg) else []
        role += [f"{s} pair head" if v.get("worker") else f"{s} slot" for s, v in slots.items() if v["head"] == n]
        role += [f"{s} pair worker (with {v['head']})" for s, v in slots.items() if v.get("worker") == n]
        if h.get("monitored_only"):
            role.append("monitored only (no backup)")
        L = M.get("llm") or {}
        serves = [{"model": x.get("model"), "tps": x.get("gen_tps"), "running": x.get("running"), "waiting": x.get("waiting"),
                   "kv_pct": x.get("kv_pct")} for x in (L.get("servers") or [])]
        if not serves and L.get("worker_for"):
            serves = [{"model": L.get("worker_model"), "worker_for": L.get("worker_for")}]
        if not serves:
            serves = [{"model": t.get("model"), "unscraped": True} for t in L.get("topology") or [] if t.get("role") == "serving"]
        # backups
        rows = [r for r in (ov.get("schedule") or {}).get("rows") or [] if r["host"] == n and r.get("enabled")]
        for r in rows:
            if r.get("failed_at"):
                why_warn.append(f"{r['profile']} backup failed last time")
            if r.get("health") in ("stale", "never"):
                why_bad.append(f"{r['profile']} backup {r['health']}")
            elif r.get("health") == "late":
                why_warn.append(f"{r['profile']} backup late")
        # storage
        free, size, per_day = (sig.get("root_free") or {}).get(n), (sig.get("root_size") or {}).get(n), (sig.get("root_use_per_day") or {}).get(n)
        days = free / per_day if free and per_day and per_day > 0 else None
        pct = M.get("root_pct")
        if pct is not None and pct >= 92:
            why_bad.append(f"root disk {pct:.0f}% full")
        elif pct is not None and pct >= 85:
            why_warn.append(f"root disk {pct:.0f}% full")
        if days is not None and days < 14:
            why_warn.append(f"root disk full in ~{days:.0f} days at this week's rate")
        disks = [d for d in F.get("disks") or [] if (d.get("size_gb") or 0) >= 5 and not str(d.get("mnt", "")).startswith(("/boot", "/sys", "/System/Volumes", "/private"))]
        # versions, hygiene, units
        vrows = ((ov.get("versions") or {}).get("hosts") or {}).get(n) or []
        behind = [f"{r['component']} {r.get('installed') or '—'} → {r.get('pinned') or r.get('latest')}" for r in vrows if r.get("state") in ("behind", "missing")]
        newer = [f"{r['component']} {r.get('latest')}" for r in vrows if r.get("state") == "upgrade"]
        if behind:
            why_warn.append("behind: " + ", ".join(behind))
        hy = M.get("hygiene") or {}
        if hy.get("warnings"):
            why_warn.append(f"{hy['warnings']} hygiene warning(s)")
        if M.get("reboot"):
            why_warn.append("reboot required")
        fu = int((sig.get("failed_units") or {}).get(n) or 0)
        if fu:
            why_warn.append(f"{fu} failed unit(s)")
        # signals
        ssh_new = [{"addr": l["addr"], "user": l["user"], "ts": v} for l, v in (sig.get("ssh_new") or {}).get(n, [])]
        if ssh_new:
            why_warn.append("ssh login from a new address")
        ssh = {"ok_24h": (sig.get("ssh_ok") or {}).get(n), "failed_24h": (sig.get("ssh_failed") or {}).get(n), "new": ssh_new}
        rate = {l["device"]: v for l, v in (sig.get("fab_rate") or {}).get(n, [])}
        ports = [{"device": l["device"], "up": v == 4, "rate": rate.get(l["device"])} for l, v in (sig.get("fab_state") or {}).get(n, [])]
        fabric = None
        if ports:
            fabric = {"active": [p for p in ports if p["up"]], "ports": len(ports),
                      "errors_1h": (sig.get("fab_err") or {}).get(n), "retrans": (sig.get("fab_retrans") or {}).get(n)}
            if (fabric["errors_1h"] or 0) > 0:
                why_warn.append(f"fabric: {fabric['errors_1h']:.0f} link errors in the last hour")
        w = {l["mountpoint"]: v for l, v in (sig.get("nfs_w") or {}).get(n, [])}
        r_ = {l["mountpoint"]: v for l, v in (sig.get("nfs_r") or {}).get(n, [])}
        nfs = [{"mount": l["mountpoint"], "ok": v == 1, "write": w.get(l["mountpoint"]), "read": r_.get(l["mountpoint"])}
               for l, v in (sig.get("nfs_ok") or {}).get(n, [])]
        for x in nfs:
            if not x["ok"]:
                why_bad.append(f"NFS {x['mount']}: the probe fails")
        hung = (sig.get("nfs_hung") or {}).get(n)
        if hung and hung > 180:
            why_bad.append(f"an NFS mount hangs ({hung / 60:.0f} min)")
        # self-repair and alerts
        mine = [p for p in problems if p.get("host") == n]
        for p in mine:
            if p.get("needs_person"):
                why_bad.append("needs a person: " + p["what"])
        alerts = [{"name": a["name"], "severity": a.get("severity"), "summary": a.get("summary"), "since": a.get("since")}
                  for a in al if a.get("host") == n and a.get("severity") != "info"]
        for a in alerts:
            (why_bad if a["severity"] == "critical" else why_warn).append(f"alert {a['name']}")
        status = "bad" if why_bad else "warn" if why_warn else "ok"
        out.append({
            "name": n, "status": status, "role": role,
            "why": [{"text": t, "level": "bad"} for t in why_bad] + [{"text": t, "level": "warn"} for t in why_warn], "os": h.get("os"), "arch": h.get("arch"),
            "os_line": (F.get("os_lines") or [None])[0], "kernel": ((F.get("os_lines") or [None, None]) + [None])[1],
            "uptime": (F.get("uptime_lines") or [None])[0], "boot": ((F.get("uptime_lines") or [None, None]) + [None])[1],
            "ip": F.get("ip"), "cores": (F.get("cpu") or [None])[0], "gpu_name": ((F.get("gpu") or [""])[0] or "").split(",")[0] or None,
            "up": up, "cpu": M.get("cpu"), "load": M.get("load1"), "mem_used": M.get("mem_used"), "mem_total": M.get("mem_total"),
            "gpu_util": M.get("gpu_util"), "gpu_temp": M.get("gpu_temp"), "gpu_power": M.get("gpu_power"),
            "sys_temp": M.get("sys_temp"), "nvme_temp": M.get("nvme_temp"), "containers": M.get("containers"),
            "net_rx": M.get("net_rx"), "net_tx": M.get("net_tx"), "fabric_rx": M.get("fabric_rx"), "fabric_tx": M.get("fabric_tx"),
            "serves": serves, "root_pct": pct, "root_free": free, "root_size": size, "root_use_per_day": per_day,
            "root_full_days": days, "disks": disks,
            "backups": [{"profile": r["profile"], "health": r.get("health"), "last_ts": r.get("last_ts"),
                         "failed_at": r.get("failed_at"), "next_ts": r.get("next_ts")} for r in rows],
            "snapshots": (((ov.get("snapshots") or {}).get("hosts") or {}).get(n) or {}).get("count"),
            "behind": behind, "newer": newer, "hygiene": hy, "reboot": M.get("reboot"), "failed_units": fu,
            "ssh": ssh, "fabric": fabric, "nfs": nfs, "nfs_hung_s": hung,
            "repairs": [{"what": p["what"], "state": p.get("state"), "why": p.get("why"), "diagnosis": p.get("diagnosis")}
                        for p in mine],
            "alerts": alerts})
    order = [x["name"] for x in ov["hosts"]]
    out.sort(key=lambda m: (rank[m["status"]], order.index(m["name"])))
    I = (fm.get("fleet") or {}).get("infra") or {}
    tps = sum((x.get("tps") or 0) for m in out for x in m["serves"])
    data = {"ts": time.time(), "machines": out,
            "fleet": {"machines": len(out), "up": sum(1 for m in out if m["up"] == 1),
                      "bad": sum(1 for m in out if m["status"] == "bad"), "warn": sum(1 for m in out if m["status"] == "warn"),
                      "alerts": sum(1 for a in al if a.get("severity") != "info"), "tps": tps,
                      "backups_ok": I.get("backups_ok"), "backups_total": I.get("backups_total"),
                      "repairs_24h": len(heal.summary().get("repairs") or [])}}
    _MACHINES.update(ts=time.time(), data=data)
    return data


def machine_view(name):
    """/machine?host=…: one machine, everything at once: its summary (status and why), every report tab's live
    numbers, its facts, backups and snapshots, components, hygiene, alerts, self-repair and changes."""
    from noctyx import change
    tabs = ("overview", "llm", "network", "storage", "gpu", "health")
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut = {t: ex.submit(host_metrics, name, t) for t in tabs}
        fut["facts"] = ex.submit(host_facts, name, 120)
        fut["machines"] = ex.submit(machines_view)
        fut["alerts"] = ex.submit(alerts_view)
        got = {}
        for k, f in fut.items():
            try:
                got[k] = f.result()
            except Exception as e:
                got[k] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    ms = (got["machines"] or {}).get("machines") or []
    me = next((m for m in ms if m["name"] == name), None)
    ov = overview()
    rows = [r for r in (ov.get("schedule") or {}).get("rows") or [] if r["host"] == name]
    sn = ((ov.get("snapshots") or {}).get("hosts") or {}).get(name) or {}
    h = next((x for x in ov["hosts"] if x["name"] == name), {"name": name})
    recs = change.ledger(2000)
    st = change.state_of(recs)
    top = {v[-1]["id"] for v in change.stacks(recs).values() if v}
    mine = [r for r in recs if r.get("host") == name][-15:][::-1]
    return {"ts": time.time(), "host": name, "summary": me, "host_cfg": h,
            "machines": [{"name": m["name"], "status": m["status"]} for m in ms],
            "tabs": {t: got[t] for t in tabs}, "facts": got["facts"],
            "schedule": rows, "snapshots": {"count": sn.get("count"), "last_ts": sn.get("last_ts"),
                                            "recent": list(reversed(sn.get("recent") or []))[:8]},
            "versions": ((ov.get("versions") or {}).get("hosts") or {}).get(name) or [],
            "versions_ts": (ov.get("versions") or {}).get("ts"),
            "hygiene": (hygiene_report().get("hosts") or {}).get(name) or {},
            "alerts": [a for a in (got["alerts"] or {}).get("active") or [] if a.get("host") == name],
            "changes": [{**{k: v for k, v in r.items() if k != "snap"}, "state": st.get(r["id"]),
                         "revertible": r["id"] in top and not change.undoes(r)} for r in mine],
            "job": None}


# Favicon + brand mark: the watching eye (assets/favicon.svg, the source of truth), inlined so the
# page stays one file.
FAVICON = 'data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%20%3Cdefs%3E%20%3ClinearGradient%20id%3D%22tile%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%220%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23151b3d%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%2305060d%22%2F%3E%20%3C%2FlinearGradient%3E%20%3ClinearGradient%20id%3D%22neon%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%233ff0ff%22%2F%3E%3Cstop%20offset%3D%22.55%22%20stop-color%3D%22%235b8cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b45cff%22%2F%3E%20%3C%2FlinearGradient%3E%20%3CradialGradient%20id%3D%22iris%22%20cx%3D%22.5%22%20cy%3D%22.42%22%20r%3D%22.62%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23f0feff%22%2F%3E%3Cstop%20offset%3D%22.3%22%20stop-color%3D%22%235ff3ff%22%2F%3E%3Cstop%20offset%3D%22.78%22%20stop-color%3D%22%235b6cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%237a3cff%22%2F%3E%20%3C%2FradialGradient%3E%20%3Cfilter%20id%3D%22glow%22%20x%3D%22-30%25%22%20y%3D%22-30%25%22%20width%3D%22160%25%22%20height%3D%22160%25%22%3E%20%3CfeGaussianBlur%20stdDeviation%3D%222%22%20result%3D%22b%22%2F%3E%3CfeMerge%3E%3CfeMergeNode%20in%3D%22b%22%2F%3E%3CfeMergeNode%20in%3D%22SourceGraphic%22%2F%3E%3C%2FfeMerge%3E%20%3C%2Ffilter%3E%20%3C%2Fdefs%3E%20%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2215%22%20fill%3D%22url%28%23tile%29%22%2F%3E%20%3Crect%20x%3D%22.75%22%20y%3D%22.75%22%20width%3D%2262.5%22%20height%3D%2262.5%22%20rx%3D%2214.25%22%20fill%3D%22none%22%20stroke%3D%22%233ff0ff%22%20stroke-opacity%3D%22.25%22%20stroke-width%3D%221.5%22%2F%3E%20%3Cg%20fill%3D%22%23d6ecff%22%3E%3Ccircle%20cx%3D%2212%22%20cy%3D%2212.5%22%20r%3D%221.1%22%2F%3E%3Ccircle%20cx%3D%2251.5%22%20cy%3D%2210%22%20r%3D%22.8%22%2F%3E%3Ccircle%20cx%3D%2254%22%20cy%3D%2252.5%22%20r%3D%221%22%2F%3E%3Ccircle%20cx%3D%229.5%22%20cy%3D%2250%22%20r%3D%22.7%22%2F%3E%3C%2Fg%3E%20%3Cg%20filter%3D%22url%28%23glow%29%22%3E%20%3Cpath%20d%3D%22M6.5%2032%20Q32%207%2057.5%2032%20Q32%2057%206.5%2032%20Z%22%20fill%3D%22none%22%20stroke%3D%22url%28%23neon%29%22%20stroke-width%3D%223.8%22%20stroke-linejoin%3D%22round%22%2F%3E%20%3Ccircle%20cx%3D%2232%22%20cy%3D%2232%22%20r%3D%2210.8%22%20fill%3D%22url%28%23iris%29%22%2F%3E%20%3C%2Fg%3E%20%3Cpath%20d%3D%22M32%2021.8%20Q35.6%2032%2032%2042.2%20Q28.4%2032%2032%2021.8%20Z%22%20fill%3D%22%2305060d%22%2F%3E%20%3Ccircle%20cx%3D%2228.2%22%20cy%3D%2227.9%22%20r%3D%222.1%22%20fill%3D%22%23fff%22%20fill-opacity%3D%22.92%22%2F%3E%20%3C%2Fsvg%3E'
# The pages are files beside the code (assets/): the sign-in page, and the one page that is the whole console
# (its CSS and JS inline, so the browser makes one request).
LOGIN_PAGE = (NX_HOME / "assets" / "login.html").read_text()


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


# changes whenever this file or a page is edited and the UI restarted
UI_BUILD = str(int(max(os.path.getmtime(f) for f in (__file__, NX_HOME / "assets" / "console.html", NX_HOME / "assets" / "login.html"))))
PAGE = (NX_HOME / "assets" / "console.html").read_text()


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
        return bool(tok) and hmac.compare_digest(self.headers.get("Authorization") or "", f"Bearer {tok}")

    def _loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

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
        if not RESTIC_DUMPS.acquire(timeout=RESTIC_WAIT):
            return self._json({"error": "two downloads are running already: try again when one ends"}, 503)
        try:
            return self._stream_dump_slot(host, snap, path, name, repo, rpath, is_dir)
        finally:
            RESTIC_DUMPS.release()

    def _stream_dump_slot(self, host, snap, path, name, repo, rpath, is_dir):
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
        # /metrics without sign-in for the head's own Prometheus (loopback) only: it names hosts, repos, versions
        if not (p.startswith("/assets/") or p in ("/favicon.ico", "/favicon.svg") or (p == "/metrics" and self._loopback())) \
                and not self._allowed("view"):
            if p.startswith("/api/"):
                return self._deny("view")
            return self._redirect("/login?next=" + urllib.parse.quote(self.path, safe=""))
        if p == "/api/v1/settings/mine":
            w = self._who()
            return self._json({"settings": AC.get_settings(w[0]) if w else {}, "user": w[0] if w else None})
        if p in ("/", "/index.html"):
            return self._send(page(), "text/html; charset=utf-8")
        if p in ("/cluster", "/cluster/", "/config", "/config/", "/recipes", "/recipes/", "/about", "/about/",
                 "/kiosk", "/kiosk/", "/heal", "/heal/", "/machines", "/machines/", "/machine", "/machine/"):
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
                return self._json(recipes_view())
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
        if p.startswith("/api/v1/machine/"):
            name = p.rsplit("/", 1)[-1]
            if name not in VALID_HOSTS:
                return self._json({"error": "unknown host"}, 404)
            try:
                return self._json(machine_view(name))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)
        if p == "/api/v1/hf-mirror/tree":
            from noctyx.cache import tree as hftree
            try:
                v = hftree.cached(CM.hf_cfg(CM.cfg())["store"], UI_STATE / "hf-tree.json", max_age=0 if q.get("refresh") else 600)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
            return self._json(v)
        if p.startswith("/api/v1/ssh-log/"):
            name = p.rsplit("/", 1)[-1]
            h = next((x for x in CFG["hosts"] if x["name"] == name), None)
            if name not in VALID_HOSTS or not h:
                return self._json({"error": "unknown host"}, 404)
            try:
                days = int(q.get("days", "7"))
                v = SSHLOG.cached(CM.cfg(), h, days, max_age=0 if q.get("refresh") else 120)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
            if "error" in v:
                return self._json(v, 502)
            # the head's own key logs in every few seconds: its rows come only when asked for
            ev = v["events"] if q.get("head") == "1" else [e for e in v["events"] if not e.get("head_key")]
            cap = 5000
            c = CM.cfg()
            names = {}
            for x in c["hosts"]:
                if x.get("address"):
                    names[CM.addr(x)] = x["name"]
                    try:                                    # a name (gpu-01.local): its addresses too
                        for ai in socket.getaddrinfo(CM.addr(x), None, proto=socket.IPPROTO_TCP):
                            names.setdefault(ai[4][0], x["name"])
                    except OSError:
                        pass
            names[c["server"]["address"]] = c["server"].get("hostname") or "head"
            return self._json({**v, "events": ev[-cap:], "shown": min(len(ev), cap), "matching": len(ev), "names": names})
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
        if p == "/api/v1/machines":
            try:
                return self._json(machines_view())
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)
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
            need = "admin"                # like setAssistant: it reaches out to a url the caller names
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
