"""noctyx heal: the repair loop. Every few minutes (the heal timer), and at once when an alert it has a playbook
for fires, it compares the fleet with what Noctyx expects and repairs what it safely can. Each check is a
playbook (PLAYBOOKS):

  playbook   broken when                                      repaired by
  service    a Noctyx timer or service, Prometheus or         a restart
             Alertmanager stopped, failed, or not answering
  exporter   Prometheus cannot scrape a node's exporter       undoing Noctyx's own change of it in the last day;
                                                              else restarting a stopped one
  dropin     the exporter's drop-in differs from Noctyx's     writing it again (a change: checked, put back if
                                                              the exporter then fails)
  textfile   the textfile collector's file is 5 min old       restarting its timer
  rules      the alert rules differ from this Noctyx's        writing them again (promtool, reload, put back)
  disk       a root filesystem is over heal.disk_pct full     vacuuming the journal to 1 GiB and apt's package
                                                              cache; never Docker. Still full: the biggest
                                                              directories, for a person
  units      a systemd unit failed                            reset-failed when its unit file is gone; a restart
                                                              only for units in heal.restart_units
  nas        the NAS backup endpoint does not answer          starting it on the NAS
  backups    a backup failed                                  none (the tick retries it): the cause, for a person
  slots      a model server in heal.restart_slots stopped     noctyx recipe-start, after 15 min down, and only
             answering                                        once Noctyx has seen that slot's own unit serve
  upgrades   heal.upgrades.auto (off by default): a           noctyx upgrade, inside the window, one machine per
             component is older than Noctyx's pin             run, waiting soak_minutes between machines

Safety, on top of each playbook's own checks (site config "heal", validate_policy):
  - each thing is repaired at most 3 times in 6 hours; then it needs a person (NoctyxNeedsAttention);
  - at most budget_per_hour repairs across the fleet in any hour;
  - freeze_after failed repairs in a row freeze every repair until a person runs `noctyx heal --unfreeze`;
  - auto: false turns every repair off (report only); a playbook can be on, shadow (logs what it would do)
    or off;
  - it never stops or changes a container, a backup or the Bitcoin node, never restarts a unit in NEVER_UNITS,
    and waits while a change (noctyx upgrade) runs or a machine reboots.

Every run goes to heal-log.jsonl (what each check found, each decision and why, every command with its output),
which the console's Self-repair page shows; every repair also goes to the audit trail and heal.json.

    noctyx heal [--dry-run] [--host H] [--trigger WHY] [--unfreeze]
"""
import contextlib
import datetime
import fcntl
import fnmatch
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

from noctyx import change, upgrades
from noctyx.paths import P

HEAL_FILE = pathlib.Path(P["state"]) / "heal.json"
LOG_FILE = pathlib.Path(P["state"]) / "heal-log.jsonl"
LOCK_FILE = pathlib.Path(P["state"]) / "heal.lock"
LOG_MAX = 8 * 1024 * 1024           # then heal-log.jsonl moves to heal-log.jsonl.1 (one old file kept)
TRIES, WINDOW = 3, 6 * 3600
NOT_READY_FOR = 600               # an active service that does not answer this long is restarted
TEXTFILE_MAX_AGE = 300
SLOT_DOWN_FOR = 900               # a model server down this long is restarted (when its slot opted in)
SLOT_SEEN_WITHIN = 7 * 86400      # ... and only when Noctyx saw that slot's own unit serve this recently
LONG_RUNNING = ("console.service", "directory.service", "hf-mirror.service")
TIMERS = ("tick.timer", "nightly.timer", "retention.timer", "hygiene.timer", "heal.timer")

# check -> (default mode, what it repairs: the console's Self-repair page and the docs)
PLAYBOOKS = {
    "service":  ("on", "a stopped or failed Noctyx service, Prometheus or Alertmanager, or one that has not "
                       "answered for 10 min: a restart"),
    "exporter": ("on", "an exporter Prometheus cannot scrape: undo Noctyx's change of it, or restart it"),
    "dropin":   ("on", "the exporter's drop-in differs from Noctyx's: write it again, checked"),
    "textfile": ("on", "the GPU/Docker metrics file is stale: restart its timer"),
    "rules":    ("on", "the alert rules differ from Noctyx's: write them again, checked"),
    "disk":     ("on", "a root filesystem over the limit: vacuum the journal and apt's cache (never Docker)"),
    "units":    ("on", "a failed unit: clear a ghost; restart only the units listed in restart_units"),
    "nas":      ("on", "the NAS backup endpoint does not answer: start it"),
    "backups":  ("on", "a failed backup: its cause, for a person (the tick retries it)"),
    "slots":    ("on", "a model server listed in restart_slots, down 15 min: start it again"),
    "upgrades": ("on", "upgrades.auto only: a component older than Noctyx's pin, upgraded in the window"),
}
MODES = ("on", "shadow", "off")
# which playbook an alert wakes (the console's alert webhook runs `noctyx heal --host H` for these)
ALERT_CHECKS = {"HostRootFilesystemFull": "disk", "HostSystemdUnitFailed": "units", "NodeExporterDown": "exporter",
                "BackupJobFailed": "backups", "RestEndpointDown": "nas", "LLMServerDown": "slots",
                "TickTimerInactive": "service", "ComponentBehind": "upgrades"}
UPGRADABLE = ("node_exporter", "prometheus", "alertmanager")   # the components Noctyx upgrades as a change
DEFAULTS = {"auto": True, "budget_per_hour": 10, "freeze_after": 3, "disk_pct": 92, "playbooks": {},
            "restart_units": [], "restart_slots": [],
            "upgrades": {"auto": False, "window": "03:00-05:00", "components": list(UPGRADABLE), "major": False,
                         "soak_minutes": 60}}
# never restarted, whatever restart_units says: model servers, containers, the Bitcoin node, the way in
NEVER_UNITS = ("docker*", "containerd*", "*bitcoin*", "tensorfold*", "vllm*",
               "sglang*", "ollama*", "mtplx*", "llama*", "glm*", "qwen*", "deepseek*", "nemotron*", "ssh*",
               "systemd-*", "networking*", "NetworkManager*", "nvidia*", "user@*", "*.mount", "*.swap", "*.slice",
               "*.scope", "*.target")


def _cm():
    from noctyx import orchestrator as cm
    return cm


# ── the run log: what each run found, decided and ran, for the Self-repair page ──
class RunLog:
    def __init__(self, trigger, host=None, dry=False):
        self.t0 = time.time()
        self.rec = {"run": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
                    "ts": change._utc(), "ts_epoch": self.t0, "trigger": trigger or "cli", "host": host,
                    "dry": bool(dry), "steps": [], "checks": {}}
        self.check = None

    def step(self, kind, text, host=None, **kw):
        s = {"t": round(time.time() - self.t0, 2), "kind": kind, "text": str(text)[:2000]}
        if self.check:
            s["check"] = self.check
        if host:
            s["host"] = host
        s.update({k: v for k, v in kw.items() if v is not None})
        self.rec["steps"].append(s)

    @contextlib.contextmanager
    def checking(self, name):
        self.check, t = name, time.time()
        try:
            yield
        except Exception as e:
            self.step("error", f"the {name} check failed: {type(e).__name__}: {e}")
        finally:
            self.rec["checks"][name] = round(time.time() - t, 2)
            self.check = None

    def finish(self, summary):
        self.rec["seconds"] = round(time.time() - self.t0, 2)
        self.rec["summary"] = summary
        quiet = not any(summary.get(k) for k in ("found", "repaired", "failed", "partial", "shadow", "waiting",
                                                  "needs_person"))
        if quiet:                       # nothing found: keep the timings, not each check's commands
            self.rec["steps"] = [s for s in self.rec["steps"] if s["kind"] not in ("cmd",)]
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
            with LOG_FILE.open("a") as f:
                f.write(json.dumps(self.rec) + "\n")
            LOG_FILE.chmod(0o644)
        except OSError:
            pass


_LOG = None


def _note(kind, text, host=None, **kw):
    if _LOG:
        _LOG.step(kind, text, host, **kw)


def runs(n=50, host=None, check=None, everything=False, q=None, run=None):
    """The newest runs first, from heal-log.jsonl (and its one rotated file). everything: also the runs that
    found nothing. host/check: runs with a step about them. q: text in any step."""
    out = []
    for f in (LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1")):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if run and r.get("run") != run:
                continue
            steps = r.get("steps") or []
            if not run:
                if not everything and not (r.get("summary") or {}).get("found") and not r.get("frozen_now"):
                    continue
                if host and not any(s.get("host") == host for s in steps) and r.get("host") != host:
                    continue
                if check and not any(s.get("check") == check for s in steps):
                    continue
                if q and q.lower() not in line.lower():
                    continue
            out.append(r)
            if len(out) >= n:
                return out
    return out


def _run(argv, timeout=60):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        rc, out = r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        rc, out = -1, str(e)
    if argv[:2] != ["systemctl", "show"]:
        _note("cmd", " ".join(shlex.quote(a) for a in argv), rc=rc, out=out[-1500:])
    return rc, out


def _unit(name):
    """{LoadState, ActiveState, UnitFileState, Type} of a systemd unit."""
    rc, out = _run(["systemctl", "show", "-p", "LoadState,ActiveState,UnitFileState,Type", name])
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def load():
    try:
        return json.loads(HEAL_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st):
    HEAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEAL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.chmod(0o644)
    tmp.replace(HEAL_FILE)


def policy(c):
    """The site's "heal" block over DEFAULTS."""
    got = c.get("heal") or {}
    pol = {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS and k != "upgrades"}}
    pol["upgrades"] = {**DEFAULTS["upgrades"], **(got.get("upgrades") or {})}
    pol["playbooks"] = {k: (got.get("playbooks") or {}).get(k) or m for k, (m, _) in PLAYBOOKS.items()}
    return pol


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def validate_policy(p):
    """-> (clean, errors) of a site "heal" block (noctyx heal-set, the console's Configure page)."""
    if not isinstance(p, dict):
        return None, ["heal: an object"]
    errs, out = [], {}
    for k in p:
        if k not in DEFAULTS and not k.startswith("_"):
            errs.append(f"{k}: unknown (known: {', '.join(DEFAULTS)})")
    if "auto" in p:
        if not isinstance(p["auto"], bool):
            errs.append("auto: true or false")
        out["auto"] = p["auto"]
    for k, lo, hi in (("budget_per_hour", 1, 100), ("freeze_after", 1, 20), ("disk_pct", 50, 99)):
        if k in p:
            if not isinstance(p[k], int) or isinstance(p[k], bool) or not lo <= p[k] <= hi:
                errs.append(f"{k}: a whole number from {lo} to {hi}")
            out[k] = p[k]
    if "playbooks" in p:
        pb = p["playbooks"]
        if not isinstance(pb, dict) or any(k not in PLAYBOOKS or v not in MODES for k, v in pb.items()):
            errs.append(f"playbooks: {{playbook: {'|'.join(MODES)}}} for {', '.join(PLAYBOOKS)}")
        else:
            out["playbooks"] = {k: v for k, v in pb.items() if v != PLAYBOOKS[k][0]}
    if "restart_units" in p:
        ru = p["restart_units"]
        if not isinstance(ru, list) or not all(isinstance(x, str) for x in ru):
            errs.append("restart_units: a list of unit names or patterns")
        else:
            for x in ru:
                lit = re.sub(r"[*?\[\]]", "", x)
                if not re.fullmatch(r"[A-Za-z0-9@._*?\[\]:-]{1,120}", x) or len(lit) < 4:
                    errs.append(f"restart_units: {x!r} is not a unit name, or matches too much (4+ letters)")
                elif any(fnmatch.fnmatch(lit, n) for n in NEVER_UNITS):
                    errs.append(f"restart_units: Noctyx never restarts {x!r} (a model server, container, the Bitcoin "
                                "node or the system's own)")
            out["restart_units"] = ru
    if "restart_slots" in p:
        rs = p["restart_slots"]
        if not isinstance(rs, list) or not all(isinstance(x, str) and re.fullmatch(r"[\w.-]{1,64}", x) for x in rs):
            errs.append("restart_slots: a list of recipe slot names")
        out["restart_slots"] = rs
    if "upgrades" in p:
        u = p["upgrades"]
        if not isinstance(u, dict):
            errs.append("upgrades: an object")
        else:
            uo = {}
            for k in u:
                if k not in DEFAULTS["upgrades"]:
                    errs.append(f"upgrades.{k}: unknown (known: {', '.join(DEFAULTS['upgrades'])})")
            for k in ("auto", "major"):
                if k in u:
                    if not isinstance(u[k], bool):
                        errs.append(f"upgrades.{k}: true or false")
                    uo[k] = u[k]
            if "window" in u:
                w = u["window"]
                m = isinstance(w, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", w)
                if not m or _hm(w[:5]) == _hm(w[6:]):
                    errs.append("upgrades.window: HH:MM-HH:MM in the head's local time, e.g. 03:00-05:00")
                uo["window"] = w
            if "components" in u:
                cs = u["components"]
                if not isinstance(cs, list) or any(x not in UPGRADABLE for x in cs):
                    errs.append(f"upgrades.components: some of {', '.join(UPGRADABLE)}")
                uo["components"] = cs
            if "soak_minutes" in u:
                s = u["soak_minutes"]
                if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 7 * 1440:
                    errs.append("upgrades.soak_minutes: minutes from 0 to 10080")
                uo["soak_minutes"] = s
            out["upgrades"] = uo
    return out, errs


def in_window(w, now=None):
    """Is the head's local time now inside HH:MM-HH:MM (which may wrap midnight)?"""
    t = now or datetime.datetime.now()
    m, a, b = t.hour * 60 + t.minute, _hm(w[:5]), _hm(w[6:])
    return a <= m < b if a < b else m >= a or m < b


class Finding:
    """Something that is not as Noctyx expects. what: stable text (an alert label); detail: what changes from
    run to run (an error, a duration). repair: () -> (ok, detail), or None when only a person can; ok None:
    it did what it safely could, and the rest needs a person (no retry)."""

    def __init__(self, check, host, what, repair=None, how=None, detail="", why=None):
        self.check, self.host, self.what, self.repair, self.how, self.detail = check, host, what, repair, how, detail
        self.why = why                   # with no repair: why it is a person's turn
        self.key = f"{check}/{host}"


# ── the head's services ─────────────────────────────────────────────────────
def _restart(unit):
    def go():
        _run(["systemctl", "reset-failed", unit])
        rc, out = _run(["systemctl", "restart", unit], timeout=120)
        time.sleep(3)
        state = _unit(unit).get("ActiveState")
        return rc == 0 and state == "active", f"{unit} is {state}" + (f": {out[-160:]}" if rc else "")
    return go


def check_services(c, st, head):
    out = []
    names = [f"{P['unit_prefix']}-{n}" for n in LONG_RUNNING + TIMERS]
    names += ["prometheus.service", "prometheus-alertmanager.service"]
    for n in names:
        u = _unit(n)
        if u.get("LoadState") != "loaded" or u.get("UnitFileState") not in ("enabled", "enabled-runtime", "static"):
            continue                     # not installed here, or deliberately disabled
        if u.get("Type") == "oneshot":
            continue
        if u.get("ActiveState") in ("failed", "inactive"):
            out.append(Finding("service", head, f"{n} is {u['ActiveState']}", _restart(n), f"restart {n}"))
    # a service that runs but does not answer
    seen = st.setdefault("not_ready", {})
    for n, url in (("prometheus.service", upgrades.PROM), ("prometheus-alertmanager.service", upgrades.SERVICES["alertmanager"]["url"])):
        if _unit(n).get("ActiveState") != "active":
            seen.pop(n, None)
            continue
        try:
            upgrades._get(f"{url}/-/ready", timeout=5)
            seen.pop(n, None)
        except Exception as e:
            since = seen.setdefault(n, time.time())
            if time.time() - since > NOT_READY_FOR:
                out.append(Finding("ready", head, f"{n} runs but does not answer on {url}", _restart(n), f"restart {n}",
                                   f"for {int((time.time() - since) // 60)} min: {getattr(e, 'reason', e)}"))
            else:
                _note("note", f"{n} does not answer on {url} (for {int(time.time() - since)} s; restarted after "
                              f"{NOT_READY_FOR // 60} min)", head)
    return out


# ── node exporters ──────────────────────────────────────────────────────────
def _targets():
    try:
        return upgrades._api(upgrades.PROM, "/api/v1/targets", state="active")["activeTargets"]
    except Exception:
        return None


def _query(q):
    try:
        return upgrades._api(upgrades.PROM, "/api/v1/query", query=q)["result"]
    except Exception as e:
        _note("note", f"Prometheus did not answer {q}: {e}")
        return None


def _revert(c, rec, h):
    def go():
        ch = upgrades.rebuild(c, rec)
        if not ch:
            return False, "cannot rebuild the change from the ledger"
        try:
            ch.rollback()
            again = ch.verify_rollback()
        except Exception as e:
            again = [str(e)]
        change.record({**{k: rec[k] for k in ("kind", "host", "component", "what")}, "id": rec["id"] + "-undo",
                       "ts": change._utc(), "ts_epoch": time.time(), "by": "noctyx heal",
                       "outcome": "reverted" if not again else "revert_failed", "problems": again,
                       "note": f"undid change {rec['id']} of {rec['ts']}: it stopped working"})
        return not again, "back to what ran before " + rec["ts"] if not again else "; ".join(again)
    return go


def _ssh(c, h, cmd, timeout=30):
    try:
        r = _cm()._on(c, h, cmd, timeout=timeout)
        rc, out = r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        rc, out = -1, str(e)
    _note("cmd", cmd, h.get("name"), rc=rc, out=out[-1500:])
    return rc, out


def _restart_remote(c, h, unit):
    def go():
        rc, out = _ssh(c, h, f"systemctl reset-failed {unit} 2>/dev/null; systemctl restart {unit} && sleep 2 && systemctl is-active {unit}",
                       timeout=60)
        return rc == 0, out[-160:] or f"{unit} restarted"
    return go


def check_exporters(c, targets, linux, reboots):
    """Exporters Prometheus scrapes as down on the Linux machines Noctyx installed them on."""
    out = []
    for t in targets or []:
        name = t["labels"].get("host")
        inst = t["labels"].get("instance", "")
        if not inst.endswith(":9100") or t.get("health") == "up" or name not in linux or name in reboots:
            continue
        h = linux[name]
        err = (t.get("lastError") or "")[:120]
        rc, state = _ssh(c, h, f"systemctl is-active {upgrades.NE_UNIT}", timeout=20)
        if rc in (255, -1):              # the machine itself is away: nothing to repair from here
            out.append(Finding("exporter", name, "no metrics and ssh does not reach it", detail=state[-120:]))
            continue
        rec = change.last_change(name, "node_exporter")
        if rec and upgrades.NodeExporter.auto_revert:
            out.append(Finding("exporter", name, "Prometheus cannot scrape its exporter since Noctyx changed it",
                               _revert(c, rec, h), f"undo change {rec['id']} of {rec['ts']} ({rec['what']})", err))
        elif state.strip() != "active":
            out.append(Finding("exporter", name, "its exporter is not running", _restart_remote(c, h, upgrades.NE_UNIT),
                               f"restart {upgrades.NE_UNIT}", state.strip()))
        else:
            out.append(Finding("exporter", name, "its exporter runs but Prometheus cannot scrape it", detail=err))
    return out


class DropIn(change.Change):
    """Write the exporter's drop-in again (it drifted), checked; the drifted one put back if the exporter fails."""
    kind = "node_exporter-dropin"

    def __init__(self, c, host, snap=None, want=""):
        super().__init__(c, host, snap)
        self.component = self.kind       # not "node_exporter": heal's undo looks for Noctyx's upgrades of that
        self.want = want
        self.what = f"the node_exporter drop-in on {host['name']}"

    def preflight(self):
        self.snap["before"] = upgrades.scrape_exporter(self.c, self.host)

    def snapshot(self):
        rc, out = _ssh(self.c, self.host, f"cat {upgrades.NE_DROPIN} 2>/dev/null")
        self.snap["was"] = out if rc == 0 else None

    def _put(self, text):
        cmd = (f"install -d {pathlib.Path(upgrades.NE_DROPIN).parent} && printf %s {_q(text)} > {upgrades.NE_DROPIN}"
               if text is not None else f"rm -f {upgrades.NE_DROPIN}")
        rc, out = _ssh(self.c, self.host, f"{cmd} && systemctl daemon-reload && systemctl restart {upgrades.NE_UNIT}",
                       timeout=60)
        if rc:
            raise RuntimeError(out[-200:])
        time.sleep(2)

    def apply(self):
        self._put(self.want)

    def verify(self):
        s = upgrades.scrape_exporter(self.c, self.host)
        if not s["ok"]:
            return [f"scrape failed: {s['error']}"]
        return [f"a scrape takes {s['seconds']}s"] if s["seconds"] > upgrades.SCRAPE_BUDGET else []

    def rollback(self):
        self._put(self.snap.get("was"))

    def verify_before(self):
        if not (self.snap.get("before") or {}).get("ok"):
            return []
        s = upgrades.scrape_exporter(self.c, self.host)
        return [] if s["ok"] else [f"scrape failed: {s['error']}"]


def _q(s):
    return shlex.quote(s)


def _norm(text):
    return (text or "").strip()


def check_dropins(c, linux, reboots, want):
    """Every Linux machine running Noctyx's pinned exporter (the binary is there) must have Noctyx's drop-in."""
    out = []
    for name, h in linux.items():
        if name in reboots:
            continue
        rc, got = _ssh(c, h, f"[ -x /usr/local/bin/node_exporter ] || {{ echo __none__; exit 0; }}; "
                             f"cat {upgrades.NE_DROPIN} 2>/dev/null || echo __missing__", timeout=20)
        if rc or _norm(got) == "__none__":
            continue                     # unreachable (the exporter check reports that), or not Noctyx's exporter
        if _norm(got) != _norm(want):
            ch = DropIn(c, h, want=want)
            out.append(Finding("dropin", name, "the node_exporter drop-in " + ("is missing" if "__missing__" in got else
                                                                              "differs from Noctyx's"),
                               lambda ch=ch: _as_change(ch), "write it again"))
    return out


def _as_change(ch, by="noctyx heal"):
    rec = change.run(ch, log=lambda m: _note("change", m, ch.host.get("name")), by=by, locked=False)
    return rec["outcome"] == "ok", rec["outcome"] + (": " + "; ".join(rec.get("problems") or []) if rec.get("problems") else "")


# ── textfile collector ──────────────────────────────────────────────────────
def check_textfiles(c, linux, reboots):
    out = []
    fname = re.escape(P["textfile_name"]).replace("\\", "\\\\")   # a PromQL string doubles a regex's backslash
    q = f'time() - node_textfile_mtime_seconds{{file=~".*/{fname}"}}'
    res = _query(q)
    if res is None:
        return out
    unit = P["textfile_unit"]
    for r in res:
        name, age = r["metric"].get("host"), float(r["value"][1])
        if name in linux and name not in reboots and age > TEXTFILE_MAX_AGE:
            h = linux[name]

            def go(h=h):
                rc, o = _ssh(c, h, f"systemctl restart {unit}.timer && systemctl start {unit}.service", timeout=120)
                return rc == 0, o[-160:] or f"{unit} restarted"
            out.append(Finding("textfile", name, f"its GPU/Docker metrics are {int(age // 60)} min old", go,
                               f"restart {unit}.timer"))
    return out


# ── alert rules ─────────────────────────────────────────────────────────────
def rules_path():
    return pathlib.Path(P["prom_config"]).parent / f"{P['unit_prefix']}-rules.yml"


def _reload_prometheus():
    if _unit("prometheus.service").get("ActiveState") == "active":
        return _run(["systemctl", "reload", "prometheus"])
    try:
        upgrades.urllib.request.urlopen(upgrades.urllib.request.Request(f"{upgrades.PROM}/-/reload", method="POST"), timeout=10)
        return 0, ""
    except Exception as e:
        return 1, str(e)


def _loaded_rules():
    try:
        groups = upgrades._api(upgrades.PROM, "/api/v1/rules")["groups"]
    except Exception:
        return None, []
    rules = [r for g in groups if g.get("file") == str(rules_path()) for r in g["rules"]]
    return len(rules), [r["name"] for r in rules if r.get("health") == "err"]


def check_rules(c, head):
    f = rules_path()
    if not f.exists():
        return []
    cm = _cm()
    want = cm.render_site(c, (cm.NX_HOME / "metrics" / "rules.yml").read_text())
    if f.read_text() == want:
        return []

    def go():
        tmp = f.with_name(f.name + ".noctyx-new")
        prev = f.with_name(f.name + ".noctyx-prev")
        tmp.write_text(want)
        tmp.chmod(0o644)
        rc, out = _run(["promtool", "check", "rules", str(tmp)])
        if rc:
            tmp.unlink(missing_ok=True)
            return False, f"promtool rejects the new rules: {out[-200:]}"
        n = int((re.search(r"(\d+) rules found", out) or [0, 0])[1])
        prev.write_text(f.read_text())
        tmp.replace(f)
        rc, out = _reload_prometheus()
        time.sleep(5)
        loaded, bad = _loaded_rules()
        if rc or loaded != n:
            f.write_text(prev.read_text())
            _reload_prometheus()
            return False, f"Prometheus loaded {loaded} of {n} rules: put the old ones back" + (f" ({out[-120:]})" if rc else "")
        return True, f"{n} rules loaded" + (f"; failing: {', '.join(bad)}" if bad else "")
    return [Finding("rules", head, f"{f} differs from this Noctyx's rules", go, "write them again and reload")]


# ── a full root filesystem ──────────────────────────────────────────────────
ROOT_PCT = '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})'
JOURNAL_KEEP = "1G"
# what the disk playbook clears: the journal beyond JOURNAL_KEEP and apt's downloaded packages. Never Docker
# (a prune also removes stopped containers someone means to start again), never packages, kernels or models.
FREE_SPACE = (f"b=$(df -B1 --output=avail / | tail -1); journalctl --vacuum-size={JOURNAL_KEEP} -q 2>&1 | tail -2; "
              "command -v apt-get >/dev/null && apt-get clean; "
              "a=$(df -B1 --output=avail / | tail -1); echo \"freed=$((a-b)) pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)\"")
BIGGEST = "timeout 120 nice -n 19 ionice -c3 du -x -h -d 2 / 2>/dev/null | sort -rh | sed -n 2,7p"


def _gib(n):
    return f"{n / 2**30:.1f} GiB"


def check_disk(c, linux, reboots, pol, st):
    out = []
    res = _query(ROOT_PCT)
    if res is None:
        return out
    limit = pol["disk_pct"]
    for r in res:
        name, pct = r["metric"].get("host"), float(r["value"][1])
        if name not in linux or name in reboots or pct <= limit:
            continue
        h = linux[name]
        what = f"its root filesystem is over {limit}% full"
        last = next((x for x in reversed(st.get("repairs") or []) if x.get("key") == f"disk/{name}"
                     and time.time() - x.get("ts_epoch", 0) < WINDOW), None)
        if last and last.get("ok") is None:      # cleared what it safely can within 6 h: a person's turn
            out.append(Finding("disk", name, what, detail=f"{pct:.0f}% · " + last.get("detail", ""),
                               why=f"cleared what it safely can at {last.get('ts', 'its last try')}: a person's turn"))
            continue

        def go(h=h):
            rc, o = _ssh(c, h, FREE_SPACE, timeout=300)
            m = re.search(r"freed=(-?\d+) pct=(\d+)", o)
            if rc or not m:
                return False, o[-200:] or f"rc {rc}"
            freed, now = int(m[1]), int(m[2])
            if now <= limit:
                return True, f"freed {_gib(freed)}: {now}% full now"
            _, big = _ssh(c, h, BIGGEST, timeout=150)
            return None, (f"freed {_gib(freed)} (journal, apt cache), still {now}% full. Largest: "
                          + "; ".join(" ".join(l.split()) for l in big.splitlines()[:6]))
        out.append(Finding("disk", name, what, go,
                           f"vacuum the journal to {JOURNAL_KEEP} and clear apt's package cache (never Docker)",
                           f"{pct:.0f}%"))
    return out


# ── failed units ────────────────────────────────────────────────────────────
def never_restart(unit):
    return any(fnmatch.fnmatch(unit, p) for p in NEVER_UNITS)


def may_restart(unit, pol):
    return not never_restart(unit) and any(fnmatch.fnmatch(unit, p) for p in pol["restart_units"])


FAILED_UNITS = ("systemctl list-units --failed --all --plain --no-legend --full | while read -r u l rest; do "
                "echo \"U $u $l\"; journalctl -u \"$u\" -n 3 --no-pager -o cat 2>/dev/null | cut -c1-200 | sed 's/^/J /'; "
                "done")


def check_units(c, hosts_by_name, reboots, pol):
    """Failed units, asked of each machine (the exporters' systemd collector leaves out a unit whose file is
    gone), with each one's last log lines."""
    out = []
    mine = f"{P['unit_prefix']}-"
    for name, h in hosts_by_name.items():
        if name in reboots:
            continue
        rc, o = _ssh(c, h, FAILED_UNITS, timeout=30)
        if rc:
            continue                     # unreachable: the exporter check reports that
        units, cur = {}, None
        for line in o.splitlines():
            if line.startswith("U "):
                parts = line.split()
                cur = parts[1] if len(parts) > 1 else None
                if cur:
                    units[cur] = {"load": parts[2] if len(parts) > 2 else "", "log": []}
            elif line.startswith("J ") and cur in units:
                units[cur]["log"].append(line[2:].strip())
        for unit, u in units.items():
            if unit.startswith(mine):
                continue                 # Noctyx's own units: the service check owns them
            tail = " | ".join(u["log"])[-300:]
            if u["load"] == "not-found":
                def go(h=h, unit=unit):
                    rc, o = _ssh(c, h, f"systemctl reset-failed {_q(unit)}")
                    return rc == 0, "failed state cleared" if rc == 0 else o[-160:]
                out.append(Finding("units", name, f"{unit} failed and its unit file is gone", go,
                                   f"systemctl reset-failed {unit}"))
            elif may_restart(unit, pol):
                def go(h=h, unit=unit):
                    rc, o = _ssh(c, h, f"systemctl reset-failed {_q(unit)}; systemctl restart {_q(unit)}; sleep 5; "
                                       f"systemctl show -p ActiveState,Result {_q(unit)}", timeout=120)
                    ok = "ActiveState=failed" not in o and "Result=success" in o
                    return ok, " ".join(o.split())[-200:]
                out.append(Finding("units", name, f"{unit} failed", go, f"restart {unit} (in restart_units)", tail))
            else:
                why = "Noctyx never restarts it" if never_restart(unit) else "not in heal.restart_units"
                out.append(Finding("units", name, f"{unit} failed", detail=f"{why}: {tail}"))
    return out


# ── the NAS backup endpoint ─────────────────────────────────────────────────
def check_nas(c):
    cm = _cm()
    if not cm.rest_on_nas(c) or cm.rest_up(c):
        return []
    name = cm.nas_name(c)

    def go():
        ok = cm.ensure_rest(c, bounded=False)
        return ok, "the endpoint answers again" if ok else "started it, but it still does not answer"
    return [Finding("nas", name, "the NAS backup endpoint does not answer", go, "start it on the NAS (run.sh start)")]


# ── failed backups: their cause ─────────────────────────────────────────────
def _backup_records(host, profile, n=3):
    cm = _cm()
    try:
        with open(cm.AUDIT, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4 * 2**20))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        if '"backup"' not in line or host not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "backup" and e.get("host") == host and e.get("profile", profile) == profile and not e.get("dryrun"):
            out.append(e)
            if len(out) >= n:
                break
    return out


def backup_cause(rec):
    """A failed backup's audit record -> its likely cause, in words."""
    rc, err = rec.get("rc"), (rec.get("stderr") or rec.get("error") or rec.get("message") or "")
    e = err.lower()
    if rc == 255:
        return "ssh lost the connection to the host (the agent may have finished on its own)"
    if any(k in e for k in ("locked", "lock was created")):
        return "the repository is locked by another writer"
    if rc == 3 or "incomplete snapshot" in e:
        bad = [l.strip() for l in err.splitlines() if "error" in l.lower() or "permission denied" in l.lower()][:3]
        return "some files could not be read (restic exit 3)" + (": " + " | ".join(bad) if bad else "")
    if "no paths file" in e:
        return "the profile has no paths file on the host (policy-apply writes it)"
    if any(k in e for k in ("connection refused", "no route", "timeout", "certificate")):
        return "the repository endpoint did not answer: " + err.strip()[-160:]
    return (f"exit {rc}: " + err.strip()[-200:]) if err.strip() else f"exit {rc}"


def check_backups(c):
    cm = _cm()
    out = []
    try:
        rows, _ = cm._sched_plan(c, cm._sched_state())
    except Exception as e:
        _note("note", f"cannot read the schedule: {e}")
        return out
    for r in rows:
        if not r.get("failed_at"):
            continue
        recs = _backup_records(r["host"], r["profile"])
        fails = 0
        for e in recs:
            if e.get("rc") in (0, None):
                break
            fails += 1
        cause = backup_cause(recs[0]) if recs and recs[0].get("rc") not in (0, None) else "the last attempt failed"
        nxt = time.strftime("%H:%M", time.localtime(r["next"])) if r.get("next") else "the next tick"
        if fails < 2:
            _note("note", f"{r['profile']} backup failed: {cause}; the tick retries it at {nxt}", r["host"])
            continue
        out.append(Finding("backups", r["host"], f"its {r['profile']} backup failed {fails} times in a row",
                           detail=f"{cause}; the tick retries at {nxt}"))
    return out


# ── model servers (only the slots in heal.restart_slots) ────────────────────
def check_slots(c, st, pol):
    cm = _cm()
    out = []
    seen_all = st.setdefault("slots", {})
    slots = cm.recipes_cfg(c)["slots"]
    for n in pol["restart_slots"]:
        if n not in slots:
            _note("note", f"restart_slots names {n}, which is not a slot")
            continue
        s = slots[n]
        seen = seen_all.setdefault(n, {})
        cur, _, _ = cm.slot_records(c, n)
        head = cm.host_by_name(c, s["head"])
        unit = (cur or {}).get("unit") or f"{P['recipe_unit_prefix']}-{n}.service"
        lv = cm.recipe_live(c, n)
        if lv.get("ok"):
            seen.pop("down_since", None)
            exp = (cur or {}).get("models") or (cur or {}).get("expect")
            rc, state = _ssh(c, head, f"systemctl is-active {_q(unit)}", timeout=20)
            if state.strip() == "active" and (not exp or set(exp) & set(lv["models"])):
                seen.update(ok_ts=time.time(), models=lv["models"], unit=unit)
            else:
                _note("note", f"{n} answers ({', '.join(lv['models'])}) but not from its own unit {unit}: "
                              "Noctyx would not restart it", s["head"])
            continue
        if (cur or {}).get("state") == "stopped":
            continue                     # stopped through Noctyx: on purpose
        since = seen.setdefault("down_since", time.time())
        down = time.time() - since
        what = f"model server {n} (port {s['port']}) does not answer"
        if time.time() - seen.get("ok_ts", 0) > SLOT_SEEN_WITHIN or seen.get("unit") != unit:
            out.append(Finding("slots", s["head"], what, detail="Noctyx has not seen its own unit "
                               f"{unit} serve it in the last 7 days, so it does not start it"))
            continue
        if down < SLOT_DOWN_FOR:
            _note("note", f"{what} for {int(down // 60)} min: started again after {SLOT_DOWN_FOR // 60} min",
                  s["head"])
            continue
        busy = cm.spindle_holder(f"recipe-{n}").get("what")
        if busy:
            _note("note", f"{what}, but the slot is busy: {busy}", s["head"])
            continue

        def go(n=n):
            env = {**os.environ, "NOCTYX_NO_ASSIST": "1", "NOCTYX_BY": "noctyx heal"}
            try:
                r = subprocess.run([sys.executable, "-m", "noctyx.cli", "recipe-start", "--slot", n],
                                   capture_output=True, text=True, timeout=45 * 60, env=env)
                rc, o = r.returncode, (r.stdout + r.stderr).strip()
            except Exception as e:
                rc, o = -1, str(e)
            _note("cmd", f"noctyx recipe-start --slot {n}", rc=rc, out=o[-1500:])
            lv = cm.recipe_live(c, n)
            return bool(rc == 0 and lv.get("ok")), (f"serving {', '.join(lv['models'])}" if lv.get("ok")
                                                    else o.splitlines()[-1] if o else f"rc {rc}")
        out.append(Finding("slots", s["head"], what, go, f"noctyx recipe-start --slot {n}",
                           f"down {int(down // 60)} min; last served {', '.join(seen.get('models') or [])}"))
    return out


# ── automatic upgrades (heal.upgrades.auto, off by default) ─────────────────
def check_upgrades(c, st, pol, only=None):
    from noctyx import components as comp
    up = pol["upgrades"]
    if not up["auto"]:
        return []
    if not in_window(up["window"]):
        _note("note", f"automatic upgrades wait for the window {up['window']}")
        return []
    cm = _cm()
    inv = cm.versions_view()
    if time.time() - (inv.get("ts") or 0) > 2 * 86400:
        _note("note", "the last versions check is over 2 days old: no automatic upgrade until it runs again")
        return []
    failed = st.setdefault("upgrade_failed", {})
    soak = up["soak_minutes"] * 60
    todo = []
    for name, rows in (inv.get("hosts") or {}).items():
        if only and name != only:
            continue
        for r in rows:
            f = r.get("fix") or {}
            if r["component"] not in up["components"] or not f.get("change") or r["state"] not in ("behind", "upgrade"):
                continue
            pin = comp.PINS.get(r["component"], {}).get("version")
            if not pin or (r.get("installed") and not comp.newer(pin, r["installed"])):
                continue
            h = cm._view_host(c, name)
            what = f"{r['component']} {r.get('installed') or '—'} is older than Noctyx's {pin}"
            key = f"{r['component']}@{pin}"
            if key in failed:
                todo.append((2, name, Finding("upgrades", name, what, detail=f"the automatic upgrade to {pin} failed on "
                                              f"{failed[key]['host']} at {failed[key]['ts']}: not tried again "
                                              "(noctyx upgrade, by hand)")))
                continue
            if r.get("installed") and comp._vtuple(r["installed"])[0] != comp._vtuple(pin)[0] and not up["major"]:
                todo.append((2, name, Finding("upgrades", name, what, detail="a major version: upgrades.major is off")))
                continue
            last = change.last_change(None, r["component"], within=soak) if soak else None
            if last:
                _note("note", f"{what}: waits {up['soak_minutes']} min after {last['host']}'s upgrade of {last['ts']}",
                      name)
                continue

            def go(h=h, r=r, key=key):
                ch = upgrades.change_for(c, h, r)
                ok, detail = _as_change(ch, by="noctyx heal (automatic upgrade)")
                if not ok:
                    failed[key] = {"host": h["name"], "ts": change._utc(), "detail": detail}
                try:
                    cm.versions(c, h["name"])
                except Exception as e:
                    _note("note", f"versions check after the upgrade failed: {e}", h["name"])
                return ok, detail
            todo.append((int(cm.is_head(c, h) or bool(h.get("local"))), name,
                         Finding("upgrades", name, what, go, f["what"])))
    todo.sort(key=lambda t: (t[0], t[1]))         # nodes before the head; the head reports for everything else
    found, acted = [], False
    for _, _, fd in todo:
        if fd.repair and acted:
            _note("note", f"{fd.what}: one upgrade per run, the next one later", fd.host)
            continue
        acted = acted or bool(fd.repair)
        found.append(fd)
    return found


# ── the loop ────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _one_at_a_time():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        yield False
        return
    try:
        yield True
    finally:
        f.close()


def unfreeze(by=None):
    with _one_at_a_time() as mine:
        if not mine:
            return False
        st = load()
        had = st.pop("frozen", None)
        st["fail_streak"] = 0
        _save(st)
    _cm().audit("heal-unfreeze", by=by, was=(had or {}).get("why"))
    return bool(had)


def run(c, dry=False, log=print, host=None, trigger=None):
    global _LOG
    with _one_at_a_time() as mine:
        if not mine:
            log("another heal run is going: this one stops")
            return {**load(), "skipped": "another heal run was going"}
        _LOG = RunLog(trigger, host, dry)
        try:
            return _run_locked(c, dry, log, host)
        finally:
            _LOG = None


def _run_locked(c, dry, log, only):
    cm = _cm()
    st = load()
    now = time.time()
    pol = policy(c)
    rl = _LOG
    rl.rec["policy"] = {"auto": pol["auto"], "budget_per_hour": pol["budget_per_hour"],
                        "upgrades": pol["upgrades"]["auto"], "frozen": bool(st.get("frozen"))}
    if change.busy():
        log("a change is running (noctyx upgrade): no checks or repairs this time")
        rl.step("skip", "a change is running (noctyx upgrade): no checks or repairs")
        st.update(ts=now, skipped="a change was running")
        _save(st)
        rl.finish({"skipped": "a change was running"})
        return st
    head = next((h["name"] for h in cm.hosts(c) if cm.is_head(c, h)), c["server"].get("hostname") or "head")
    reboots = set(cm.reboot_pending() or {})
    for r in sorted(reboots):
        rl.step("note", "rebooting: left alone", r)
    every = {h["name"]: h for h in cm.hosts(c) if h.get("address") or h.get("local")}
    linux = {n: h for n, h in every.items() if h.get("os", "linux") == "linux" and h.get("address")}
    if only:
        linux = {n: h for n, h in linux.items() if n == only}
        every = {n: h for n, h in every.items() if n == only}
    want = (cm.NX_HOME / "metrics" / "node-exporter.conf").read_text()
    on = {k for k, m in pol["playbooks"].items() if m != "off"}
    found, ran = [], set()

    def check(name, fn, *a):
        if name not in on:
            return []
        ran.add(name)
        with rl.checking(name):
            got = fn(*a)
            for f in got:
                rl.step("found", f.what + (f" ({f.detail})" if f.detail else ""), f.host)
            return got
        return []

    head_too = not only or only == head
    if head_too:
        found += check("service", check_services, c, st, head)    # its findings: "service" and "ready"
        if "service" in ran:
            ran.add("ready")
    drift = check("dropin", check_dropins, c, linux, reboots, want)
    found += drift
    targets = _targets()
    if targets is None:
        rl.step("note", "Prometheus does not answer: the checks that read it wait")
    else:
        drifted = {f.host for f in drift}           # writing the drop-in again is that exporter's repair
        found += [f for f in check("exporter", check_exporters, c, targets, linux, reboots) if f.host not in drifted]
        found += check("textfile", check_textfiles, c, linux, reboots)
        if head_too:
            found += check("rules", check_rules, c, head)
        found += check("disk", check_disk, c, linux, reboots, pol, st)
        found += check("units", check_units, c, linux, reboots, pol)
    if head_too:
        found += check("nas", check_nas, c)
        found += check("backups", check_backups, c)
        found += check("slots", check_slots, c, st, pol)
    found += check("upgrades", check_upgrades, c, st, pol, only)
    tries = {k: [t for t in v if now - t < WINDOW] for k, v in (st.get("tries") or {}).items()}
    old = {p["key"]: p for p in st.get("problems") or []}
    repairs = st.get("repairs") or []
    problems = []
    acted_hour = sum(1 for r in repairs if now - r.get("ts_epoch", 0) < 3600)
    frozen = st.get("frozen")
    streak = st.get("fail_streak", 0)
    tally = {"found": len(found), "repaired": 0, "failed": 0, "partial": 0, "shadow": 0, "waiting": 0,
             "needs_person": 0}
    for f in found:
        mode = pol["playbooks"].get("service" if f.check == "ready" else f.check, "on")
        p = {"key": f.key, "check": f.check, "host": f.host, "what": f.what, "detail": f.detail,
             "since": old.get(f.key, {}).get("since", now), "how": f.how}
        said = f"{f.host}: {f.what}" + (f" ({f.detail})" if f.detail else "")
        n_tries = len(tries.get(f.key, []))
        # why it is not repaired now, or None: repair it
        if not f.repair:
            why, state = f.why or "no safe repair: a person's turn", "needs_person"
        elif dry:
            why, state = f"dry run: would {f.how}", "would"
        elif not pol["auto"]:
            why, state = f"report only (heal.auto is off): would {f.how}", "needs_person"
        elif mode == "shadow":
            why, state = f"shadow mode: would {f.how}", "shadow"
        elif frozen:
            why, state = f"repairs are frozen ({frozen['why']}): would {f.how}", "needs_person"
        elif n_tries >= TRIES:
            why, state = f"repaired {n_tries} times in 6 h and it came back: a person's turn", "needs_person"
        elif acted_hour >= pol["budget_per_hour"]:
            why, state = f"{acted_hour} repairs in the last hour (budget {pol['budget_per_hour']}): next run", "waiting"
        else:
            why, state = None, None
        if why is None:
            log(f"✗ {said} — repairing: {f.how}")
            rl.step("repair", f.how, f.host, check=f.check)
            t0 = time.time()
            rl.check = f.check           # the commands it runs are this playbook's
            try:
                with change._held():
                    ok, detail = f.repair()
            except change.Refused as e:
                ok, detail = False, str(e)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            finally:
                rl.check = None
            acted_hour += 1
            tries.setdefault(f.key, []).append(now)
            rep = {"ts": change._utc(), "ts_epoch": now, "key": f.key, "check": f.check, "host": f.host, "what": f.what,
                   "how": f.how, "ok": ok, "detail": detail, "seconds": round(time.time() - t0, 1),
                   "run": rl.rec["run"]}
            repairs.append(rep)
            cm.audit("heal", **{k: v for k, v in rep.items() if k != "ts_epoch"})
            res = "repaired" if ok else "partial" if ok is None else "failed"
            tally[res] += 1
            rl.step("result", detail, f.host, check=f.check, outcome=res, seconds=rep["seconds"])
            log(f"  {'✓' if ok else '·' if ok is None else '✗'} {detail}")
            if ok:
                streak = 0
                continue
            if ok is None:
                p.update(needs_person=True, tries=len(tries[f.key]), state="needs_person",
                         why=f"did what it safely could: {detail}")
                tally["needs_person"] += 1
                problems.append(p)
                continue
            streak += 1
            if streak >= pol["freeze_after"] and not frozen:
                frozen = {"ts": change._utc(), "ts_epoch": time.time(),
                          "why": f"{streak} repairs in a row failed, the last: {f.host}: {f.how}"}
                rl.rec["frozen_now"] = True
                rl.step("freeze", f"froze every repair: {frozen['why']} (noctyx heal --unfreeze)")
                cm.audit("heal-freeze", why=frozen["why"])
                log(f"✗ froze every repair: {frozen['why']}")
            why = (f"repaired {len(tries[f.key])} times in 6 h and it came back: a person's turn"
                   if len(tries[f.key]) >= TRIES else f"the repair failed ({detail}): tried again next run")
            state = "needs_person" if len(tries[f.key]) >= TRIES or frozen else "waiting"
        rl.step("decide", why, f.host, check=f.check, state=state)
        p.update(needs_person=state == "needs_person" or state == "shadow", tries=len(tries.get(f.key, [])),
                 state=state, why=why)
        if state in tally:
            tally[state] += 1
        log(f"{'✗' if p['needs_person'] else '·'} {said} — {why}")
        problems.append(p)
    if frozen:
        problems.append({"key": f"heal/{head}", "check": "heal", "host": head, "state": "needs_person",
                         "what": "self-repair is frozen", "detail": frozen["why"], "since": frozen.get("ts_epoch", now),
                         "how": "noctyx heal --unfreeze, or unfreeze on the Self-repair page", "needs_person": True,
                         "why": "too many repairs failed in a row"})
    # a run limited to one host (an alert's) keeps what the others last reported
    if only:
        problems += [p for k, p in old.items() if p["check"] != "heal" and
                     (p["host"] != only or p["check"] not in ran) and k not in {x["key"] for x in problems}]
    if not found:
        log("✓ everything Noctyx manages is as expected" + (f" on {only}" if only else ""))
    st.update(ts=now, problems=problems, repairs=repairs[-200:], tries={k: v for k, v in tries.items() if v},
              skipped=None, fail_streak=streak, frozen=frozen, last_run=rl.rec["run"])
    if not st["frozen"]:
        st.pop("frozen")
    if not dry:
        _save(st)
    rl.finish(tally)
    return st


def summary(st=None, within=86400):
    """For the console, its metrics and the brief: open problems and the repairs of the last day."""
    st = st if st is not None else load()
    now = time.time()
    return {"ts": st.get("ts"), "problems": st.get("problems") or [],
            "repairs": [r for r in st.get("repairs") or [] if now - r.get("ts_epoch", 0) < within],
            "skipped": st.get("skipped"), "frozen": st.get("frozen"), "fail_streak": st.get("fail_streak", 0),
            "actions_1h": sum(1 for r in st.get("repairs") or [] if now - r.get("ts_epoch", 0) < 3600)}
