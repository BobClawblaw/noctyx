"""The upgrades Noctyx makes as transactions (noctyx.change): node_exporter on any Linux machine, and taking
over the head's distro Prometheus and Alertmanager with Noctyx's pinned releases.

node_exporter: `noctyx tool install-node-metrics` puts the pinned binary and a unit drop-in in place of the
distro's; the snapshot keeps the binary and drop-in it replaces (on the machine itself), the verify scrapes
it the way Prometheus does (authenticated, within half the scrape timeout, every collector that answered
before still answering) and waits for Prometheus's own next scrape of it to be up.

Prometheus and Alertmanager on the head: Noctyx keeps the distro's unit, user, config, data and ARGS, and a
drop-in runs its pinned binary instead (`apt` may go on upgrading the package: the drop-in stays). The
first move from a 2.x Prometheus to 3.x runs the new release beside the old one first (a trial on a copy of
the data, scraping the same targets with the same rules, alerts sent nowhere) and switches only when the
trial matches. The switch stops the old one, copies its data aside (3.x data is not readable by 2.x, so this
copy is the way back), starts the new one on the same data, and checks: ready, the pinned version, every
target that was up is up, every rule that evaluated still does, the alert path still found, the history
before the switch still there. Anything else puts the copy and the old binary back.
"""
import datetime
import json
import os
import pathlib
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from noctyx import change, components as comp
from noctyx.paths import P

NE_UNIT = "prometheus-node-exporter"
NE_DROPIN = f"/etc/systemd/system/{NE_UNIT}.service.d/noctyx.conf"
NE_PREV = f"{P['node_lib']}/prev/node_exporter"   # one directory per change below it (older changes: its files)
KEEP = 3                      # the copies kept per machine and component, for noctyx revert: the newest changes'
PROM = "http://127.0.0.1:9090"
SHADOW_PORT = 20991                 # the trial Prometheus, loopback only, only while a trial runs
SCRAPE_BUDGET = 5.0                 # a scrape must finish in half Prometheus's 10 s default timeout


def _cm():
    from noctyx import orchestrator as cm
    return cm


def _sh(c, h, cmd, timeout=60):
    """A root shell snippet on h (locally on the head) -> (rc, output)."""
    r = _cm()._on(c, h, cmd, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def from_checkpoint(c, h, snap_id, paths, dest):
    """Put back what restic's checkpoint snapshot snap_id held at `paths` into dest (dest/<path>) on h, for a
    revert whose own copy is gone. A path the snapshot did not have is simply not there. -> [problem, …]"""
    if not re.fullmatch(r"[0-9a-f]{8,64}", snap_id or ""):
        return [f"no checkpoint snapshot to restore from ({snap_id})"]
    rc, out = _sh(c, h, f"rm -rf {shlex.quote(dest)} && mkdir -p {shlex.quote(dest)}")
    if rc:
        return [out[-200:]]
    for p in paths:
        rc, out = _sh(c, h, f"{P['agent_bin']} restore --snapshot {snap_id} --path {shlex.quote(p)} "
                            f"--target {shlex.quote(dest)}", timeout=1800)
        if rc:
            return [f"restoring {p} from snapshot {snap_id}: {out[-200:]}"]
    return []


def stream(argv, timeout):
    """Run argv, its output indented into ours as it comes; killed after `timeout` s -> rc (-9 when killed)."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    t = threading.Timer(timeout, p.kill)
    t.daemon = True
    t.start()
    try:
        for line in p.stdout:
            print(f"    {line.rstrip()}", flush=True)
        return p.wait()
    finally:
        t.cancel()
        if p.poll() is None:
            p.kill()


def _get(url, timeout=10, ctx=None, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def _api(base, path, **q):
    url = f"{base}{path}" + ("?" + urllib.parse.urlencode(q) if q else "")
    return json.loads(_get(url, timeout=20))["data"]


def _ts(s):
    """Prometheus's RFC 3339 with nanoseconds -> epoch seconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)", s or "")
    if not m:
        return 0.0
    frac = float(m.group(2) or 0)
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.datetime.fromisoformat(m.group(1) + tz).timestamp() + frac


# ── node_exporter ───────────────────────────────────────────────────────────
def _collectors(text):
    return {m.group(1): m.group(2) == "1" for m in
            re.finditer(r'^node_scrape_collector_success\{collector="([^"]+)"\} (\S+)', text, re.M)}


def scrape_exporter(c, h, timeout=10):
    """One scrape of h's node_exporter the way Prometheus makes it -> {ok, seconds, series, version,
    collectors: {name: answered}, error}."""
    cm = _cm()
    if cm.is_head(c, h):
        url, ctx, auth = "http://127.0.0.1:9100/metrics", None, None
    else:
        ctx = ssl.create_default_context(cafile=c["server"]["tls"]["ca"])
        url = f"https://{cm.addr(h)}:9100/metrics"
        try:
            auth = "prometheus:" + pathlib.Path("/etc/prometheus/node-exporter.pass").read_text().strip()
        except OSError as e:
            return {"ok": False, "error": f"no scrape password: {e}"}
    t0 = time.time()
    try:
        text = _get(url, timeout=timeout, ctx=ctx, auth=auth).decode(errors="replace")
    except Exception as e:
        return {"ok": False, "seconds": round(time.time() - t0, 2), "error": str(getattr(e, "reason", e))[:160]}
    v = re.search(r'^node_exporter_build_info\{[^}]*version="([^"]+)"', text, re.M)
    return {"ok": True, "seconds": round(time.time() - t0, 2), "series": text.count("\nnode_"),
            "version": v.group(1) if v else None, "collectors": _collectors(text)}


def prom_target(c, h, since, wait=90, base=PROM):
    """Wait for Prometheus to scrape h's node_exporter after `since` -> (health, lastError, seconds), or None
    when Prometheus has no such target (or does not answer)."""
    cm = _cm()
    head = cm.is_head(c, h)
    end = time.time() + wait
    while True:
        try:
            ts = _api(base, "/api/v1/targets", state="active")["activeTargets"]
        except Exception:
            return None
        mine = [t for t in ts if (t["labels"].get("host") == h["name"] and t["labels"].get("instance", "").endswith(":9100"))
                or (head and t["labels"].get("instance") in ("localhost:9100", "127.0.0.1:9100"))]
        if not mine:
            return None
        t = mine[0]
        if _ts(t.get("lastScrape")) > since:
            return t.get("health"), (t.get("lastError") or "")[:160], t.get("lastScrapeDuration")
        if time.time() > end:
            return "stale", f"no scrape since the change ({wait}s)", None
        time.sleep(3)


class NodeExporter(change.Change):
    """Noctyx's pinned node_exporter on one Linux machine (install-node-metrics)."""
    kind = "node_exporter"
    auto_revert = True
    checkpoint = True
    timeout = 900

    def __init__(self, c, host, snap=None):
        super().__init__(c, host, snap)
        self.pin = comp.PINS["node_exporter"]["version"]
        self.what = f"node_exporter {self.pin} on {host['name']}"

    def preflight(self):
        rc, out = _sh(self.c, self.host, "true", timeout=20)
        if rc:
            raise change.Refused(f"cannot reach {self.host['name']}: {out[-160:]}")
        self.snap["before"] = scrape_exporter(self.c, self.host)

    def snapshot(self):
        d = f"{NE_PREV}/{self.id or 'manual'}"
        rc, out = _sh(self.c, self.host, f"""set -e; d={d}; rm -rf "$d.new"; mkdir -p "$d.new"
[ -f /usr/local/bin/node_exporter ] && cp -p /usr/local/bin/node_exporter "$d.new/node_exporter" || true
[ -f {NE_DROPIN} ] && cp -p {NE_DROPIN} "$d.new/noctyx.conf" || true
rm -rf "$d"; mv "$d.new" "$d"; ls "$d"
ls -1dt {NE_PREV}/*/ 2>/dev/null | tail -n +{KEEP + 1} | xargs -r rm -rf""")
        if rc:
            raise RuntimeError(f"snapshot on {self.host['name']}: {out[-200:]}")
        self.snap.update(dir=d, had=out.split())

    def revert_target(self):
        return {"ok": True, "seconds": (self.snap.get("before") or {}).get("seconds") or 0}

    def revert_missing(self):
        d = self.snap.get("dir") or NE_PREV
        rc, out = _sh(self.c, self.host, f"[ -d {d} ] && echo kept || echo gone", timeout=20)
        if rc:
            return f"cannot reach {self.host['name']}: {out[-160:]}"
        if out.strip() == "kept":
            return None
        if self.checkpoint_snap:
            self.from_snapshot = True        # restic's checkpoint snapshot has what ran before it
            return None
        return f"its copy ({d}) is gone (Noctyx keeps the last {KEEP} per machine) and it has no checkpoint snapshot"

    def apply(self):
        self.snap["applied"] = time.time()
        rc = stream([sys.executable, "-m", "noctyx.cli", "tool", "install-node-metrics", "--host", self.host["name"]],
                    self.timeout)
        if rc:
            raise RuntimeError(f"install-node-metrics exited {rc}" if rc > 0 else f"install-node-metrics took over {self.timeout}s")

    def verify(self):
        bad = []
        before = (self.snap.get("before") or {}).get("collectors") or {}
        for i in range(2):
            s = scrape_exporter(self.c, self.host)
            if not s["ok"]:
                return [f"scrape failed after {s.get('seconds')}s: {s['error']}"]
            if s["seconds"] > SCRAPE_BUDGET:
                bad.append(f"a scrape took {s['seconds']}s (more than {SCRAPE_BUDGET}s: Prometheus would time out)")
            if i == 0:
                if s["version"] != self.pin:
                    bad.append(f"runs node_exporter {s['version']}, not {self.pin}")
                if s["series"] < 100:
                    bad.append(f"only {s['series']} node_* series")
                lost = sorted(k for k, ok in before.items() if ok and not s["collectors"].get(k, True))
                if lost:
                    bad.append("collectors that answered before fail now: " + ", ".join(lost))
            time.sleep(2)
        if bad:
            return bad
        t = prom_target(self.c, self.host, self.snap.get("applied", 0))
        if t and t[0] != "up":
            bad.append(f"Prometheus scrapes it as {t[0]}: {t[1]}")
        return bad

    def rollback(self):
        d = self.snap.get("dir") or NE_PREV
        if self.from_snapshot:
            d = f"{NE_PREV}/restic-{self.checkpoint_snap}"
            bad = from_checkpoint(self.c, self.host, self.checkpoint_snap, ["/usr/local/bin/node_exporter", NE_DROPIN],
                                  d + ".r")
            if bad:
                raise RuntimeError("; ".join(bad))
            _sh(self.c, self.host, f"""set -e; rm -rf {d}; mkdir -p {d}
[ -f {d}.r/usr/local/bin/node_exporter ] && mv {d}.r/usr/local/bin/node_exporter {d}/node_exporter || true
[ -f {d}.r{NE_DROPIN} ] && mv {d}.r{NE_DROPIN} {d}/noctyx.conf || true; rm -rf {d}.r""")
        rc, out = _sh(self.c, self.host, f"""set -e; d={d}
if [ -f "$d/node_exporter" ]; then install -m 0755 "$d/node_exporter" /usr/local/bin/node_exporter; else rm -f /usr/local/bin/node_exporter; fi
if [ -f "$d/noctyx.conf" ]; then install -m 0644 "$d/noctyx.conf" {NE_DROPIN}; else rm -f {NE_DROPIN}; fi
systemctl daemon-reload; systemctl reset-failed {NE_UNIT} 2>/dev/null || true; systemctl restart {NE_UNIT}""", timeout=90)
        if rc:
            raise RuntimeError(f"rollback on {self.host['name']}: {out[-200:]}")
        time.sleep(2)

    def verify_before(self):
        b = self.snap.get("before") or {}
        if not b.get("ok"):
            return []                 # it did not answer before either
        s = scrape_exporter(self.c, self.host)
        if not s["ok"]:
            return [f"scrape failed: {s['error']}"]
        return [f"a scrape takes {s['seconds']}s"] if s["seconds"] > max(SCRAPE_BUDGET, b.get("seconds", 0) * 2) else []


# ── the head's distro Prometheus and Alertmanager ───────────────────────────
SERVICES = {
    "prometheus": {"unit": "prometheus", "bins": ["prometheus", "promtool"], "distro": "/usr/bin/prometheus",
                   "flags": ("--config.file", "--storage.tsdb.path"), "data": "--storage.tsdb.path",
                   "url": PROM},
    "alertmanager": {"unit": "prometheus-alertmanager", "bins": ["alertmanager", "amtool"],
                     "distro": "/usr/bin/prometheus-alertmanager", "flags": ("--config.file", "--storage.path"),
                     "data": "--storage.path", "url": "http://127.0.0.1:9093"},
}


def dropin(name):
    return f"/etc/systemd/system/{SERVICES[name]['unit']}.service.d/noctyx.conf"


def _run(argv, timeout=60):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _flag_defaults(binary, names):
    """The compiled-in defaults of these flags (the distro patches its own paths in)."""
    rc, out = _run([binary, "--help"])
    got = {}
    for n in names:
        m = re.search(re.escape(n) + r'="([^"]*)"', out)
        if m:
            got[n] = m.group(1)
    return got


def _args(unit):
    """The ARGS the distro's /etc/default file passes."""
    try:
        txt = pathlib.Path(f"/etc/default/{unit}").read_text()
    except OSError:
        return ""
    m = re.search(r'^ARGS="?(.*?)"?\s*$', txt, re.M)
    return m.group(1) if m else ""


def _major(v):
    return comp._vtuple(v)[0]


class HeadService(change.Change):
    """Noctyx's pinned Prometheus or Alertmanager in the head's distro unit (a drop-in), or a newer pin there.
    Its binaries and drop-in are kept per change (the last KEEP); its data once, for the newest change only."""
    auto_revert = False          # a revert loses what was recorded since: a person decides
    checkpoint = True
    timeout = 600
    keep_data = False            # a revert that keeps today's data (its copy is another change's, or not wanted)

    def __init__(self, c, host, snap=None, name="prometheus", shadow_minutes=30):
        super().__init__(c, host, snap)
        self.kind = self.component = name
        self.spec = SERVICES[name]
        self.pin = comp.PINS[name]["version"]
        self.shadow_minutes = shadow_minutes
        self.stage = pathlib.Path(P["node_lib"]) / "staging" / f"{name}-{self.pin}"
        self.what = f"{name} {self.pin} on the head ({host['name']})"

    # what runs now
    def _flags(self):
        """{flag: value} the binary must be started with: the distro's compiled-in paths (or the ones the
        drop-in already names), minus any ARGS sets itself."""
        s = self.spec
        args = _args(s["unit"])
        cur = {}
        try:
            for m in re.finditer(r"(--[a-z.-]+)=(\S+)", pathlib.Path(dropin(self.kind)).read_text().split("ExecStart=")[-1]):
                cur[m.group(1)] = m.group(2)
        except OSError:
            pass
        flags = {n: cur.get(n) for n in s["flags"]}
        missing = [n for n, v in flags.items() if not v]
        if missing:
            flags.update(_flag_defaults(s["distro"], missing))
        return {n: v for n, v in flags.items() if v and n not in args}, args

    def _state(self, base=None):
        base = base or self.spec["url"]
        if self.kind == "prometheus":
            return prom_state(base)
        return am_state(base)

    def preflight(self):
        s = self._inspect()
        data = self.snap["data"]
        flags, args = self.snap["flags"], self.snap["args"]
        self._stage(flags, args, data)

    def prepare_revert(self):
        self._inspect(strict=False)
        if not self.keep_data:
            size = int(_run(["du", "-sb", self.snap["data"]])[1].split()[0] or 0)
            free = shutil.disk_usage(pathlib.Path(self.snap["data"]).parent).free
            if free < size * 2 + 2 ** 30:      # today's data kept apart, and the old copy put in its place
                raise change.Refused(f"not enough room beside {self.snap['data']} for two copies of it "
                                     f"({size >> 30} GiB each): {free >> 30} GiB free")
            self.snap["size"] = size
        else:
            self.snap["size"] = 0

    def revert_target(self):
        b = self.snap.get("before") or {}
        if not b.get("ready"):
            return {}                    # broken now: answering is what it must do after
        # the data it goes back to has the silences of that time, not today's
        return {k: v for k, v in b.items() if k != "silences"} if not self.keep_data else b

    def _inspect(self, strict=True):
        """What runs now: the unit, its state, flags, ARGS and data directory (self.snap). strict: refuse a unit
        that is not running and answering (a revert takes a broken one too)."""
        s = self.spec
        if not pathlib.Path("/run/systemd/system").exists():
            raise change.Refused("this head does not run systemd")
        rc, out = _run(["systemctl", "is-active", s["unit"]])
        if out != "active" and strict:
            raise change.Refused(f"{s['unit']}.service is {out or 'not installed'}: Noctyx takes over a running one only")
        before = self._state()
        if not before.get("ready") and strict:
            raise change.Refused(f"{self.kind} does not answer on {s['url']} now: {before.get('error')}")
        self.snap["before"] = before
        self.snap["was"] = before.get("version")
        flags, args = self._flags()
        if not all(n in flags or n in args for n in s["flags"]):
            raise change.Refused(f"cannot tell {s['unit']}'s {', '.join(s['flags'])} (the distro binary is gone?)")
        self.snap["flags"], self.snap["args"] = flags, args
        data = (flags.get(s["data"]) or re.search(re.escape(s["data"]) + r"=(\S+)", args).group(1)).rstrip("/")
        self.snap["data"] = data
        return s

    def _stage(self, flags, args, data):
        s = self.spec
        # the pinned release, checked against its sums, staged beside the audit trail
        try:
            from noctyx import setup
            got = setup._from_tar(setup._fetch(self.kind), s["bins"])
        except SystemExit as e:
            raise change.Refused(f"cannot fetch {self.kind} {self.pin}: {e}")
        except Exception as e:
            raise change.Refused(f"cannot fetch {self.kind} {self.pin}: {e}")
        self.stage.mkdir(parents=True, exist_ok=True)
        self.stage.chmod(0o755)
        self.stage.parent.chmod(0o755)
        for n, b in got.items():
            f = self.stage / n
            f.write_bytes(b)
            f.chmod(0o755)
        new = str(self.stage / s["bins"][0])
        # what the new release must accept: the config (and for Prometheus its rules), and every ARGS flag
        if self.kind == "prometheus":
            cfgf = flags.get("--config.file") or "/etc/prometheus/prometheus.yml"
            rc, out = _run([str(self.stage / "promtool"), "check", "config", cfgf])
        else:
            cfgf = flags.get("--config.file") or "/etc/prometheus/alertmanager.yml"
            rc, out = _run([str(self.stage / "amtool"), "check-config", cfgf])
        if rc:
            raise change.Refused(f"{self.kind} {self.pin} rejects {cfgf}: {out[-400:]}")
        self.snap["config"] = cfgf
        rc, helptxt = _run([new, "--help"])
        unknown = [f for f in re.findall(r"(--[a-z][a-z0-9.-]*)", args) if f not in helptxt]
        if unknown:
            raise change.Refused(f"{self.kind} {self.pin} does not know {', '.join(unknown)} from /etc/default/{s['unit']} ARGS")
        # room for the copy kept for the way back (and the trial's copy)
        size = int(_run(["du", "-sb", data])[1].split()[0] or 0)
        free = shutil.disk_usage(pathlib.Path(data).parent).free
        trial = self._needs_trial()
        if free < size * (3 if trial else 2) + 2 ** 30:
            raise change.Refused(f"not enough room beside {data}: {free >> 30} GiB free, its copy needs {size >> 30} GiB"
                                 + (" twice (the trial and the way back)" if trial else ""))
        self.snap["size"] = size
        if trial:
            problems = self.trial()
            if problems:
                raise change.Refused("the trial run found: " + "; ".join(problems))

    def _needs_trial(self):
        return self.kind == "prometheus" and _major(self.snap.get("was")) != _major(self.pin) and self.shadow_minutes > 0

    def trial(self):
        """The new release beside the old one, on a copy of the data, for shadow_minutes -> [problem, …]."""
        s, flags = self.spec, self.snap["flags"]
        unit = f"{P['unit_prefix']}-prometheus-trial"
        data = pathlib.Path(self.snap["data"])
        tdata = pathlib.Path(self.snap["data"] + ".noctyx-trial")       # beside the data: same disk, same owner
        cfgf = pathlib.Path(self.snap["config"])
        tcfg = cfgf.with_name(".noctyx-trial.yml")        # beside the real one: relative paths resolve the same
        base = f"http://127.0.0.1:{SHADOW_PORT}"
        print(f"· trial: {self.kind} {self.pin} beside {self.snap['was']} on 127.0.0.1:{SHADOW_PORT} for "
              f"{self.shadow_minutes} min, on a copy of {data} (alerts go nowhere)", flush=True)
        try:
            _run(["systemctl", "stop", unit])
            _run(["rm", "-rf", str(tdata)])
            rc, out = _run(["cp", "-a", "--reflink=auto", str(data), str(tdata)], timeout=1800)
            if rc:
                return [f"copying the data for the trial: {out[-200:]}"]
            tcfg.write_text(strip_alerting(cfgf.read_text()))
            shutil.copymode(cfgf, tcfg)
            os.chown(tcfg, cfgf.stat().st_uid, cfgf.stat().st_gid)
            extra = [a for a in shlex.split(self.snap["args"]) if not a.startswith(("--web.listen-address", "--config.file",
                                                                                     "--storage.tsdb.path"))]
            rc, out = _run(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "-p", "User=prometheus",
                            "-p", "Group=prometheus", str(self.stage / "prometheus"), f"--config.file={tcfg}",
                            f"--storage.tsdb.path={tdata}", f"--web.listen-address=127.0.0.1:{SHADOW_PORT}", *extra])
            if rc:
                return [f"cannot start the trial: {out[-200:]}"]
            end = time.time() + 600
            while not prom_state(base).get("ready"):
                if time.time() > end:
                    return ["the trial never became ready (10 min)"]
                time.sleep(5)
            end = time.time() + self.shadow_minutes * 60
            while True:
                left = end - time.time()
                prod, tri = prom_state(PROM), prom_state(base)
                diff = compare(prod, tri, alerting=False) if tri.get("targets") else ["discovering its targets"]
                up = sum(1 for v in tri.get("targets", {}).values() if v[0] == "up")
                print(f"  trial {max(0, round(left / 60)):>3} min left: {up}/{len(tri.get('targets', {}))} targets up, "
                      f"{len(tri.get('rules_bad', []))} rules failing{(' · ' + '; '.join(diff)) if diff else ''}", flush=True)
                if left <= 0:
                    break
                time.sleep(min(60, max(1, left)))
            problems = compare(prod, tri, alerting=False) + history_gap(self.snap["before"], base)
            ratio = series_ratio(PROM, base)
            problems += [f"{job}: the trial has {r:.0%} of the series production has" for job, r in ratio.items()
                         if not 0.9 <= r <= 1.1]
            return problems
        finally:
            _run(["systemctl", "stop", unit])
            tcfg.unlink(missing_ok=True)
            _run(["rm", "-rf", str(tdata)])

    def snapshot(self):
        s, data = self.spec, self.snap["data"]
        # a revert keeps today's data apart: the copy at .noctyx-prev is the one its rollback puts back
        prev = data + (".noctyx-prev-undo" if self.for_revert else ".noctyx-prev")
        self.snap["prev"] = prev
        self.snap["had_dropin"] = pathlib.Path(dropin(self.kind)).exists()
        root = pathlib.Path(data + ".noctyx-prev-bin")   # files at its top: the one copy older Noctyx kept
        bins = root / (self.id or "manual")
        self.snap["bins"] = str(bins)
        _run(["rm", "-rf", str(bins)])
        bins.mkdir(parents=True)
        for old in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)[:-KEEP]:
            _run(["rm", "-rf", str(old)])
        for b in s["bins"]:
            f = pathlib.Path("/usr/local/bin") / b
            if f.exists():
                _run(["cp", "-p", str(f), str(bins / b)])
        if self.snap["had_dropin"]:
            _run(["cp", "-p", dropin(self.kind), str(bins / "noctyx.conf")])
        self.snap["stopped"] = time.time()
        if self.keep_data:                       # a revert that keeps the data: the binaries and drop-in only
            self.snap["data_copy"] = None
            return
        rc, out = _run(["systemctl", "stop", s["unit"]], timeout=120)
        if rc:
            raise RuntimeError(f"stopping {s['unit']}: {out[-200:]}")
        _run(["rm", "-rf", prev])
        rc, out = _run(["cp", "-a", "--reflink=auto", data, prev], timeout=1800)
        if rc:
            raise RuntimeError(f"copying {data} aside: {out[-200:]}")
        pathlib.Path(prev + ".change").write_text(self.id or "manual")    # whose copy it is
        self.snap["data_copy"] = self.id or "manual"
        print(f"  {data} copied to {prev} ({self.snap['size'] >> 20} MiB) in {time.time() - self.snap['stopped']:.0f}s",
              flush=True)

    def apply(self):
        s = self.spec
        for b in s["bins"]:
            tmp = pathlib.Path("/usr/local/bin") / f".{b}.noctyx-new"
            tmp.write_bytes((self.stage / b).read_bytes())
            tmp.chmod(0o755)
            tmp.replace(pathlib.Path("/usr/local/bin") / b)
        d = pathlib.Path(dropin(self.kind))
        d.parent.mkdir(parents=True, exist_ok=True)
        flags = " ".join(f"{k}={v}" for k, v in self.snap["flags"].items())
        d.write_text(f"# managed by Noctyx (noctyx upgrade --component {self.kind}): Noctyx's pinned {self.kind} in place\n"
                     f"# of the distro's binary; the same unit, user, config, data and ARGS\n"
                     f"[Service]\nExecStart=\nExecStart=/usr/local/bin/{s['bins'][0]} {flags} $ARGS\n")
        d.chmod(0o644)
        _run(["systemctl", "daemon-reload"])
        rc, out = _run(["systemctl", "start", s["unit"]], timeout=120)
        if rc:
            raise RuntimeError(f"starting {s['unit']}: {out[-300:]}")

    def _wait_ready(self, budget=600):
        end = time.time() + budget
        while time.time() < end:
            st = self._state()
            if st.get("ready"):
                return st
            rc, out = _run(["systemctl", "is-active", self.spec["unit"]])
            if out == "failed":
                return {"ready": False, "error": f"{self.spec['unit']} failed: " + _run(
                    ["journalctl", "-u", self.spec["unit"], "-n", "5", "--no-pager", "-o", "cat"])[1][-400:]}
            time.sleep(3)
        return {"ready": False, "error": f"not ready after {budget}s"}

    def verify(self):
        st = self._wait_ready()
        if not st.get("ready"):
            return [f"{self.kind} is not ready: {st.get('error')}"]
        print(f"  {self.kind} {st.get('version')} ready {time.time() - self.snap['stopped']:.0f}s after the stop", flush=True)
        bad = [] if st.get("version") == self.pin else [f"runs {st.get('version')}, not {self.pin}"]
        if self.kind == "prometheus":
            time.sleep(45)               # every target scraped at least twice since the start
            bad += compare(self.snap["before"], prom_state(PROM), since=self.snap["stopped"])
            bad += history_gap(self.snap["before"], PROM)
        else:
            bad += compare_am(self.snap["before"], am_state(self.spec["url"]))
        return bad

    def revert_missing(self):
        bins = pathlib.Path(self.snap.get("bins") or self.snap.get("data", "") + ".noctyx-prev-bin")
        if not bins.is_dir():                    # empty is a copy too: nothing of Noctyx's ran there before
            if not self.checkpoint_snap:
                return (f"its copy of the binaries ({bins}) is gone (Noctyx keeps the last {KEEP}) and it has no "
                        "checkpoint snapshot")
            self.from_snapshot = True        # the binaries and drop-in from restic's checkpoint snapshot
        if self.keep_data:
            return None
        mark = pathlib.Path(self.snap.get("prev", "") + ".change")
        whose = mark.read_text().strip() if mark.exists() else (None if "data_copy" in self.snap else "legacy")
        mine = self.snap.get("data_copy", "legacy")
        if not pathlib.Path(self.snap.get("prev", "")).is_dir() or whose != mine:
            return (f"the copy of {self.kind}'s data taken before it was replaced by a later change's: undo it with "
                    "--keep-data (the binaries and drop-in go back, the data written since stays)")
        return None

    def rollback(self):
        s, data, prev = self.spec, self.snap["data"], self.snap.get("prev")
        _run(["systemctl", "stop", s["unit"]], timeout=120)
        bins = pathlib.Path(self.snap.get("bins") or data + ".noctyx-prev-bin")
        if self.from_snapshot:
            bins = pathlib.Path(data + f".noctyx-prev-bin/restic-{self.checkpoint_snap}")
            r = pathlib.Path(str(bins) + ".r")
            bad = from_checkpoint(self.c, self.host, self.checkpoint_snap,
                                  [f"/usr/local/bin/{b}" for b in s["bins"]] + [dropin(self.kind)], str(r))
            if bad:
                raise RuntimeError("; ".join(bad))
            _run(["rm", "-rf", str(bins)])
            bins.mkdir(parents=True)
            for b in s["bins"]:
                if (r / "usr/local/bin" / b).exists():
                    shutil.move(str(r / "usr/local/bin" / b), str(bins / b))
            if (r / dropin(self.kind).lstrip("/")).exists():
                shutil.move(str(r / dropin(self.kind).lstrip("/")), str(bins / "noctyx.conf"))
            _run(["rm", "-rf", str(r)])
        for b in s["bins"]:
            f = pathlib.Path("/usr/local/bin") / b
            if (bins / b).exists():
                _run(["cp", "-p", str(bins / b), str(f)])
            else:
                f.unlink(missing_ok=True)
        if (bins / "noctyx.conf").exists():
            _run(["cp", "-p", str(bins / "noctyx.conf"), dropin(self.kind)])
        else:
            pathlib.Path(dropin(self.kind)).unlink(missing_ok=True)
        if self.keep_data or self.snap.get("data_copy", "legacy") is None:
            _run(["systemctl", "daemon-reload"])
            rc, out = _run(["systemctl", "start", s["unit"]], timeout=120)
            if rc:
                raise RuntimeError(f"starting {s['unit']} again: {out[-200:]}")
            return
        failed = data + ".noctyx-failed"
        _run(["rm", "-rf", failed])
        _run(["mv", data, failed])
        rc, out = _run(["cp", "-a", "--reflink=auto", prev, data], timeout=1800)
        if rc:
            raise RuntimeError(f"restoring {data} from {prev}: {out[-200:]}")
        _run(["systemctl", "daemon-reload"])
        rc, out = _run(["systemctl", "start", s["unit"]], timeout=120)
        if rc:
            raise RuntimeError(f"starting {s['unit']} again: {out[-200:]}")
        print(f"  the data {self.kind} {self.pin} wrote is kept at {failed} for a look", flush=True)

    def verify_rollback(self):
        st = self._wait_ready()
        if not st.get("ready"):
            return [f"{self.kind} is not ready after the rollback: {st.get('error')}"]
        if self.kind == "prometheus":
            time.sleep(45)
            return compare(self.snap["before"], prom_state(PROM))
        return compare_am(self.snap["before"], am_state(self.spec["url"]))


# ── what "Prometheus works" means, measured ─────────────────────────────────
def strip_alerting(text):
    """The config without its top-level alerting: block (a trial must not notify anyone)."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\S", line):
            skip = line.startswith("alerting:")
        if not skip:
            out.append(line)
    return "".join(out)


def prom_state(base):
    """{ready, version, targets: {(job, instance): (health, error, lastScrape)}, rules_bad: [name], rules: n,
    alertmanagers: n, history: (t, value)} of a Prometheus, or {ready: False, error}."""
    try:
        _get(f"{base}/-/ready", timeout=5)
        st = {"ready": True, "version": _api(base, "/api/v1/status/buildinfo").get("version")}
        st["targets"] = {f"{t['labels'].get('job')} {t['labels'].get('instance')}":
                         (t.get("health"), (t.get("lastError") or "")[:160], _ts(t.get("lastScrape")))
                         for t in _api(base, "/api/v1/targets", state="active")["activeTargets"]}
        rules = [r for g in _api(base, "/api/v1/rules")["groups"] for r in g["rules"]]
        st["rules"] = len(rules)
        st["rules_bad"] = sorted({r["name"] for r in rules if r.get("health") == "err"})
        st["alertmanagers"] = len(_api(base, "/api/v1/alertmanagers").get("activeAlertmanagers") or [])
        t = int(time.time() // 60 * 60) - 3600       # an hour back: written before any switch
        r = _api(base, "/api/v1/query", query="count(up)", time=t)["result"]
        st["history"] = (t, r[0]["value"][1] if r else None)
        return st
    except Exception as e:
        return {"ready": False, "error": str(getattr(e, "reason", e))[:160]}


def compare(before, now, since=0, alerting=True):
    """What worked in `before` and does not in `now` -> [problem, …]."""
    if not now.get("ready"):
        return [f"not answering: {now.get('error')}"]
    bad = []
    for k, (health, _, _) in (before.get("targets") or {}).items():
        h = (now.get("targets") or {}).get(k)
        if health != "up":
            continue
        if not h:
            bad.append(f"target {k} is gone")
        elif h[0] != "up":
            bad.append(f"target {k} is {h[0]}: {h[1]}")
        elif since and h[2] < since:
            bad.append(f"target {k} not scraped since the switch")
    newly = sorted(set(now.get("rules_bad") or []) - set(before.get("rules_bad") or []))
    if newly:
        bad.append("rules that evaluated before fail now: " + ", ".join(newly))
    if (now.get("rules") or 0) < (before.get("rules") or 0):
        bad.append(f"{now.get('rules')} rules loaded, {before.get('rules')} before")
    if alerting and before.get("alertmanagers") and (now.get("alertmanagers") or 0) < before["alertmanagers"]:
        bad.append(f"sends alerts to {now.get('alertmanagers')} Alertmanager(s), {before['alertmanagers']} before")
    return bad


def history_gap(before, base):
    """The value an hour back must read the same through `base`: the old data is all there."""
    t, want = before.get("history") or (None, None)
    if t is None or want is None:
        return []
    try:
        r = _api(base, "/api/v1/query", query="count(up)", time=t)["result"]
    except Exception as e:
        return [f"cannot query the history: {e}"]
    got = r[0]["value"][1] if r else None
    return [] if got == want else [f"count(up) an hour back reads {got}, {want} before: the old data is not all there"]


def series_ratio(a, b):
    """{job: series in b / series in a}."""
    q = "count by (job) ({__name__=~\".+\"})"
    try:
        ra = {r["metric"].get("job"): float(r["value"][1]) for r in _api(a, "/api/v1/query", query=q)["result"]}
        rb = {r["metric"].get("job"): float(r["value"][1]) for r in _api(b, "/api/v1/query", query=q)["result"]}
    except Exception:
        return {}
    return {j: rb.get(j, 0) / n for j, n in ra.items() if n}


def am_state(base):
    try:
        _get(f"{base}/-/ready", timeout=5)
        st = json.loads(_get(f"{base}/api/v2/status"))
        sil = json.loads(_get(f"{base}/api/v2/silences"))
        found = prom_state(PROM).get("alertmanagers")
        return {"ready": True, "version": (st.get("versionInfo") or {}).get("version"),
                "silences": sum(1 for s in sil if (s.get("status") or {}).get("state") == "active"),
                "found_by_prometheus": found}
    except Exception as e:
        return {"ready": False, "error": str(getattr(e, "reason", e))[:160]}


def compare_am(before, now):
    if not now.get("ready"):
        return [f"not answering: {now.get('error')}"]
    bad = []
    if now.get("silences", 0) < before.get("silences", 0):
        bad.append(f"{now.get('silences')} active silences, {before.get('silences')} before")
    end = time.time() + 90
    while before.get("found_by_prometheus") and (now.get("found_by_prometheus") or 0) < before["found_by_prometheus"]:
        if time.time() > end:
            bad.append("Prometheus no longer finds it")
            break
        time.sleep(5)
        now = am_state(SERVICES["alertmanager"]["url"])
    return bad


# ── which change brings a row up to date ────────────────────────────────────
def change_for(c, host, row, shadow_minutes=30):
    """The transaction for a versions row's fix, or None when its fix is a plain command."""
    kind = (row.get("fix") or {}).get("change")
    if kind == "node_exporter":
        return NodeExporter(c, host)
    if kind in SERVICES:
        return HeadService(c, host, name=kind, shadow_minutes=shadow_minutes)
    return None


def rebuild(c, rec, fresh=False):
    """A ledger record -> its change, ready to roll back (noctyx heal, noctyx revert); fresh: a new change of the
    same kind on the same machine, with nothing kept yet."""
    snap = None if fresh else rec.get("snap")
    if rec["kind"] == "node_exporter-dropin":
        from noctyx import heal
        h = next((x for x in _cm().hosts(c) if x["name"] == rec["host"]), None)
        return heal.DropIn(c, h, snap=snap) if h else None
    if fresh:
        rec = {**rec, "snap": None}
    h = next((x for x in _cm().hosts(c) if x["name"] == rec["host"]), None)
    if not h:
        return None if rec["kind"] not in SERVICES else HeadService(c, {"name": rec["host"], "local": True},
                                                                    snap=rec.get("snap"), name=rec["kind"])
    if rec["kind"] == "node_exporter":
        return NodeExporter(c, h, snap=rec.get("snap"))
    if rec["kind"] in SERVICES:
        return HeadService(c, h, snap=rec.get("snap"), name=rec["kind"])
    return None


def revert(c, rid, keep_data=False):
    """noctyx revert: the change that undoes ledger record rid, or raise change.Refused(why)."""
    rec, why = change.revertible(rid)
    if why:
        raise change.Refused(f"change {rid}: {why}")
    old, cur = rebuild(c, rec), rebuild(c, rec, fresh=True)
    if not old or not cur:
        raise change.Refused(f"change {rid}: Noctyx cannot undo a {rec['kind']} change")
    old.keep_data = cur.keep_data = keep_data
    old.checkpoint_snap = (rec.get("checkpoint") or {}).get("snapshot")
    return change.Revert(rec, old, cur)
