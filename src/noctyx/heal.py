"""noctyx heal: the repair loop. Every few minutes (the heal timer) it compares what Noctyx manages with what it
expects, and repairs what it safely can:

  what                       broken when                                  repaired by
  head services              Noctyx's timers and services, Prometheus,    a restart
                             Alertmanager: stopped, failed, or not
                             answering for 10 minutes
  a node's exporter          Prometheus cannot scrape it                  undoing Noctyx's own change of it in the
                                                                          last day; else restarting a stopped one
  the exporter's drop-in     differs from this Noctyx's                   writing it again (a change: checked, and
                                                                          put back if the exporter then fails)
  the textfile collector     its file is over 5 minutes old               restarting its timer
  the alert rules            differ from this Noctyx's                    writing them again (promtool-checked, then
                                                                          a reload; put back if they do not load)

Each thing is repaired at most 3 times in 6 hours. After that, and for what has no safe repair, it stays a
problem that needs a person: the console shows it and NoctyxNeedsAttention alerts on it. Every repair goes
to the audit trail and to heal.json, which the console, its metrics and the morning brief read.

It never stops, restarts or changes a model server, a container or a backup, and it waits while a change
(noctyx upgrade) runs or a machine reboots.

    noctyx heal [--dry-run]       (--dry-run: report, repair nothing)
"""
import json
import pathlib
import re
import shlex
import subprocess
import time

from noctyx import change, upgrades
from noctyx.paths import P

HEAL_FILE = pathlib.Path(P["state"]) / "heal.json"
TRIES, WINDOW = 3, 6 * 3600
NOT_READY_FOR = 600               # an active service that does not answer this long is restarted
TEXTFILE_MAX_AGE = 300
LONG_RUNNING = ("console.service", "directory.service", "hf-mirror.service")
TIMERS = ("tick.timer", "nightly.timer", "retention.timer", "hygiene.timer", "heal.timer")


def _cm():
    from noctyx import orchestrator as cm
    return cm


def _run(argv, timeout=60):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)


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


class Finding:
    """Something that is not as Noctyx expects. what: stable text (an alert label); detail: what changes from
    run to run (an error, a duration). repair: () -> (ok, detail), or None when only a person can."""

    def __init__(self, check, host, what, repair=None, how=None, detail=""):
        self.check, self.host, self.what, self.repair, self.how, self.detail = check, host, what, repair, how, detail
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
    return out


# ── node exporters ──────────────────────────────────────────────────────────
def _targets():
    try:
        return upgrades._api(upgrades.PROM, "/api/v1/targets", state="active")["activeTargets"]
    except Exception:
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
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return -1, str(e)


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


def _as_change(ch):
    rec = change.run(ch, log=lambda m: None, by="noctyx heal", locked=False)
    return rec["outcome"] == "ok", rec["outcome"] + (": " + "; ".join(rec.get("problems") or []) if rec.get("problems") else "")


# ── textfile collector ──────────────────────────────────────────────────────
def check_textfiles(c, linux, reboots):
    out = []
    q = f'time() - node_textfile_mtime_seconds{{file=~".*/{re.escape(P["textfile_name"])}"}}'
    try:
        res = upgrades._api(upgrades.PROM, "/api/v1/query", query=q)["result"]
    except Exception:
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


# ── the loop ────────────────────────────────────────────────────────────────
def run(c, dry=False, log=print):
    cm = _cm()
    st = load()
    now = time.time()
    if change.busy():
        log("a change is running (noctyx upgrade): no checks or repairs this time")
        st.update(ts=now, skipped="a change was running")
        _save(st)
        return st
    head = next((h["name"] for h in cm.hosts(c) if cm.is_head(c, h)), c["server"].get("hostname") or "head")
    reboots = set(cm.reboot_pending() or {})
    linux = {h["name"]: h for h in cm.hosts(c) if h.get("os", "linux") == "linux" and h.get("address")}
    want = (cm.NX_HOME / "metrics" / "node-exporter.conf").read_text()
    targets = _targets()
    found = check_services(c, st, head)
    drift = check_dropins(c, linux, reboots, want)
    found += drift
    if targets is not None:
        drifted = {f.host for f in drift}           # writing the drop-in again is that exporter's repair
        found += [f for f in check_exporters(c, targets, linux, reboots) if f.host not in drifted]
        found += check_textfiles(c, linux, reboots) + check_rules(c, head)
    tries = {k: [t for t in v if now - t < WINDOW] for k, v in (st.get("tries") or {}).items()}
    old = {p["key"]: p for p in st.get("problems") or []}
    repairs = st.get("repairs") or []
    problems = []
    for f in found:
        p = {"key": f.key, "check": f.check, "host": f.host, "what": f.what, "detail": f.detail,
             "since": old.get(f.key, {}).get("since", now), "how": f.how}
        said = f"{f.host}: {f.what}" + (f" ({f.detail})" if f.detail else "")
        if f.repair and len(tries.get(f.key, [])) < TRIES and not dry:
            log(f"✗ {said} — repairing: {f.how}")
            try:
                with change._held():
                    ok, detail = f.repair()
            except change.Refused as e:
                ok, detail = False, str(e)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            tries.setdefault(f.key, []).append(now)
            rep = {"ts": change._utc(), "ts_epoch": now, "key": f.key, "check": f.check, "host": f.host, "what": f.what,
                   "how": f.how, "ok": ok, "detail": detail}
            repairs.append(rep)
            cm.audit("heal", **{k: v for k, v in rep.items() if k != "ts_epoch"})
            log(f"  {'✓' if ok else '✗'} {detail}")
            if ok:
                continue
        p["needs_person"] = not f.repair or len(tries.get(f.key, [])) >= TRIES
        p["tries"] = len(tries.get(f.key, []))
        if dry and f.repair:
            p["needs_person"] = False
        log(f"{'✗' if p['needs_person'] else '·'} {said}"
            + (" — needs a person" if p["needs_person"] else f" — {'would repair' if dry else 'next try'}: {f.how}"))
        problems.append(p)
    if not found:
        log("✓ everything Noctyx manages is as expected")
    st.update(ts=now, problems=problems, repairs=repairs[-100:], tries={k: v for k, v in tries.items() if v},
              skipped=None)
    if not dry:
        _save(st)
    return st


def summary(st=None, within=86400):
    """For the console, its metrics and the brief: open problems and the repairs of the last day."""
    st = st if st is not None else load()
    now = time.time()
    return {"ts": st.get("ts"), "problems": st.get("problems") or [],
            "repairs": [r for r in st.get("repairs") or [] if now - r.get("ts_epoch", 0) < within],
            "skipped": st.get("skipped")}
