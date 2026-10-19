#!/usr/bin/env python3
"""Noctyx orchestrator: the head node's half of Noctyx.

Stdlib-only. The node agents do the backing-up; this decides *when*, *in what order*, and *whether it is
safe to start at all*, deploys model recipes to GPU slots, feeds the Hugging Face mirror and keeps the
fleet's state for the console.

What a cron of `restic backup` lines cannot do:

  1. PER-SPINDLE SERIALISATION. Concurrency is a lock on the *backup target*, not on the source host:
     two writers on one SMR spindle or one small NAS collapse its throughput.
  2. A THROUGHPUT FLOOR. A job that would take 40 hours refuses to start rather than quietly run for two
     days and miss every later window.
"""
from __future__ import annotations
import argparse, base64, json, os, re, shlex, shutil, subprocess, sys, time, fcntl, datetime, pathlib, threading, urllib.parse, urllib.request

if __package__ in (None, ""):                       # run as a file (detached jobs, the console's actions)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from noctyx.paths import P, render, CONFIG           # noqa: E402

# the package directory: node-side templates (agent/), metrics/, assets/
NX_HOME = pathlib.Path(__file__).resolve().parent
CFG_PATH = CONFIG
# the directory service reads the same state dir: one path, from paths.py
STATE_DIR = pathlib.Path(P["state"])
LOCK_DIR = pathlib.Path(P["locks"])
AUDIT = pathlib.Path(P["audit"])
JOB_LOG_DIR = pathlib.Path(P["job_logs"])
UNIT = P["unit_prefix"]                              # systemd unit prefix for jobs and timers
AGENT = P["agent_bin"]
RESULT = re.escape(P["result_token"])
_RESULT_RX = re.compile(rf"^(?:{RESULT}|NOCTYX_RESULT|CLUSTERMAN_RESULT) (.*)$", re.M)


def result_lines(out):
    """The agent's machine-readable lines (its RESULT token; older agents' tokens too)."""
    return _RESULT_RX.findall(out or "")


def cfg():
    return json.load(open(CFG_PATH))


def nas_name(c):
    """Name of the NAS: its spindle key in the site config (the lock every writer to it takes) and its TLS
    name. Explicit server.nas_name, else the spindle whose mount holds the repositories, else "nas"."""
    if c["server"].get("nas_name"):
        return c["server"]["nas_name"]
    root = (c["server"].get("repo_root") or "").rstrip("/") + "/"
    best = max(((k, s["mount"].rstrip("/")) for k, s in (c.get("spindles") or {}).items()
                if isinstance(s, dict) and s.get("mount") and root.startswith(s["mount"].rstrip("/") + "/")),
               key=lambda x: len(x[1]), default=None)
    return best[0] if best else "nas"


def repo_root(c):
    """Where the live repositories are (the NAS mount)."""
    return c["server"]["repo_root"]


def repo_dir(c, repo):
    """A repository's directory on the NAS mount."""
    return f"{repo_root(c)}/{P['repo_dir_prefix']}{repo}/{repo}"


def rest_url(c):
    """Where clients reach the repository endpoint."""
    s = c["server"]
    return f"https://{s.get('rest_host') or s['address']}:{s['rest_port']}"


def rest_on_nas(c):
    s = c["server"]
    return bool(s.get("rest_host")) and s["rest_host"] != s["address"]


def nas_ssh(c, cmd, stdin=None, timeout=120):
    n = c["server"]["nas_ssh"]
    return subprocess.run(["ssh", *SSH_OPTS, "-i", c["server"]["ssh"]["key"],
                           f"{n['user']}@{n['host']}", cmd],
                          input=stdin, capture_output=True, timeout=timeout)


def rest_up(c):
    """Endpoint answers TLS with our CA. With --private-repos an anonymous GET
    of / is 401 — which is exactly 'up'."""
    import ssl, urllib.request, urllib.error
    if not os.path.exists(c["server"]["tls"]["ca"]):
        return False                     # no CA yet (a site being set up): nothing can be verified
    ctx = ssl.create_default_context(cafile=c["server"]["tls"]["ca"])
    try:
        urllib.request.urlopen(rest_url(c) + "/", context=ctx, timeout=8)
        return True
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 404)
    except Exception:
        return False


def ensure_rest(c):
    """Called by every tick and before backups: bring the NAS endpoint back
    after a NAS reboot (DSM has no boot hook we can install without the GUI)."""
    if not rest_on_nas(c) or rest_up(c):
        return True
    r = nas_ssh(c, f"sh ~/{c['server']['nas_ssh']['dir']}/run.sh start", timeout=60)
    ok = rest_up(c)
    audit("nas-rest-restart", ok=ok, out=(r.stdout or b"").decode()[-200:])
    print(f"  NAS rest-server was down: restart {'OK' if ok else 'FAILED'}")
    return ok


NAS_REST_CERT = pathlib.Path(P["nas_rest"])


def cmd_nas_rest(a):
    """deploy | start | stop | restart | status of the repository endpoint on the NAS."""
    c = cfg()
    d = c["server"]["nas_ssh"]["dir"]
    if a.action == "deploy":
        host = c["server"]["rest_host"]
        if not c["server"]["nas_ssh"].get("repo_root"):
            sys.exit("server.nas_ssh.repo_root: the NAS's own path to repo_root (e.g. /volume1/backups/noctyx)")
        if not pathlib.Path("/usr/local/bin/rest-server").exists():
            sys.exit("rest-server is not installed on the head: put the NAS's build (e.g. linux_amd64) at "
                     "/usr/local/bin/rest-server; deploy copies it from there")
        runsh = NAS_REST_CERT / "run.sh"
        runsh.write_text(render_site(c, (NX_HOME / "server" / "nas-rest-server.sh").read_text()))
        NAS_REST_CERT.mkdir(mode=0o700, exist_ok=True)
        crt, key = NAS_REST_CERT / "server.crt", NAS_REST_CERT / "server.key"
        ca_crt, ca_key = f"{P['tls']}/ca.crt", f"{P['tls']}/ca.key"
        if not crt.exists() or subprocess.run(["openssl", "x509", "-in", str(crt), "-noout",
                                               "-checkend", "2592000"]).returncode:
            ext = NAS_REST_CERT / "ext.cnf"
            ext.write_text(f"subjectAltName=IP:{host},DNS:{nas_name(c)},DNS:{nas_name(c)}.local\n"
                           "extendedKeyUsage=serverAuth\n")
            csr = NAS_REST_CERT / "server.csr"
            subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-subj", f"/O=Noctyx/CN={nas_name(c)}", "-out", str(csr)], check=True, capture_output=True)
            subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", ca_crt, "-CAkey", ca_key,
                            "-CAcreateserial", "-days", "825", "-extfile", str(ext), "-out", str(crt)],
                           check=True, capture_output=True)
            os.chmod(key, 0o600); csr.unlink(); ext.unlink()
        files = [(f"{d}/bin/rest-server", "/usr/local/bin/rest-server", "0755"),
                 (f"{d}/run.sh", str(runsh), "0755"),
                 (f"{d}/tls/server.crt", str(crt), "0644"),
                 (f"{d}/tls/server.key", str(key), "0600"),
                 (f"{d}/htpasswd", f"{P['etc']}/htpasswd", "0600")]
        nas_ssh(c, f"umask 077; mkdir -p ~/{d}/bin ~/{d}/tls && chmod 700 ~/{d}")
        for dst, src, mode in files:
            # contents over stdin — no secret ever on a command line
            r = nas_ssh(c, f"cat > ~/{dst}.new && chmod {mode} ~/{dst}.new && mv -f ~/{dst}.new ~/{dst}",
                        stdin=open(src, "rb").read(), timeout=300)
            if r.returncode:
                sys.exit(f"deploy {dst} failed: {r.stderr.decode()[-200:]}")
            print(f"  -> {nas_name(c)}:~/{dst}")
        a.action = "restart"
    if a.action in ("start", "stop", "restart", "status"):
        r = nas_ssh(c, f"sh ~/{d}/run.sh {a.action}", timeout=60)
        print((r.stdout or b"").decode().strip(), (r.stderr or b"").decode().strip())
        if a.action != "stop":
            up = rest_up(c)
            print(f"endpoint {rest_url(c)}: {'UP' if up else 'DOWN'}")
            audit("nas-rest", action=a.action, up=up)
            sys.exit(0 if up else 1)


def as_owner(argv):
    """Run argv as the repo owner. Anything root writes into a live repo is
    unreadable to rest-server (the repository owner) and wedges client backups."""
    o = P["repo_owner"]
    return ["setpriv", f"--reuid={o}", f"--regid={o}", "--clear-groups"] + argv


def hosts(cfg_, only_enabled=True):
    return [h for h in cfg_["hosts"] if (h.get("enabled", True) is not False) or not only_enabled]


def backup_hosts(cfg_):
    """Hosts Noctyx backs up: enabled, with a repo, not backup:false
    (a monitored-only host like the Mac has metrics but no repo)."""
    return [h for h in hosts(cfg_) if h.get("backup", True) is not False and h.get("repo") and h["repo"] != "windows-class"]


def host_by_name(cfg_, name):
    for h in hosts(cfg_):
        if h["name"] == name:
            return h
    sys.exit(f"unknown host: {name}")


# Keepalives: a dead link fails the ssh in ~10 min instead of hanging a job
# (and its spindle lock) forever. The agent survives the disconnect either way.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=10"]


# A host may be named rather than addressed (`"address": "mac1.local"` — a Mac on DHCP,
# dual-homed Wi-Fi + Ethernet, whose mDNS name answers with EITHER address). llm-discover
# resolves such names every 5 min, asks the box for its WIRED interface's IP (operator rule:
# always Ethernet over Wi-Fi) and records it here; every ssh/scrape uses that IP.
ADDR_CACHE = f"{P['console_state']}/addresses.json"
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def addr(h):
    a = h.get("address")
    if not a or _IPV4.match(a):
        return a
    try:
        return json.load(open(ADDR_CACHE)).get(h["name"], {}).get("ip") or a
    except Exception:
        return a


def ssh(cfg_, h, remote_cmd, timeout=86400, capture=True):
    s = cfg_["server"]["ssh"]
    cmd = ["ssh", *SSH_OPTS, "-i", s["key"],
           f"{h.get('ssh_user') or s['user']}@{addr(h)}", f"sudo -n {remote_cmd}"]
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)


def ssh_stream(cfg_, h, remote_cmd):
    """Run remote cmd streaming remote stderr to ours, live.

    Why this exists: restic's progress is carriage-return updates on stderr,
    and subprocess.run() withholds everything until the command ends — a
    40-minute backup looked dead. Pumping stderr through here (unbuffered) lets
    the UI see transfer status in real time. stdout (the agent's RESULT
    line) is still captured for parsing; both are returned for inspection.
    """
    s = cfg_["server"]["ssh"]
    cmd = ["ssh", *SSH_OPTS, "-i", s["key"],
           f"{s['user']}@{addr(h)}", f"sudo -n {remote_cmd}"]
    # stderr MUST be binary+read1: TextIOWrapper.read(n) blocks until n chars
    # or EOF, which withheld the whole stream until the job ENDED — the exact
    # "no transfer status" bug this function exists to fix (probe tests hid it
    # by being short enough to finish between reads).
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks = []

    def pump():
        for raw in iter(lambda: p.stderr.read1(65536), b""):
            chunk = raw.decode("utf-8", "replace").replace("\r", "\n")
            chunks.append(chunk)
            sys.stderr.write(chunk)
            sys.stderr.flush()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    out = p.stdout.read().decode("utf-8", "replace") if p.stdout else ""
    p.wait()
    t.join(timeout=5)
    return p.returncode, out or "", "".join(chunks)


LOCK_HINTS = ("locked by another", "repo already locked", "already locked",
              "lock was created", "unlock", "repository is locked")


def _looks_like_a_stale_lock(err):
    e = (err or "").lower()
    return any(k in e for k in LOCK_HINTS)


def audit(event, **kw):
    # UTC with an explicit offset: clients run mixed timezones, and naive local
    # stamps sorted wrongly against the agents' offset-bearing ones
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "event": event, **kw}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


class SpindleBusy(RuntimeError):
    """Raised (not sys.exit) so cmd_backup can skip ONE host rather than
    killing the whole run — sys.exit here used to abort `backup ALL` on the
    first busy spindle, which is what made 'backup everything' fail
    unpredictably (nightly 02:30 vs manual UI clicks)."""


def spindle_holder(name):
    """{what, pid, since} of whoever holds the spindle, or {} if free/unknown.
    Reads the holder's note and checks its pid — never probes the flock itself,
    which could make a real job starting that instant see a spurious 'busy'."""
    try:
        h = json.loads((LOCK_DIR / f"{name}.lock").read_text() or "{}")
        os.kill(int(h["pid"]), 0)          # note left by a killed holder: pid is gone
        return h
    except Exception:
        return {}


class spindle_lock:
    """One writer per target spindle (SMR constraint: two writers on one spindle collapse its throughput)."""

    def __init__(self, name, blocking=False, what=None):
        self.name, self.blocking, self.fh = name, blocking, None
        self.what = what or " ".join(sys.argv[1:])

    def __enter__(self):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = LOCK_DIR / f"{self.name}.lock"
        # "a+", not "w": opening with "w" truncates — a WAITER would erase the
        # holder's note before it could be read
        self.fh = open(path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            h = spindle_holder(self.name)
            who = (f" by `{h.get('what')}` since {h.get('since')} (pid {h.get('pid')})"
                   if h else "")
            raise SpindleBusy(
                f"spindle '{self.name}' is busy{who} — jobs are serialised per "
                f"spindle by design (SMR band relocations).")
        # who holds it, for the busy message above and the UI
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(json.dumps({"what": self.what, "pid": os.getpid(),
                                  "since": datetime.datetime.now(datetime.timezone.utc)
                                  .isoformat(timespec="seconds")}))
        self.fh.flush()
        return self

    def __exit__(self, *a):
        try:
            self.fh.seek(0)
            self.fh.truncate()
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# The CA lands on clients at a different path than it lives on the head node
# (install-agent.sh writes <node_etc>/tls/ca.crt). Passing the server's own
# path to a client-side curl makes the probe silently return 0 bytes/s, which the
# floor logic then reads as "network too slow" and refuses the job — a false
# negative that looks like a network problem.
CLIENT_CA = f"{P['tls']}/ca.crt"


def net_floor(cfg_, h, probe_bytes=20_000_000):
    """Measure the REAL client->server path with the REAL tooling before a job
    that could take days. Returns Mbit/s or None if unmeasurable."""
    s = cfg_["server"]
    url = f"https://{s['address']}:{s['directory_port']}/api/v1/probe?bytes={probe_bytes}"
    r = ssh(cfg_, h, f"curl -s --cacert {shlex.quote(CLIENT_CA)} -o /dev/null "
                     f"-w '%{{speed_download}}' --max-time 120 {shlex.quote(url)}", timeout=180)
    try:
        bps = float(r.stdout.strip())
    except ValueError:
        return None
    if bps <= 0:
        return None
    mbit = bps * 8 / 1e6
    _record_probe(h["name"], mbit)
    return mbit


PROBE_FILE = pathlib.Path(P["console_state"]) / "probe-last.json"


def _record_probe(name, mbit):
    """Persist one host's path reading for the UI's Network panel. Every backup
    probes its path first, so the panel stays current without its own timer;
    Console 'probe all' writes the same file. Merge, keep per-host times, write
    atomically so the UI never reads a half-written file. Best effort."""
    try:
        try:
            cur = json.loads(PROBE_FILE.read_text())
        except Exception:
            cur = {}
        now = time.time()
        mb = dict(cur.get("mbit") or {}); mb[name] = round(float(mbit), 1)
        at = dict(cur.get("at") or {}); at[name] = now
        PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROBE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "mbit": mb, "at": at}))
        os.replace(tmp, PROBE_FILE)
    except Exception:
        pass


def cmd_probe(a):
    c = cfg()
    for h in hosts(c):
        if a.host and h["name"] != a.host:
            continue
        m = net_floor(c, h)
        print(f"{h['name']:<12} {('%.0f Mbit/s' % m) if m else 'unmeasurable'}")


def cmd_hosts(a):
    c = cfg()
    print(f"{'host':<12} {'address':<16} {'arch':<8} {'repo':<14} {'boot':<16} {'lvm':<5} used")
    for h in hosts(c):
        print(f"{h['name']:<12} {str(addr(h)):<16} {h['arch']:<8} {(h.get('repo') or 'monitored'):<14} "
              f"{h.get('boot','-'):<16} {str(h.get('lvm',False)):<5} {h.get('used_gb','-')}GB")


def _live_writer_in_class(c, h):
    """Any OTHER host sharing h's repo whose agent.lock is currently held =
    a live restic writer there. Unlocking a live lock would mean two writers
    on one repository — the exact thing every lock here exists to prevent."""
    for other in hosts(c):
        if other["name"] == h["name"] or other.get("repo") != h.get("repo"):
            continue
        try:
            r = ssh(c, other, f"fuser {P['node_lib']}/agent.lock 2>/dev/null", timeout=20)
            if (r.stdout or "").strip():
                return other["name"]
        except Exception:
            continue
    return None


def _run_backup(c, h, profile, dry, force_low_net, _unlocked=False):
    net = net_floor(c, h)
    floor = c["network"].get("min_throughput_mbit", 40)
    if net is None and not force_low_net:
        print(f"  SKIP {h['name']}: could not measure the path (probe failed). Refusing to"
              f" start a possibly multi-day job blind. Check the directory service on"
              f" :{c['server']['directory_port']} and the client CA at {CLIENT_CA},"
              f" or pass --force-low-net.")
        audit("backup-refused-unmeasurable", host=h["name"])
        return False
    if net is not None and net < floor and not force_low_net:
        print(f"  SKIP {h['name']}: measured {net:.0f} Mbit/s < floor {floor} Mbit/s "
              f"(would take too long). --force-low-net to override.")
        audit("backup-refused", host=h["name"], mbit=round(net), floor=floor)
        return False

    sp = h.get("spindle", nas_name(c))
    with spindle_lock(sp, what=f"backup {h['name']}/{profile}"):
        t0 = time.time()
        print(f"  START {h['name']} profile={profile} spindle={sp} net={net and round(net)} Mbit/s")
        # streamed: restic's --progress verbose lines reach this process's stderr
        # as they happen, so the UI shows live transfer status instead of 40
        # minutes of silence.
        rc, out, err = ssh_stream(c, h, f"{AGENT} backup --profile {profile}"
                                  + (" --dry-run" if dry else ""))
        # A backup killed by an earlier disconnect/timeout leaves a restic lock
        # in the repo; the NEXT backup then fails with "repo already locked" —
        # the classic invisible cause of "snapshots randomly don't happen".
        # Unlock and retry once, but only on a lock-shaped error.
        if rc != 0 and _looks_like_a_stale_lock(err) and not _unlocked:
            live = _live_writer_in_class(c, h)
            if live:
                print(f"  SKIP {h['name']}: repo lock is HELD BY A LIVE WRITER on {live} "
                      f"(shared repo '{h['repo']}') — not stale, not clearing it. Wait for "
                      f"that backup to finish; check {live}'s Machine Report.")
                audit("backup-refused-live-lock", host=h["name"], held_by=live)
                return False
            print(f"  UNLOCK {h['name']}: repo lock error detected — clearing stale locks and retrying once")
            ssh(c, h, f"{AGENT} unlock")
            audit("backup-unlock-retry", host=h["name"])
            return _run_backup(c, h, profile, dry, force_low_net, _unlocked=True)
        # agent-level "one job per host" flock busy: a skip, not a failure —
        # the manual run that raced this one is doing the work
        if rc != 0 and ("another noctyx job" in (err or "") or "another clusterman job" in (err or "")):
            print(f"  SKIP {h['name']}: a backup is already running on that host "
                  f"(started elsewhere); counted as busy, not broken")
            audit("backup-skipped-running", host=h["name"])
            return False
        line = result_lines(out or "")
        res = json.loads(line[-1]) if line else {"event": "backup", "rc": rc,
                                                      "stderr": (err or "")[-400:]}
        dt = time.time() - t0
        res.update(seconds_wall=round(dt), host=h["name"], profile=profile, dryrun=bool(dry))
        audit(**res)
        status = "OK" if rc == 0 else "FAIL"
        print(f"  {status:5} {h['name']:<12} snapshot={res.get('snapshot','?')} "
              f"agent_secs={res.get('seconds','?')} wall={dt:.0f}s")
        if status == "FAIL":
            print("   ", (err or "")[-500:].replace("\n", " | "))
            if rc == 255:
                print(f"    NOTE: rc=255 is ssh losing the connection, not restic failing. The "
                      f"agent keeps running on {h['name']}; its outcome lands in that host's "
                      f"audit and in the repo, and the next tick will see the snapshot.")
        return status == "OK"


def _sweep_root_owned_repo_files(c):
    """Root-owned files inside a restic repo endpoint wedge rest-server (it
    runs as the repository owner, reads every lock/pack, hits EACCES, answers HTTP 500,
    and client backups hang forever retrying the lock — a real incident
    2026-09-26 13:03→21:39, orphan lock left by a killed root-run restic).
    Chown them back; never delete anything. Cheap: one find over the trees."""
    # data/ is skipped: the repos now live on NFS where a full walk costs
    # minutes, and nothing writes packs as root any more (copy runs as
    # the repository owner, head-side reads are --no-lock). locks/keys/index/snapshots
    # are small and are where a stray root file actually wedges rest-server.
    roots = {repo_root(c)}
    sel = "-path '*/data' -prune -o \\( -user root -o -group root \\)"
    for root in roots:
        try:
            subprocess.run(["sudo", "sh", "-c",
                            f"find {root} -mindepth 1 {sel} "
                            f"-exec chown -h {P['repo_owner']}:{P['repo_owner']} {{{{}}}} + 2>/dev/null; true"],
                           capture_output=True, text=True, timeout=180)
            r = subprocess.run(["sudo", "sh", "-c",
                                f"find {root} -mindepth 1 {sel} -print | wc -l"],
                               capture_output=True, text=True, timeout=60)
            if (r.stdout or "0").strip() not in ("0", ""):
                audit("repo-ownership-sweep-incomplete", root=root, remaining=r.stdout.strip())
        except Exception:
            pass


def cmd_backup(a):
    c = cfg()
    _sweep_root_owned_repo_files(c)
    targets = [host_by_name(c, a.host)] if a.host else backup_hosts(c)
    if a.host and (targets[0].get("backup", True) is False or not targets[0].get("repo")):
        sys.exit(f"{a.host} is monitored only (backup: false in the site config)")
    print(f"Noctyx backup — profile={a.profile} targets={len(targets)}")
    ok = 0
    for h in targets:
        # one repo class at a time so a shared repo never has two writers either
        try:
            if _run_backup(c, h, a.profile, a.dry_run, a.force_low_net):
                ok += 1
        except SpindleBusy as e:
            print(f"  SKIP {h['name']}: {e} (another job holds the spindle; the nightly "
                  f"or an earlier run is still going — not an error, just busy)")
            audit("backup-skipped-busy", host=h["name"])
    print(f"done: {ok}/{len(targets)} succeeded")
    sys.exit(0 if ok == len(targets) else 1)


def cmd_snapshots(a):
    c = cfg()
    for h in backup_hosts(c):
        if a.host and h["name"] != a.host:
            continue
        r = ssh(c, h, f"{AGENT} snapshots --json", timeout=300)
        try:
            snaps = json.loads("".join(l for l in r.stdout.splitlines()
                                       if not _RESULT_RX.match(l)))
        except Exception:
            print(f"{h['name']}: no readable snapshot list (agent not installed?)"); continue
        print(f"{h['name']}: {len(snaps)} snapshot(s)")
        for s in snaps[-(a.limit or 5):]:
            print(f"    {s['short_id']}  {s['time'][:19]}  {s.get('hostname','')}")


def cmd_verify(a):
    c = cfg()
    _sweep_root_owned_repo_files(c)
    subset = a.subset or (c.get("verification") or {}).get("read_data_subset", "2%")
    for h in backup_hosts(c):
        if a.host and h["name"] != a.host:
            continue
        r = ssh(c, h, f"{AGENT} verify {subset}", timeout=6 * 3600)
        line = result_lines(r.stdout or "")
        res = json.loads(line[-1]) if line else {"rc": r.returncode}
        audit(**res)
        print(f"{h['name']:<12} verify rc={res.get('rc')} (subset {subset})")


def _repo_env(repo):
    """env for a restic run as the repository owner on repo (key via environment, never argv)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    env.update(RESTIC_PASSWORD=open(f"{P['repo_keys']}/{repo}.key").read().strip(), HOME="/tmp")
    return env


def _restic_local(c, repo, args, timeout, capture=True):
    r = subprocess.run(as_owner(["restic", "-r", f"{repo_dir(c, repo)}", "--no-cache"] + args),
                       env=_repo_env(repo), capture_output=True, text=True, timeout=timeout)
    return r


def cmd_prune(a):
    """Retention on the NAS repositories. DEFAULT IS DRY-RUN; --apply deletes.

    the site config `retention` is the single source of truth. Runs on the head
    node straight against the mounted repos (no agent, no endpoint), holding
    the NAS lock so no backup runs meanwhile. --apply is gated:
    check → forget → prune → check; a failed check before means nothing is
    deleted, a failed check after is a loud audit line. keep_2nd_last_monthly
    has no restic 0.19 equivalent and is reported, not silently dropped.
    Grouped by host+tags, NOT paths: every snapshot also backs up its own
    fsmeta/<timestamp> dir, so a path grouping made each snapshot a group of
    one and nothing was ever removed.
    """
    c = cfg()
    r = c["retention"]
    flags = [f"--keep-daily={r['daily']}", f"--keep-weekly={r['weekly']}", f"--keep-monthly={r['monthly']}",
             "--group-by", "host,tags"]
    if r.get("keep_2nd_last_monthly"):
        print("NOTE: keep_2nd_last_monthly is NOT enforced — restic 0.19.1 has no equivalent "
              "flag; --keep-monthly=%d approximates the intent." % r["monthly"])
    if a.host:
        h = host_by_name(c, a.host)
        repos, host_flag = [h["repo"]], ["--host", a.host]
    else:
        repos, host_flag = [x["id"] for x in c["repositories"] if x["id"] != "windows-class"], []
    ok = 0
    for repo in repos:
        if not os.path.isdir(f"{repo_dir(c, repo)}"):
            print(f"SKIP {repo}: not initialised"); continue
        with spindle_lock(nas_name(c), blocking=True, what=f"retention {repo}{' --apply' if a.apply else ''}"):
            if a.forget:
                # explicit ids instead of the policy: the plan is whichever of them live in this repo
                plan = _restic_local(c, repo, ["snapshots", "--json"] + host_flag, 1800)
                try:
                    snaps = json.loads(plan.stdout or "[]")
                except json.JSONDecodeError:
                    snaps = []
                picked = [s for s in snaps if any(s["id"].startswith(i) for i in a.forget)]
                groups = [{"host": None, "remove": picked, "keep": [s for s in snaps if s not in picked]}]
                for s in picked:
                    s["host"] = s.get("hostname")
                fargs = [s["id"] for s in picked]
            else:
                fargs = flags + host_flag
                plan = _restic_local(c, repo, ["forget", "--dry-run", "--json"] + flags + host_flag, 1800)
                try:
                    groups = json.loads(plan.stdout or "[]")
                except json.JSONDecodeError:
                    groups = []
            if plan.returncode != 0:
                print(f"FAIL {repo}: forget --dry-run rc={plan.returncode}: {(plan.stderr or '')[-200:]}")
                audit("retention", repo=repo, apply=False, rc=plan.returncode); continue
            removes = [(g.get("host") or s.get("host"), s["short_id"], s["time"][:16])
                       for g in groups for s in (g.get("remove") or [])]
            keeps = sum(len(g.get("keep") or []) for g in groups)
            print(f"{'APPLY' if a.apply else 'DRY  '} {repo}: keep {keeps}, remove {len(removes)}"
                  + (": " + ", ".join(f"{h}/{i}@{t}" for h, i, t in removes[:12]) + (" …" if len(removes) > 12 else "") if removes else ""))
            audit("retention-plan", repo=repo, keep=keeps, remove=len(removes),
                  removed=[f"{h}/{i}" for h, i, _ in removes], apply=bool(a.apply))
            if not a.apply:
                ok += 1; continue
            if not removes:
                print(f"  nothing to remove for {repo}; skipping prune"); ok += 1; continue
            t0 = time.time()
            chk = _restic_local(c, repo, ["check"], 6 * 3600)
            if chk.returncode != 0:
                print(f"  REFUSED {repo}: check failed before forget — deleting nothing:\n    {(chk.stderr or chk.stdout)[-300:]}")
                audit("retention", repo=repo, apply=True, rc=chk.returncode, refused="check-before"); continue
            fg = _restic_local(c, repo, ["forget"] + fargs, 1800)
            pr = _restic_local(c, repo, ["prune"], 12 * 3600) if fg.returncode == 0 else None
            after = _restic_local(c, repo, ["check"], 6 * 3600) if pr is not None and pr.returncode == 0 else None
            rc = fg.returncode or (pr.returncode if pr else 1) or (after.returncode if after else 1)
            tail = [l for l in ((pr.stdout if pr else fg.stderr) or "").splitlines() if l.strip()][-4:]
            for t in tail:
                print("   ", t)
            print(f"  {'OK' if rc == 0 else 'FAIL'} {repo}: forget rc={fg.returncode} prune rc={pr.returncode if pr else '-'} "
                  f"check-after rc={after.returncode if after else '-'} wall={time.time() - t0:.0f}s")
            audit("retention", repo=repo, apply=True, rc=rc, removed=len(removes), seconds_wall=round(time.time() - t0))
            ok += 1 if rc == 0 else 0
    print(f"retention: {ok}/{len(repos)} repos {'pruned' if a.apply else 'evaluated'}")
    sys.exit(0 if ok == len(repos) else 1)


HYGIENE_FILE = pathlib.Path(P["console_state"]) / "hygiene.json"


def cmd_hygiene(a):
    """Host housekeeping across the fleet: ghost failed units, stale reboot
    flags, killed user managers, removable packages, old kernels, journal and
    docker leftovers, logrotate errors, services needing restart.

    --fix auto     fix the tier that cannot break anything (the daily timer)
    --fix manual   everything the UI offers a button for (space reclaim, package removal)
    --fix ID,ID    specific items (what a UI button sends)
    Results land in hygiene.json for the UI and one audit line per host.
    """
    c = cfg()
    targets = [host_by_name(c, a.host)] if a.host else [h for h in hosts(c) if h.get("os") != "macos" and h.get("repo") != "windows-class"]
    try:
        report = json.loads(HYGIENE_FILE.read_text())
    except Exception:
        report = {"hosts": {}}
    fixarg = f" --fix {shlex.quote(a.fix)}" if a.fix else ""
    total, fixed = 0, 0
    for h in targets:
        r = ssh(c, h, f"{AGENT} hygiene{fixarg}", timeout=900)
        line = result_lines(r.stdout or "")
        if not line:
            print(f"  {h['name']:<10} no result (rc={r.returncode}): {(r.stderr or '')[-160:].strip()}")
            report["hosts"][h["name"]] = {"ts": time.time(), "error": (r.stderr or f"rc={r.returncode}")[-200:].strip(), "items": []}
            continue
        res = json.loads(line[-1])
        items = res.get("items", [])
        report["hosts"][h["name"]] = {"ts": time.time(), "items": items}
        total += len(items); fixed += sum(1 for i in items if i.get("fixed"))
        print(f"  {h['name']:<10} {len(items)} item(s)" + (f", {sum(1 for i in items if i.get('fixed'))} fixed" if a.fix else ""))
        for i in items:
            print(f"     {'✓' if i.get('fixed') else '·'} [{i['severity']}] {i['title']}"
                  + (f" — {i['detail'][:110]}" if i.get("detail") else "") + (f"  ({i['tier']})" if i.get("tier") else "")
                  + (f"  frees {'~' if i.get('bytes_approx') else ''}{i['bytes'] / 1e9:.1f} GB" if (i.get("bytes") or 0) > 0 and not i.get("fixed") else ""))
        audit("hygiene", host=h["name"], items=len(items), fixed=sum(1 for i in items if i.get("fixed")),
              fix=a.fix or "", warnings=sum(1 for i in items if i["severity"] == "warning"))
    report["ts"] = time.time()
    free = sum(i.get("bytes") or 0 for d in report["hosts"].values() for i in d.get("items") or [] if not i.get("fixed") and (i.get("bytes") or 0) > 0)
    if free:
        print(f"reclaimable: about {free / 1e9:.1f} GB (the space fixes, if all applied)")
    try:
        HYGIENE_FILE.parent.mkdir(parents=True, exist_ok=True)
        HYGIENE_FILE.write_text(json.dumps(report, indent=1))
    except Exception as e:
        print(f"  WARN cannot write {HYGIENE_FILE}: {e}")
    print(f"hygiene: {total} item(s) across {len(targets)} host(s)" + (f", {fixed} fixed" if a.fix else ""))



# ── reboot ────────────────────────────────────────────────────────────────
# A reboot is the one action here that takes a box away. Blockers refuse it (a backup or
# restore writing, a Noctyx job aimed at the host, dpkg mid-upgrade — a reboot there is
# a recovery job, not a reboot); impact is what goes down with it, shown before the confirm.
# Remote hosts: schedule `systemctl reboot` a few seconds out (the ssh session closes
# cleanly first), wait for a NEW boot_id, then check what came back. The head node cannot
# wait for itself: it leaves a pending record, and the UI finishes the job on the new boot.
REBOOT_DIR = HYGIENE_FILE.parent / "reboots"
LLM_TOPO = pathlib.Path(P["console_state"]) / "llm-topology.json"
REBOOT_DELAY = {"remote": 10, "head": 20}     # head: time for the UI to answer and the page to show the banner
_JOB_UNIT = re.compile(rf"^({re.escape(UNIT)}-[a-z][a-z-]*?)(-[A-Za-z0-9_.]+)?-\d{{8}}T\d{{6}}Z-\d+\.service$")


def is_head(c, h):
    return addr(h) == c["server"]["address"]


def _on(c, h, cmd, timeout=30):
    """Run a root shell snippet on h: locally on the head node, else over ssh."""
    if is_head(c, h):
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return ssh(c, h, f"sh -c {shlex.quote(cmd)}", timeout=timeout)


def boot_id(c, h, timeout=15):
    try:
        r = _on(c, h, "cat /proc/sys/kernel/random/boot_id", timeout=timeout)
        v = (r.stdout or "").strip()
        return v if r.returncode == 0 and re.fullmatch(r"[0-9a-f-]{36}", v) else None
    except Exception:
        return None


# timer-driven jobs run inside these services, not as per-job units (tick runs the scheduled backups)
_TIMER_JOBS = tuple(f"{UNIT}-{j}.service" for j in ("tick", "nightly", "retention", "hygiene"))


def _active_job_units(timer_jobs=False):
    r = subprocess.run(["systemctl", "list-units", "--type=service", "--state=active,activating",
                        "--plain", "--no-legend", f"{UNIT}-*"], capture_output=True, text=True)
    names = [l.split()[0] for l in (r.stdout or "").splitlines() if l.split()]
    return [u for u in names if _JOB_UNIT.match(u) or (timer_jobs and u in _TIMER_JOBS)]


def reboot_pending(name=None):
    """Pending reboot records ({host: rec}); a record older than an hour is a waiter that died."""
    out = {}
    for f in (REBOOT_DIR.glob("*.json") if REBOOT_DIR.is_dir() else []):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        rec["stale"] = time.time() - rec.get("ts", 0) > 3600
        out[rec.get("host", f.stem)] = rec
    return out.get(name) if name else out


def _llm_on(topo, name):
    return (topo.get("hosts", {}).get(name) or {}).get("servers", [])


def llm_impact(c, h):
    """What stops serving if h goes down: its own servers and the other half of a TP pair."""
    try:
        topo = json.loads(LLM_TOPO.read_text())
    except Exception:
        return [], []
    lines, models = [], []
    for s_ in _llm_on(topo, h["name"]):
        if s_.get("role") == "serving":
            lines.append(f"serves {s_.get('model') or '?'} ({s_['engine']}, port {s_.get('port')})")
            models.append({"host": h["name"], "model": s_.get("model"), "engine": s_["engine"]})
            for wn, d in topo.get("hosts", {}).items():
                for w in d.get("servers", []):
                    if w.get("role") == "worker" and w.get("worker_for") == h["name"]:
                        lines.append(f"{wn} is its tensor-parallel worker: idle until {h['name']} is back")
        elif s_.get("role") == "worker" and s_.get("worker_for"):
            hd = s_["worker_for"]
            lines.append(f"tensor-parallel worker for {hd}: {s_.get('model') or '?'} stops on {hd} too"
                         f" (it may need both ranks restarted)")
            models.append({"host": hd, "model": s_.get("model"), "engine": s_["engine"]})
    au = urllib.parse.urlparse((c.get("assistant") or {}).get("url") or "")
    if au.hostname and any(s_.get("target") == f"{au.hostname}:{au.port}" for s_ in _llm_on(topo, h["name"])):
        lines.append("the Noctyx assistant (explain / brief) falls back to another model while it is down")
    return lines, models


def reboot_plan(c, name):
    """{host, head, allowed, blockers, warnings, impact} — the UI's confirm dialog and the
    CLI's last check before rebooting both read this."""
    h = next((x for x in hosts(c) if x["name"] == name), None)
    if not h:
        return {"host": name, "allowed": False, "blockers": [f"unknown host {name}"], "warnings": [], "impact": []}
    head = is_head(c, h)
    plan = {"host": name, "head": head, "allowed": False, "blockers": [], "warnings": [], "impact": []}
    B, W = plan["blockers"], plan["warnings"]
    if h.get("os") == "macos":
        B.append("macOS host: monitored only, Noctyx does not reboot it")
    if h.get("repo") == "windows-class" or not h.get("address"):
        B.append("no reachable address / not a managed Linux host")
    if B:
        return plan
    pend = reboot_pending(name)
    if pend and not pend.get("stale"):
        B.append(f"a reboot of {name} is already in progress (requested {pend.get('requested', '?')})")
    try:
        r = _on(c, h, f"fuser {P['node_lib']}/agent.lock 2>/dev/null; echo '|';"
                      " fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock 2>/dev/null; echo '|';"
                      " cat /proc/sys/kernel/random/boot_id", timeout=25)
        parts = (r.stdout or "").split("|")
        if r.returncode != 0 or len(parts) < 3:
            raise RuntimeError((r.stderr or f"rc={r.returncode}").strip()[-120:])
        if parts[0].strip():
            B.append(f"a backup/restore is writing on {name} right now (agent.lock held)")
        if parts[1].strip():
            B.append(f"the package manager is running on {name} (dpkg lock held): a reboot mid-upgrade can leave it unbootable")
        plan["boot_id"] = parts[2].strip()
    except Exception as e:
        B.append(f"cannot reach {name}: {str(e)[:120]}")
    units = [u for u in _active_job_units(timer_jobs=head) if not u.startswith(f"{UNIT}-reboot-")]
    mine = [u for u in units if f"-{name}-" in u]
    if mine:
        B.append(f"Noctyx job running for {name}: {', '.join(mine)}")
    if head:
        others = [u for u in units if u not in mine]
        if others:
            B.append(f"a Noctyx job is running on the head node and would be killed: {', '.join(others)}"
                     f" — try again when it ends (Runs shows it)")
        plan["impact"] += ["the Noctyx console, Prometheus and Alertmanager go down until it is back",
                           "scheduled backups pause; anything due runs on the first tick after boot",
                           "this page reconnects by itself"]
    else:
        fleet = [u for u in units if not _JOB_UNIT.match(u).group(2)]
        if fleet:
            W.append(f"a fleet-wide job is running ({', '.join(fleet)}): if it reaches {name} while it is down, "
                     f"{name} is skipped and retried on the next tick")
    lines, _ = llm_impact(c, h)
    plan["impact"] = lines + plan["impact"]
    plan["allowed"] = not B
    return plan


def _wait_llm(c, models, budget=600):
    """Rerun discovery until every model that was serving is serving again, or the budget ends."""
    if not models:
        return [], []
    t0 = time.time()
    while True:
        subprocess.run(["systemctl", "start", "llm-discover.service"], capture_output=True, timeout=300)
        try:
            topo = json.loads(LLM_TOPO.read_text())
        except Exception:
            topo = {}
        back = [m for m in models if any(s_.get("role") == "serving" and s_.get("target") and s_.get("model") == m["model"]
                                          for s_ in _llm_on(topo, m["host"]))]
        missing = [m for m in models if m not in back]
        if not missing or time.time() - t0 > budget:
            return back, missing
        print(f"  waiting for {', '.join(m['host'] + ' ' + str(m['model']) for m in missing)} "
              f"({int(time.time() - t0)}s)", flush=True)
        time.sleep(60)


def _reboot_finish(c, h, rec):
    """Everything after the host is back: LLMs, a Hygiene rescan, the audit line."""
    back, missing = _wait_llm(c, rec.get("models") or [])
    for m in back:
        print(f"  ✓ {m['host']} serving {m['model']} again")
    for m in missing:
        print(f"  ✗ {m['host']} {m['model']} NOT serving after 10 min — check its service (TP pairs may need both ranks restarted)")
    print("hygiene rescan:")
    try:
        cmd_hygiene(argparse.Namespace(host=h["name"], fix=None))
    except SystemExit:
        pass
    except Exception as e:
        print(f"  hygiene rescan failed: {e}")
    audit("reboot", host=h["name"], rc=0 if not missing else 1, requested=rec.get("requested"),
          down_s=rec.get("down_s"), back_s=rec.get("back_s"),
          llm_back=[m["model"] for m in back], llm_missing=[m["model"] for m in missing])
    try:
        (REBOOT_DIR / f"{h['name']}.json").unlink()
    except FileNotFoundError:
        pass
    print(f"reboot {h['name']}: done" + (f" — {len(missing)} model(s) not back" if missing else ""))
    if missing:
        sys.exit(1)


def cmd_reboot(a):
    """Reboot a host — refused while anything Noctyx cares about is in flight there.

    --plan     print what blocks it and what goes down with it (JSON), change nothing
    --yes      do it (re-checks the plan first); remote: waits for the new boot and checks LLMs
    --finish   the head node's post-boot half (the UI runs this when it starts on a new boot)
    """
    c = cfg()
    h = host_by_name(c, a.host)
    if a.plan:
        print(json.dumps(reboot_plan(c, h["name"]), indent=1))
        return
    if a.finish:
        rec = reboot_pending(h["name"])
        if not rec:
            sys.exit(f"no pending reboot for {h['name']}")
        now_id = boot_id(c, h)
        if now_id and now_id == rec.get("boot_id"):
            sys.exit(f"{h['name']} has not rebooted yet (same boot_id)")
        try:
            booted = float(open("/proc/stat").read().split("btime ")[1].split()[0]) if is_head(c, h) else None
        except Exception:
            booted = None
        if booted and booted >= rec["ts"]:
            rec["down_s"] = round(booted - rec["ts"])
            print(f"{h['name']} booted {datetime.datetime.fromtimestamp(booted, datetime.timezone.utc):%H:%M:%SZ}, "
                  f"{rec['down_s']}s after the reboot was requested")
        _reboot_finish(c, h, rec)
        return
    if not a.yes:
        sys.exit("reboot takes the host down; add --yes if you mean it (see --plan first)")
    plan = reboot_plan(c, h["name"])
    for w in plan["warnings"]:
        print(f"  ! {w}")
    if not plan["allowed"]:
        for b in plan["blockers"]:
            print(f"  REFUSED: {b}")
        audit("reboot-refused", host=h["name"], blockers=plan["blockers"])
        sys.exit(2)
    for line in plan["impact"]:
        print(f"  · {line}")
    _, models = llm_impact(c, h)
    head = plan["head"]
    delay = REBOOT_DELAY["head" if head else "remote"]
    rec = {"host": h["name"], "head": head, "ts": time.time(), "requested": datetime.datetime.now(
           datetime.timezone.utc).isoformat(timespec="seconds"), "boot_id": plan.get("boot_id"), "models": models}
    REBOOT_DIR.mkdir(parents=True, exist_ok=True)
    (REBOOT_DIR / f"{h['name']}.json").write_text(json.dumps(rec, indent=1))
    unit = f"{UNIT}-host-reboot-{int(time.time())}"
    r = _on(c, h, f"systemd-run --quiet --collect --unit={unit} --on-active={delay} "
                  f"--description='Noctyx: reboot requested from the console/CLI' systemctl reboot", timeout=30)
    if r.returncode != 0:
        (REBOOT_DIR / f"{h['name']}.json").unlink()
        audit("reboot", host=h["name"], rc=1, error=(r.stderr or "").strip()[-200:])
        sys.exit(f"could not schedule the reboot on {h['name']}: {(r.stderr or '').strip()[-200:]}")
    audit("reboot-requested", host=h["name"], head=head, delay_s=delay, impact=plan["impact"])
    print(f"{h['name']}: reboot in {delay}s", flush=True)
    if head:
        print("the head node cannot watch itself reboot: the UI finishes this job (LLMs, hygiene, audit) "
              "when it starts on the new boot")
        return
    # remote: down (ssh fails or boot_id changes) then up with a new boot_id
    t0, down_at, old = time.time(), None, rec["boot_id"]
    while time.time() - t0 < 1500:
        time.sleep(10)
        bid = boot_id(c, h, timeout=12)
        if bid is None:
            if down_at is None:
                down_at = time.time()
                print(f"  down after {int(down_at - t0)}s", flush=True)
            continue
        if bid != old:
            rec["down_s"] = int((down_at or time.time()) - t0)
            rec["back_s"] = int(time.time() - t0)
            print(f"  back after {rec['back_s']}s (new boot)", flush=True)
            break
    else:
        audit("reboot", host=h["name"], rc=1, error="not back with a new boot after 25 min")
        sys.exit(f"{h['name']} is not back with a new boot after 25 min — check its console/BMC")
    _reboot_finish(c, h, rec)


def cmd_restore(a):
    """Restore a path from a snapshot — never in place unless --in-place.

    --to staging  (default) head node restores into repo_root/../restores/<host>/<ts>/
                  on the NAS: browseable over NFS/SMB, nothing on the live host touched
    --to host     the host's agent restores into <node_lib>/restore/<ts>/ there
    --in-place    the host's agent restores onto / (overwrites!) — CLI only, needs --yes
    """
    c = cfg()
    h = host_by_name(c, a.host)
    if not re.fullmatch(r"[0-9a-f]{8,64}|latest", a.snapshot):
        sys.exit("bad --snapshot (hex id or 'latest')")
    if not a.path.startswith("/") or "\0" in a.path:
        sys.exit("bad --path")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t0 = time.time()
    if a.in_place:
        if not a.yes:
            sys.exit("--in-place overwrites files on the live host; add --yes if you mean it")
        rc, out, err = ssh_stream(c, h, f"{AGENT} restore --snapshot {a.snapshot} "
                                        f"--path {shlex.quote(a.path)} --target /")
        where = f"{h['name']}:/ (in place)"
    elif a.to == "host":
        rc, out, err = ssh_stream(c, h, f"{AGENT} restore --snapshot {a.snapshot} "
                                        f"--path {shlex.quote(a.path)}")
        res = result_lines(out)
        where = (json.loads(res[-1]).get("target") if res else None) or f"{h['name']}:{P['node_lib']}/restore/…"
        where = f"{h['name']}:{where}"
    else:
        repo = h["repo"]
        target = f"{os.path.dirname(repo_root(c))}/restores/{h['name']}/{stamp}"
        subprocess.run(as_owner(["mkdir", "-p", target]), check=True)
        print(f"RESTORE {h['name']} {a.snapshot} {a.path} -> {target}", flush=True)
        r = subprocess.run(as_owner(["restic", "-r", f"{repo_dir(c, repo)}", "--no-cache", "--no-lock",
                                          "restore", a.snapshot, "--host", h["name"], "--include", a.path,
                                          "--target", target]),
                           env=_repo_env(repo), capture_output=True, text=True, timeout=2 * 86400)
        rc, err = r.returncode, (r.stdout or "") + (r.stderr or "")
        for line in err.strip().splitlines()[-4:]:
            print("   ", line)
        where = target
    res = {"event": "restore", "host": h["name"], "snapshot": a.snapshot, "path": a.path,
           "to": "in-place" if a.in_place else a.to, "target": where, "rc": rc, "seconds_wall": round(time.time() - t0)}
    audit(**res)
    print(f"  {'OK' if rc == 0 else 'FAIL'} restored {a.path} from {a.snapshot} -> {where} ({res['seconds_wall']}s)")
    if rc != 0:
        print("   ", (err or "")[-400:].replace("\n", " | "))
    sys.exit(rc)


def cmd_copy(a):
    """Migrate snapshots that exist only on the RETIRED NVMe hot tier into the
    primary repositories on the NAS (repo_root). Since the migration every
    backup lands on the NAS directly; this exists to carry over history and
    becomes a no-op ("up to date") once the NVMe repos are drained.

    Runs on the head node: both repos are local paths (NVMe + NFS mount). The
    restic here runs AS the repository owner (passwords via env, read by root), so every
    file it writes into the live repo is owned by the user rest-server runs as.

    First run per repo inits the NAS repo with THE SAME key: a copy that needs
    a different key is a second backup of the data, not a copy — same key keeps
    `restic copy` incremental (blob-level, not re-encrypted).

    restic 0.19.1 `copy` has NO --dry-run; the flag is emulated honestly here:
    enumerate what would be copied by diffing snapshot IDs, touching nothing
    (NOT even init — a dry run that initialises repositories is a bug that was
    actually shipped once, caught by the very test it was meant to protect).
    """
    c = cfg()
    nas_root = repo_root(c)
    hot = c["server"].get("hot_root")
    if not hot:
        sys.exit("no retired hot tier in the site config — the migration to the NAS is complete; nothing to copy")
    if os.path.realpath(nas_root) == os.path.realpath(hot):
        sys.exit("repo_root IS the hot root — nothing to migrate")
    if not os.path.isdir(c["spindles"].get(nas_name(c), {}).get("mount", "")):
        sys.exit("NAS mount not present — check /etc/fstab (_netdev); refusing to copy "
                 "into an unmounted mountpoint and fill the root disk.")
    ok = 0
    repos = [r["id"] for r in c["repositories"]]
    for repo in repos:
        key = f"{P['repo_keys']}/{repo}.key"
        # On the head node the LOCAL hot path is authoritative — same box, no
        # TLS loop, no auth round-trips; the endpoint creds stay endpoint-only.
        src = f"{hot}/{P['repo_dir_prefix']}{repo}/{repo}"
        dst = f"{nas_root}/{P['repo_dir_prefix']}{repo}/{repo}"
        if not os.path.isdir(src):
            print(f"SKIP {repo}: source {src} not initialised"); continue
        env = dict(os.environ, RESTIC_PASSWORD_FILE=key)
        # for the restic run as the repository owner: it cannot read root's 0600 key file,
        # so root reads it and hands it over in the environment (not argv —
        # /proc/<pid>/environ is private to the owner, argv is not)
        try:
            pw = open(key).read().strip()
        except OSError as e:
            print(f"SKIP {repo}: cannot read {key}: {e}"); continue
        cenv = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        cenv.update(RESTIC_PASSWORD=pw, RESTIC_FROM_PASSWORD=pw, HOME="/tmp")

        def snap_ids(repo_path):
            r = subprocess.run(["restic", "-r", repo_path, "--password-file", key,
                                "--no-lock", "snapshots", "--json"] + ([f"--host", a.host] if a.host else []),
                               capture_output=True, text=True, env=env, timeout=600)
            try:
                # a copied snapshot gets a NEW id in the destination and keeps
                # the source id in `original` — compare on that, or every
                # snapshot looks pending forever
                return {s.get("original") or s["id"] for s in json.loads(r.stdout or "[]")}
            except json.JSONDecodeError:
                return None

        src_ids, dst_ids = snap_ids(src), (snap_ids(dst) if os.path.isdir(dst) else set())
        if src_ids is None:
            print(f"SKIP {repo}: cannot read source snapshots"); continue
        pending = src_ids - (dst_ids or set())
        if a.dry_run:
            print(f"DRY  {repo}: {len(pending)} snapshot(s) of {len(src_ids)} would copy to {dst}")
            ok += 1
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not pending:
            print(f"OK   {repo}: destination up to date ({len(src_ids)} snapshots)"); ok += 1; continue
        if not os.path.isdir(dst):
            print(f"INIT {dst} (same key as {repo})")
            r0 = subprocess.run(as_owner(["restic", "-r", dst, "--no-cache", "init"]),
                                capture_output=True, text=True, env=cenv)
            if r0.returncode != 0 and "already initialized" not in (r0.stderr or ""):
                print(f"  init failed: {(r0.stderr or '')[-200:]}"); continue
        # one writer on the NAS at a time: backups take the same spindle
        with spindle_lock(_spindle_of(c, dst), blocking=True, what=f"copy {repo}"):
            t0 = time.time()
            print(f"COPY {repo} ({len(pending)} snapshot(s)) {src} -> {dst}")
            host_args = ["--host", a.host] if a.host else []
            # `copy` takes the DESTINATION password from RESTIC_PASSWORD and the
            # SOURCE password from RESTIC_FROM_PASSWORD. Setting only the former
            # produces "an empty password is not allowed" against a perfectly
            # good source repo — the error points at the wrong repo.
            r = subprocess.run(as_owner(["restic", "-r", dst, "--no-cache", "copy",
                                              "--from-repo", src] + host_args),
                               capture_output=True, text=True, env=cenv, timeout=3 * 86400)
            dt = time.time() - t0
            res = {"event": "copy", "repo": repo, "rc": r.returncode,
                   "snapshots": len(pending), "seconds_wall": round(dt)}
            audit(**res)
            print(f"  {'OK' if r.returncode == 0 else 'FAIL'} repo={repo} wall={dt:.0f}s"
                  + ("" if r.returncode == 0 else f"\n    {(r.stderr or '')[-300:]}"))
            ok += 1 if r.returncode == 0 else 0
        _sweep_root_owned_repo_files(c)
    print(f"copy: {ok}/{len(repos)} repos done")
    sys.exit(0 if ok == len(repos) else 1)


def _spindle_of(c, path):
    """Name of the spindle whose mount holds `path` (longest mount prefix)."""
    # "_comment"-style keys hold strings, not spindles: skip them (a string
    # here crashed the migration copy on 2026-09-27)
    best = max(((n, sp["mount"]) for n, sp in c["spindles"].items()
                if isinstance(sp, dict) and "mount" in sp
                and (path == sp["mount"] or path.startswith(sp["mount"].rstrip("/") + "/"))),
               key=lambda x: len(x[1]), default=(None, ""))
    return best[0] or nas_name(c)


def cmd_capacity(a):
    c = cfg()
    print("backup target free space:")
    for path in (repo_root(c), c["server"].get("hot_root")):   # the NAS (+ a retired tier, if any)
        if not path:
            continue
        st = os.statvfs(path) if os.path.isdir(path) else None
        free = st.f_bavail * st.f_frsize / 1e9 if st else 0
        print(f"  {path:<44} free {free:8.0f} GB")
    total_src = sum(h.get("used_gb", 0) for h in hosts(c))
    print(f"  {'TOTAL SOURCE (measured used)':<44} {total_src:8.0f} GB")
    st = os.statvfs(repo_root(c)) if os.path.isdir(repo_root(c)) else None
    if st and total_src > st.f_bavail * st.f_frsize / 1e9:
        print("  WARNING: measured source exceeds free space on the NAS.")


def cmd_install_agent(a):
    """install-agent.sh with this site's paths in NX_* and the agent + dumps hook rendered for the site."""
    import tempfile
    c = cfg()
    with tempfile.TemporaryDirectory() as td:
        files = {"common": str(pathlib.Path(td) / "exclusions-common.conf")}
        pathlib.Path(files["common"]).write_text(common_exclusions(c))
        for src, name in (("noctyx-agent.sh", "agent"), ("noctyx-dumps.py", "dumps")):
            f = pathlib.Path(td) / src
            f.write_text(render((NX_HOME / "agent" / src).read_text()))
            files[name] = str(f)
        env = {**os.environ, "NOCTYX_CONFIG": CFG_PATH, "NX_AGENT_FILE": files["agent"], "NX_DUMPS_FILE": files["dumps"],
               "NX_COMMON_FILE": files["common"],
               "NX_ORCH": str(pathlib.Path(__file__).resolve()),
               **{f"NX_{k.upper()}": str(v) for k, v in P.items()}}
        for name in ([a.host] if a.host else [h["name"] for h in hosts(c)]):
            cmd = [str(NX_HOME / "agent" / "install-agent.sh"), "--host", name, "--profile", a.profile]
            if a.with_client_timer:
                cmd.append("--with-client-timer")
            print(f"$ {' '.join(cmd)}")
            subprocess.run(cmd, check=False, env=env)


def _host_macs(h):
    """Physical NIC MACs of a host from node_exporter (node_network_info), so the
    recovery menu can tell which machine it booted on. [] when unknown."""
    inst = "localhost:9100" if h["address"] == cfg()["server"]["address"] else f"{h['address']}:9100"
    q = ('node_network_info{instance="%s",device!~"lo|docker.*|veth.*|br-.*|virbr.*|tailscale.*|wg.*",'
         'address=~"([0-9a-f]{2}:){5}[0-9a-f]{2}",address!="00:00:00:00:00:00"}' % inst)
    try:
        url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": q})
        with urllib.request.urlopen(url, timeout=5) as r:
            res = json.loads(r.read())["data"]["result"]
        return sorted({m["metric"]["address"] for m in res})
    except Exception:
        return []


def cmd_directory(a):
    """Regenerate the JSON the directory service and restore image read."""
    c = cfg()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
           "server": {k: c["server"][k] for k in ("address", "rest_port", "directory_port",
                                                  "discovery_udp_port", "hostname")},
           "network": c["network"],
           "hosts": [{"name": h["name"], "repo": h["repo"], "arch": h["arch"],
                      "boot": h.get("boot"), "root_disk": h.get("root_disk"),
                      "lvm": h.get("lvm", False), "vg": h.get("vg"), "lv": h.get("lv"),
                      "restore_template": h.get("restore_template"), "address": h.get("address"),
                      "macs": _host_macs(h)}
                     for h in backup_hosts(c)]}
    try:  # Prometheus down: keep the MACs we knew rather than ship a stick without them
        old = {x["name"]: x.get("macs") for x in json.loads((STATE_DIR / "hosts.json").read_text())["hosts"]}
        for x in out["hosts"]:
            x["macs"] = x["macs"] or old.get(x["name"]) or []
    except Exception:
        pass
    (STATE_DIR / "hosts.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {STATE_DIR/'hosts.json'} ({len(out['hosts'])} hosts)")


# ── detached jobs ─────────────────────────────────────────────────────────
# Long jobs run as their own transient systemd service, never as a child of
# whoever asked for them. Before this, the UI ran the orchestrator inside its
# own cgroup (KillMode=control-group): every UI restart killed every in-flight
# backup — ssh, stream and all — leaving stale repo locks and snapshots with no
# audit record (25+ restarts on 2026-09-26; one node's 13:03 wedge and another's
# 11:59 silent seed both line up with one). The caller now only FOLLOWS the
# job's log; killing the follower never touches the job.
DETACHABLE = {"backup", "verify", "copy", "prune", "restore", "hygiene", "reboot", "hf-harvest",
              "recipe-deploy", "recipe-stop", "recipe-start", "recipe-rollback"}


def _inline():
    return bool(os.environ.get("NOCTYX_DETACHED") or os.environ.get("NOCTYX_FOREGROUND"))


def detach(a):
    if os.geteuid() != 0:
        sys.exit("noctyx must run as root (ssh key, locks, systemd-run) — use sudo")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tgt = getattr(a, "host", None) or getattr(a, "slot", None)
    target = f"-{tgt}" if tgt else ""
    unit = f"{UNIT}-{a.cmd}{target}-{stamp}-{os.getpid()}"
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = JOB_LOG_DIR / f"{unit}.log"
    env = [f"--setenv={k}={os.environ[k]}" for k in
           ("NOCTYX_CONFIG",) + tuple(f"NOCTYX_PATH_{x.upper()}" for x in P) if k in os.environ]
    cmd = ["systemd-run", "--quiet", "--collect", f"--unit={unit}",
           "--property=Type=exec",
           f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
           "--property=Nice=10", "--property=IOSchedulingClass=best-effort",
           "--setenv=NOCTYX_DETACHED=1", "--setenv=HOME=/root", *env,
           sys.executable, "-u", os.path.realpath(__file__), *sys.argv[1:]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"could not start {unit}: {(r.stderr or '').strip()}")
    print(f"DETACHED unit={unit} log={log}", flush=True)
    audit("job-detached", unit=unit, argv=sys.argv[1:])
    if a.no_follow:
        sys.exit(0)
    sys.exit(follow(unit, log))


def _unit_running(unit):
    r = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", unit],
                       capture_output=True, text=True)
    return (r.stdout or "").strip() in ("active", "activating", "deactivating", "reloading")


def follow(unit, log, poll=2.0):
    """Stream a detached job's log until its unit ends; return the job's rc
    (from its NOCTYX_EXIT line). State is checked BEFORE each read so the
    final read always drains everything the job wrote."""
    pos, tail = 0, ""
    while True:
        running = _unit_running(unit)
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            pos += len(chunk)
            text = chunk.decode("utf-8", "replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            tail = (tail + text)[-8192:]
        if not running:
            break
        time.sleep(poll)
    m = re.findall(r"^(?:NOCTYX|CLUSTERMAN)_EXIT (-?\d+)$", tail, re.M)
    if m:
        return int(m[-1])
    print(f"follow: {unit} ended without an exit record (killed?) — see {log}")
    return 125


def cmd_follow(a):
    sys.exit(follow(a.unit, a.log))


# ── schedule ──────────────────────────────────────────────────────────────
# "How often" is policy in the site config `schedule`, evaluated every tick
# (the tick timer): a host/profile is DUE when its newest committed
# snapshot in the hot repo is older than `every`, inside `window`. Driving off
# the repo — not the audit log, not a fixed time — is what makes this
# self-healing: a skipped (busy), failed or killed run is simply still due on
# the next tick, and a snapshot that landed without an audit record still counts.
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_DUR_MULT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r} (use e.g. 30m, 6h, 1d)")
    return float(m.group(1)) * _DUR_MULT[m.group(2)]


def fmt_duration(s):
    s = int(max(0, s))
    return f"{s // 86400}d{s % 86400 // 3600:02d}h" if s >= 86400 else \
           f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def in_window(local_dt, window):
    """window 'HH:MM-HH:MM' in head-node local time; may wrap midnight."""
    if not window:
        return True
    a, b = [datetime.time.fromisoformat(x.strip()) for x in window.split("-")]
    t = local_dt.time()
    return a <= t < b if a <= b else (t >= a or t < b)


def effective_schedule(c, host):
    """defaults merged with hosts.<host>; a profile set to null is disabled."""
    sch = c.get("schedule") or {}
    out = {p: dict(r) for p, r in (sch.get("defaults") or {}).items() if r}
    for p, r in ((sch.get("hosts") or {}).get(host) or {}).items():
        if r is None:
            out.pop(p, None)
        else:
            out[p] = {**out.get(p, {}), **r}
    return {p: {"every": parse_duration(r["every"]), "window": r.get("window")}
            for p, r in out.items() if not p.startswith("_")}


def due(now, local_dt, last, every, window, failed_at=None, retry=3600, slack=0):
    """Pure decision: (is_due, reason). `last` = newest snapshot epoch or None;
    `failed_at` = epoch of the last FAILED attempt newer than `last`."""
    if not in_window(local_dt, window):
        return False, f"outside window {window}"
    if failed_at and now - failed_at < retry:
        return False, f"failed {fmt_duration(now - failed_at)} ago, retry in {fmt_duration(retry - (now - failed_at))}"
    if last is None:
        return True, "never backed up"
    age = now - last
    if window and every >= 86400:
        # calendar semantics: "every 1d in 01:00-07:00" means once per night,
        # not "24h after whenever the last one happened to run" — a daytime
        # manual run must not push the nightly out by a whole extra night
        cutoff = _window_start(local_dt, window).timestamp() - (every - 86400)
        if last < cutoff:
            return True, f"no snapshot this window (last {fmt_duration(age)} ago)"
        return False, "already done this window"
    if age + slack >= every:
        return True, f"age {fmt_duration(age)} ≥ every {fmt_duration(every)}"
    return False, f"next in {fmt_duration(every - age - slack)}"


def _window_start(local_dt, window):
    """Most recent opening of `window` at or before local_dt."""
    a = datetime.time.fromisoformat(window.split("-")[0].strip())
    start = local_dt.replace(hour=a.hour, minute=a.minute, second=0, microsecond=0)
    return start if start <= local_dt else start - datetime.timedelta(days=1)


def next_due(now, last, every, window, failed_at=None, retry=3600, slack=0):
    """Earliest epoch at which due() turns true (ignoring the tick grid)."""
    earliest = max(now, (failed_at + retry) if failed_at else now)
    if window and every >= 86400:
        span = 86400 - window_gap(window)
        w = _window_start(datetime.datetime.fromtimestamp(earliest), window)
        for _ in range(400):
            wt = w.timestamp()
            if (last is None or last < wt - (every - 86400)) and earliest < wt + span:
                return max(wt, earliest)
            w += datetime.timedelta(days=1)
        return float("inf")
    t = earliest if last is None else max(earliest, last + every - slack)
    if window:
        dt = datetime.datetime.fromtimestamp(t)
        if not in_window(dt, window):
            a = datetime.time.fromisoformat(window.split("-")[0].strip())
            start = dt.replace(hour=a.hour, minute=a.minute, second=0, microsecond=0)
            if start <= dt:
                start += datetime.timedelta(days=1)
            t = start.timestamp()
    return t


def window_gap(window):
    """Seconds per day OUTSIDE the window — how late a due backup can be made
    to wait by the window alone, which is not a fault."""
    if not window:
        return 0
    a, b = [datetime.time.fromisoformat(x.strip()) for x in window.split("-")]
    mins = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    return 86400 - (mins % 1440) * 60


def health(now, last, every, window, slack=0):
    """ok | late | stale | never — RPO against the schedule, not a raw age."""
    if last is None:
        return "never"
    age = now - last
    grace = window_gap(window) + slack
    return "ok" if age <= every + grace else "late" if age <= 2 * every + grace else "stale"


def _restic_ts(t):
    # restic prints nanoseconds + offset; fromisoformat takes microseconds
    t = re.sub(r"(\.\d{6})\d+", r"\1", t).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(t).timestamp()


def local_repo_snapshots(c, repo_id):
    """Read-only listing of a hot repo straight off the head node's disk.
    --no-lock is load-bearing: a locking read run as root drops a ROOT-owned
    lock file into a repo that rest-server (the repository owner) must read — the
    EACCES/HTTP-500 wedge _sweep_root_owned_repo_files() exists to clean up."""
    key = f"{P['repo_keys']}/{repo_id}.key"
    path = f"{repo_dir(c, repo_id)}"
    r = subprocess.run(["restic", "-r", path, "--password-file", key, "--no-lock", "--no-cache",
                        "snapshots", "--json"], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"{repo_id}: {(r.stderr or '').strip()[-200:]}")
    return json.loads(r.stdout or "[]")


def last_success(c):
    """{(host, profile): epoch of newest committed snapshot}. restic only
    commits a snapshot at the end of a run, so existence == success."""
    out, errors = {}, []
    for repo in c["repositories"]:
        if repo["id"] == "windows-class":
            continue
        try:
            snaps = local_repo_snapshots(c, repo["id"])
        except Exception as e:
            # always "<repo id>: <reason>" — _sched_plan keys on the prefix
            msg = str(e)
            errors.append(msg if msg.startswith(repo["id"] + ":") else f"{repo['id']}: {msg}")
            continue
        for s in snaps:
            profs = [t[8:] for t in (s.get("tags") or []) if t.startswith("profile-")]
            for p in profs:
                k = (s.get("hostname"), p)
                out[k] = max(out.get(k, 0), _restic_ts(s["time"]))
    return out, errors


_EVERY_RE = re.compile(r"^\d{1,4}[mhdw]$")
_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def _dump_compact_leaves(v, indent):
    """json with flat objects on one line ({ "every": "24h", "window": … }),
    matching the hand-written style so edits make one-line diffs."""
    if isinstance(v, dict) and v and any(isinstance(x, (dict, list)) for x in v.values()):
        pad = " " * (indent + 2)
        items = [f"{pad}{json.dumps(k)}: {_dump_compact_leaves(x, indent + 2)}" for k, x in v.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    if isinstance(v, dict) and v:
        return "{ " + ", ".join(f"{json.dumps(k)}: {json.dumps(x, ensure_ascii=False)}"
                                for k, x in v.items()) + " }"
    return json.dumps(v, ensure_ascii=False)


def _write_schedule(sched):
    _write_block("schedule", sched)


def _dump_lines(v, indent):
    """A flat block one key per line (the assistant): an edit is a one-line diff."""
    pad = " " * (indent + 2)
    return "{\n" + ",\n".join(f"{pad}{json.dumps(k)}: {_dump_compact_leaves(x, indent + 2)}" for k, x in v.items()) \
        + "\n" + " " * indent + "}"


def _write_block(key, value, lines=False):
    """Rewrite ONLY one top-level block of the site config ("schedule", "retention",
    "backup_policy"). The rest of the file is hand-written (comments, aligned
    columns) and must survive byte-for-byte, so no json.dump of the whole config.
    A missing block is inserted before "schedule". Verified by re-parsing before
    the atomic replace; owner/mode preserved (the UI runs as root)."""
    text = open(CFG_PATH).read()
    if f'"{key}": {{' not in text:
        at = text.index('"schedule": {')
        ls = text.rfind("\n", 0, at) + 1
        new = text[:ls] + text[ls:at] + f'"{key}": ' + _dump_compact_leaves(value, at - ls) + ",\n\n" + text[ls:]
        return _replace_cfg(new, key, value)
    j = text.index("{", text.index(f'"{key}": {{'))
    depth, k, in_str, esc = 0, j, False, False
    while True:
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    new = text[:j] + (_dump_lines if lines else _dump_compact_leaves)(value, 2) + text[k + 1:]
    _replace_cfg(new, key, value)


def _replace_cfg(new, key, value):
    if json.loads(new).get(key) != value:
        raise RuntimeError(f"{key} rewrite did not round-trip; the site config left untouched")
    st = os.stat(CFG_PATH)
    tmp = f"{CFG_PATH}.tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, CFG_PATH)


def cmd_schedule_set(a):
    """Change how often a profile is snapshotted — fleet default, or one host."""
    c = cfg()
    if "schedule" not in c:
        sys.exit("the site config has no \"schedule\" block to edit")
    if a.profile not in c.get("profiles", {}):
        sys.exit(f"unknown profile '{a.profile}' (have: {', '.join(c.get('profiles', {}))})")
    if a.host and a.host not in {h["name"] for h in hosts(c)}:
        sys.exit(f"unknown host '{a.host}'")
    if a.every and (not _EVERY_RE.match(a.every)
                    or not 900 <= parse_duration(a.every) <= 90 * 86400):
        sys.exit(f"--every '{a.every}': use 15m..90d, e.g. 6h, 1d, 1w")
    if a.window and a.window != "any" and (not _WINDOW_RE.match(a.window)
                                          or a.window[:5] == a.window[6:]):
        sys.exit(f"--window '{a.window}': use HH:MM-HH:MM (the head node's local time, may wrap midnight) or 'any'")
    if not (a.every or a.window or a.disable or a.reset):
        sys.exit("nothing to change: give --every/--window, --disable or --reset")
    win = None if a.window == "any" else a.window
    sched = c["schedule"]
    defaults, hs = sched.setdefault("defaults", {}), sched.setdefault("hosts", {})
    if a.host:
        ho = hs.setdefault(a.host, {})
        before = ho.get(a.profile, "inherit") if a.profile in ho else "inherit"
        if a.reset:
            ho.pop(a.profile, None)
        elif a.disable:
            ho[a.profile] = None
        else:
            cur = dict(ho.get(a.profile) or {})
            if a.every:
                cur["every"] = a.every
            if a.window:
                cur["window"] = win
            ho[a.profile] = cur
        if not ho:
            hs.pop(a.host)
        after = (hs.get(a.host) or {}).get(a.profile, "inherit") \
            if a.profile in (hs.get(a.host) or {}) else "inherit"
    else:
        if a.reset:
            sys.exit("--reset needs --host (it drops a host's override back to the default)")
        before = defaults.get(a.profile)
        if a.disable:
            defaults.pop(a.profile, None)
        else:
            cur = dict(defaults.get(a.profile) or {})
            if a.every:
                cur["every"] = a.every
            if a.window:
                cur["window"] = win
            if "every" not in cur:
                sys.exit(f"--every is required for a new default '{a.profile}' schedule")
            defaults[a.profile] = cur
        after = defaults.get(a.profile)
    try:
        for h in hosts(c):                    # every host must still resolve
            for r in effective_schedule(c, h["name"]).values():
                if not r["every"]:
                    raise ValueError
    except Exception:
        sys.exit("that change leaves a host with a schedule but no interval; give --every")
    _write_schedule(sched)
    audit("schedule-changed", host=a.host or "*", profile=a.profile, before=before, after=after)
    print(f"schedule {a.host or 'default'}/{a.profile}: {before} -> {after}")
    for h in hosts(c):
        if a.host and h["name"] != a.host:
            continue
        r = effective_schedule(c, h["name"]).get(a.profile)
        print(f"  {h['name']:<10} {a.profile}: " + (
            f"every {fmt_duration(r['every'])}, window {r['window'] or 'any'}" if r else "disabled"))


# ── backup policy: what is covered, edited from the UI's Config view ──────────
# the site config "backup_policy" (absent = these defaults). Every value has a fleet
# default and an optional per-host override; `policy-apply` renders it into files
# on each host (the agent and the dump hook read only those files, never this).
#   docker_volumes  back up /var/lib/docker/volumes (named volumes; image layers
#                   and container rootfs stay excluded: rebuilt from images)
#   db_dumps        run the dumps hook before each `system` backup
#   exclude         extra restic patterns; "!pattern" re-includes (applied after
#                   the baseline list, so it can un-exclude something there)
#   skip_dumps      discovered database ids NOT to dump ("docker:NAME", "host:mysql", …)
#   sqlite          extra SQLite files to copy with the online backup API
POLICY_DEFAULTS = {"docker_volumes": True, "db_dumps": True, "exclude": [], "skip_dumps": [], "sqlite": []}
POLICY_BOOLS, POLICY_LISTS = ("docker_volumes", "db_dumps"), ("exclude", "skip_dumps", "sqlite")
DUMPS_STATE = STATE_DIR / "dumps-discovery.json"
_DANGEROUS = {"/", "/*", "/**", "*", "**", "/.", "!"}


def policy_hosts(c):
    """Hosts the policy applies to: backed up, Linux, real."""
    return [h for h in hosts(c) if h.get("os") != "macos" and h.get("repo") not in (None, "windows-class")]


def validate_policy(pol, c=None):
    """Return (clean policy, errors). Used by policy-set AND by the UI before it
    passes anything on: patterns are argv/file content, never shell, but they
    still must be one line, printable, and must not exclude a whole root."""
    c = c or cfg()
    errs = []
    if not isinstance(pol, dict):
        return None, ["policy must be an object"]
    names = {h["name"] for h in policy_hosts(c)}

    def one(d, where, partial):
        r = {}
        for k, v in d.items():
            if k in POLICY_BOOLS:
                if v is None and partial:
                    continue
                if not isinstance(v, bool):
                    errs.append(f"{where}.{k}: must be true/false")
                    continue
                r[k] = v
            elif k in POLICY_LISTS:
                if not isinstance(v, list) or len(v) > 200:
                    errs.append(f"{where}.{k}: must be a list (≤200)")
                    continue
                clean = []
                for x in v:
                    x = x.strip() if isinstance(x, str) else x
                    if not isinstance(x, str) or not x or len(x) > 512 or any(ord(ch) < 32 for ch in x):
                        errs.append(f"{where}.{k}: bad entry {str(x)[:40]!r}")
                    elif k == "exclude" and (x.rstrip("/") or "/") in _DANGEROUS:
                        errs.append(f"{where}.exclude: {x!r} would exclude everything")
                    elif k == "sqlite" and not x.startswith("/"):
                        errs.append(f"{where}.sqlite: {x!r} must be an absolute path")
                    else:
                        clean.append(x)
                if clean or not partial:
                    r[k] = list(dict.fromkeys(clean))
            else:
                errs.append(f"{where}: unknown key {k!r}")
        return r

    out = {**POLICY_DEFAULTS, **one({k: v for k, v in pol.items() if k != "hosts"}, "policy", False)}
    hs = pol.get("hosts") or {}
    if not isinstance(hs, dict):
        errs.append("policy.hosts must be an object")
        hs = {}
    out["hosts"] = {}
    for n, d in hs.items():
        if n not in names:
            errs.append(f"policy.hosts: unknown host {n!r}")
        elif not isinstance(d, dict):
            errs.append(f"policy.hosts.{n}: must be an object")
        else:
            r = one(d, f"policy.hosts.{n}", True)
            if r:
                out["hosts"][n] = r
    return out, errs


def policy(c):
    pol, _ = validate_policy(c.get("backup_policy") or {}, c)
    return pol or {**POLICY_DEFAULTS, "hosts": {}}


def effective_policy(c, host):
    """Fleet values with the host's overrides; lists ADD to the fleet list."""
    p = policy(c)
    o = p["hosts"].get(host, {})
    eff = {k: o.get(k, p[k]) for k in POLICY_BOOLS}
    for k in POLICY_LISTS:
        eff[k] = list(dict.fromkeys(p[k] + o.get(k, [])))
    return eff


def common_exclusions(c):
    """The baseline list every profile uses (unless it ships its own): the packaged one, plus this site's
    own separate mount points (`common_exclusions_extra`, e.g. a data volume backed up by its own profile)."""
    base = (NX_HOME / "agent" / "exclusions-common.conf").read_text()
    extra = [x for x in (c.get("common_exclusions_extra") or []) if isinstance(x, str) and x.strip()]
    if extra:
        base += "\n# ── this site's own mount points (site config common_exclusions_extra) ──\n" + "\n".join(extra) + "\n"
    return base


def render_exclusions(c, host):
    e = effective_policy(c, host)
    lines = ["# GENERATED by `noctyx policy-apply` from the site config's backup_policy —",
             "# edit in the console (Config) or with `noctyx policy-set`, not here.",
             "# Read by the agent AFTER the baseline list, so \"!pattern\" re-includes.", ""]
    if e["docker_volumes"]:
        lines += ["# docker: named volumes are backed up; image layers / container rootfs are not",
                  "/var/lib/docker/*", "!/var/lib/docker/volumes"]
    else:
        lines += ["# docker: excluded entirely (docker_volumes = false)", "/var/lib/docker/**"]
    if e["exclude"]:
        lines += ["", "# extra patterns (fleet + host)"] + e["exclude"]
    return "\n".join(lines) + "\n"


def render_dumps_conf(c, host):
    e = effective_policy(c, host)
    return json.dumps({"enabled": e["db_dumps"], "skip": e["skip_dumps"], "sqlite": e["sqlite"],
                       "profiles": ["system"]}, indent=1) + "\n"


def _put(c, h, path, content, mode):
    """Install content at path on h (root-owned), via stdin — never a command line."""
    cmd = f"install -D -m {mode} -o root -g root /dev/stdin {shlex.quote(path)}"
    if is_head(c, h):
        r = subprocess.run(["sh", "-c", cmd], input=content, capture_output=True, text=True, timeout=60)
    else:
        s_ = c["server"]["ssh"]
        r = subprocess.run(["ssh", *SSH_OPTS, "-i", s_["key"], f"{h.get('ssh_user') or s_['user']}@{addr(h)}",
                            f"sudo -n {cmd}"], input=content, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"{path}: {(r.stderr or r.stdout).strip()[:200]}")


def _dumps_state():
    try:
        return json.loads(DUMPS_STATE.read_text())
    except Exception:
        return {}


def _scan_dumps(c, h):
    r = _on(c, h, f"{P['dumps_bin']} list", timeout=120)
    try:
        d = json.loads(r.stdout)
    except Exception:
        d = {"error": (r.stderr or r.stdout or "no output").strip()[:200]}
    d["scanned_ts"] = time.time()
    st = _dumps_state()
    st[h["name"]] = d
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DUMPS_STATE.write_text(json.dumps(st))
    return d


def policy_apply(c, only=None, scan=True):
    """Push the rendered policy (and the code that reads it) to each host."""
    src = NX_HOME / "agent"
    agent, dumps = render((src / "noctyx-agent.sh").read_text()), render((src / "noctyx-dumps.py").read_text())
    common = common_exclusions(c)
    hook = ("#!/bin/sh\n# GENERATED by noctyx policy-apply: database dumps before a backup\n"
            f"exec {P['dumps_bin']} hook \"$@\"\n")
    failed = 0
    for h in policy_hosts(c):
        if only and h["name"] != only:
            continue
        try:
            profs = _on(c, h, f"ls -d {P['node_etc']}/profile-*.d 2>/dev/null", timeout=30).stdout.split()
            _put(c, h, P["agent_bin"], agent, "0755")
            _put(c, h, P["dumps_bin"], dumps, "0755")
            _put(c, h, f"{P['node_etc']}/hooks/pre.d/10-db-dumps.sh", hook, "0755")
            _put(c, h, f"{P['node_etc']}/db-dumps.json", render_dumps_conf(c, h["name"]), "0600")
            _put(c, h, f"{P['node_etc']}/exclusions-common.conf", common, "0644")
            for pd in profs:
                _put(c, h, f"{pd}/exclusions", render_exclusions(c, h["name"]), "0644")
            e = effective_policy(c, h["name"])
            line = (f"  {h['name']:<10} applied · docker volumes {'on' if e['docker_volumes'] else 'off'}"
                    f" · db dumps {'on' if e['db_dumps'] else 'off'} · {len(e['exclude'])} extra pattern(s)"
                    f" · profiles {','.join(pathlib.Path(x).name[8:-2] for x in profs) or 'none'}")
            if scan:
                d = _scan_dumps(c, h)
                dbs = d.get("discovered", [])
                line += (" · databases: " + ", ".join(x["id"] + ("" if x["enabled"] else " (skip)") for x in dbs)
                         if dbs else " · no databases")
                if d.get("error"):
                    line += f" · scan failed: {d['error']}"
            print(line, flush=True)
        except Exception as ex:
            failed += 1
            print(f"  {h['name']:<10} FAILED: {ex}", flush=True)
    audit("policy-applied", host=only or "*", failed=failed)
    return failed


def cmd_policy_apply(a):
    sys.exit(1 if policy_apply(cfg(), a.host, scan=not a.no_scan) else 0)


def cmd_policy_set(a):
    """Replace backup_policy (and optionally retention) from JSON, then apply."""
    c = cfg()
    try:
        body = json.loads(a.json)
    except Exception as e:
        sys.exit(f"--json: {e}")
    if not isinstance(body, dict):
        sys.exit("--json: must be an object")
    pol, errs = validate_policy(body.get("policy", body), c)
    ret = body.get("retention")
    keys = ("daily", "weekly", "monthly")
    if ret is not None:
        if not isinstance(ret, dict) or any(type(ret.get(k)) is not int or not 0 <= ret[k] <= 999 for k in keys):
            errs.append("retention: daily/weekly/monthly must be whole numbers 0..999")
        elif sum(ret[k] for k in keys) == 0:
            errs.append("retention: keeping nothing would delete every snapshot")
    hf = body.get("hf_mirror")
    if hf is not None:
        names = {h["name"] for h in c["hosts"]}
        if (not isinstance(hf, dict) or not isinstance(hf.get("harvest"), bool)
                or not isinstance(hf.get("exclude_hosts"), list) or any(x not in names for x in hf["exclude_hosts"])
                or type(hf.get("bwlimit_mb")) is not int or not 0 <= hf["bwlimit_mb"] <= 10000):
            errs.append("hf_mirror: harvest true/false, exclude_hosts known host names, bwlimit_mb 0..10000")
    if errs:
        sys.exit("refused:\n  " + "\n  ".join(errs))
    if hf is not None:
        new_hf = {**hf_cfg(c), **{k: hf[k] for k in ("harvest", "exclude_hosts", "bwlimit_mb")}}
        if new_hf != c.get("hf_mirror"):
            _write_block("hf_mirror", new_hf)
            audit("hf-mirror-changed", before=c.get("hf_mirror"), after=new_hf)
            print(f"hf_mirror saved: harvest {'on' if new_hf['harvest'] else 'off'}, "
                  f"excluded {', '.join(new_hf['exclude_hosts']) or 'none'}, bwlimit {new_hf['bwlimit_mb'] or 'none'}")
    before = c.get("backup_policy")
    if pol != before:
        _write_block("backup_policy", pol)
        audit("policy-changed", before=before, after=pol)
        print("backup_policy saved")
    if ret is not None:
        new_ret = {**c.get("retention", {}), **{k: ret[k] for k in keys}}
        if new_ret != c.get("retention"):
            _write_block("retention", new_ret)
            audit("retention-changed", before=c.get("retention"), after=new_ret)
            print(f"retention saved: {', '.join(f'{k} {new_ret[k]}' for k in keys)}")
    sys.exit(1 if policy_apply(cfg()) else 0)


def cmd_policy_show(a):
    c = cfg()
    for h in policy_hosts(c):
        if a.host and h["name"] != a.host:
            continue
        print(f"== {h['name']}: profile-*.d/exclusions\n{render_exclusions(c, h['name'])}"
              f"== {h['name']}: {P['node_etc']}/db-dumps.json\n{render_dumps_conf(c, h['name'])}")


def cmd_dumps(a):
    """Run the dump hook now (no backup), then refresh what the UI shows."""
    c, bad = cfg(), 0
    for h in policy_hosts(c):
        if a.host and h["name"] != a.host:
            continue
        r = _on(c, h, f"{P['dumps_bin']} now", timeout=3 * 3600)
        print(f"== {h['name']} (rc={r.returncode})\n{(r.stderr or '').rstrip() or '  nothing to dump'}", flush=True)
        bad += r.returncode != 0
        _scan_dumps(c, h)
    audit("dumps-run", host=a.host or "*", failed=bad)
    sys.exit(1 if bad else 0)


# ── HF mirror: one deduplicated Hugging Face cache on the NAS, fed from the hosts ──
# `hf-harvest` copies model files the store does not have yet out of every host's HF
# caches (found by cache/lister.py), checks each against its content hash, and
# merges snapshots/refs. cache/mirror.py serves the store as an HF_ENDPOINT.
# The store is a plain HF hub cache: models--org--name/{blobs,snapshots,refs}, so it
# also works read-only as HF_HUB_CACHE. Blobs are content-addressed (sha256 for LFS
# files, git sha1 otherwise), so the same model on five hosts is stored once.
HF_DEFAULTS = {"store": "/srv/noctyx/hf-mirror", "port": 20995, "harvest": True, "scan_roots": ["/"],
               "exclude_hosts": [], "bwlimit_mb": 0}


def hf_cfg(c):
    return {**HF_DEFAULTS, **(c.get("hf_mirror") or {})}


def hf_skip(c):
    """Never scan the NAS mount (the store and the repositories live there) nor configured extras."""
    hc = hf_cfg(c)
    roots = [str(pathlib.Path(hc["store"]))] + ([repo_root(c)] if c.get("server", {}).get("repo_root") else [])
    try:
        common = os.path.commonpath(roots)
    except ValueError:
        common = "/"
    return ([common] if common not in ("/", "") else roots) + list(hc.get("skip") or [])


def hf_sources(c):
    ex = set(hf_cfg(c)["exclude_hosts"])
    return [h for h in c["hosts"] if h.get("enabled", True) is not False and h.get("address")
            and h.get("repo") != "windows-class" and h["name"] not in ex]


def _run_on(c, h, cmd, stdin=None, timeout=600):
    """Root shell snippet on h with optional stdin: locally on the head node, else ssh + sudo."""
    if is_head(c, h):
        return subprocess.run(["sh", "-c", cmd], input=stdin, capture_output=True, text=True, timeout=timeout)
    s_ = c["server"]["ssh"]
    return subprocess.run(["ssh", *SSH_OPTS, "-i", s_["key"], f"{h.get('ssh_user') or s_['user']}@{addr(h)}",
                           f"sudo -n {cmd}"], input=stdin, capture_output=True, text=True, timeout=timeout)


def _hf_hash_ok(path, name):
    """True/False against the blob's own name; None when the name is not a hash we know."""
    import hashlib
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
    if a.ignore_window:
        return
    win = ((c.get("schedule") or {}).get("defaults") or {}).get("system", {}).get("window")
    said = False
    while True:
        now = datetime.datetime.now()
        busy = spindle_holder(nas_name(c))
        inwin = win and (in_window(now, win) or in_window(now + datetime.timedelta(minutes=15), win))
        if not busy and not inwin:
            return
        if not said:
            print(f"  paused: {'backup window ' + win if inwin else nas_name(c) + ' busy: ' + str(busy.get('what'))}"
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


def hf_harvest_host(c, h, a, hub):
    hc = hf_cfg(c)
    starts = "~" if h.get("os") == "macos" else " ".join(shlex.quote(r) for r in hf_cfg(c)["scan_roots"])
    lst = (NX_HOME / "cache" / "lister.py").read_text()
    skip = " ".join(shlex.quote(f"--skip={s}") for s in hf_skip(c))
    r = _run_on(c, h, f"python3 - {skip} {starts}", stdin=lst, timeout=900)
    try:
        repos = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f"lister failed: {(r.stderr or r.stdout).strip()[:200]}")
    st = {"repos": 0, "new_blobs": 0, "new_bytes": 0, "bad_hash": 0, "unverified": 0, "skipped": []}
    s_ = c["server"]["ssh"]
    for d in repos:
        if a.repo and a.repo not in d["repo"]:
            continue
        st["repos"] += 1
        hr = hub / d["repo"]
        want = {b for files in d["files"].values() for b in files.values()}
        have = {p.name for p in (hr / "blobs").glob("*")} if (hr / "blobs").is_dir() else set()
        need = sorted(want - have)
        if need:
            if any(ch in d["root"] for ch in " '\"\\$`"):
                st["skipped"].append(d["root"])
                continue
            _hf_wait(c, a)
            inc = hub.parent / ".incoming" / h["name"] / d["repo"]
            inc.mkdir(parents=True, exist_ok=True)
            src = f"{d['root']}/{d['repo']}/blobs/"
            cmd = ["rsync", "-t", "--partial", "--files-from=-"]
            if hc["bwlimit_mb"]:
                cmd.append(f"--bwlimit={int(hc['bwlimit_mb'] * 1000)}")
            if is_head(c, h):
                cmd += [src, str(inc) + "/"]
            else:
                cmd += ["-e", f"ssh -i {s_['key']} -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
                              " -o ServerAliveInterval=60",
                        "--rsync-path=sudo -n rsync", f"{h.get('ssh_user') or s_['user']}@{addr(h)}:{src}", str(inc) + "/"]
            gb = sum(d["blobs"][b] for b in need) / 1e9
            print(f"  {h['name']}: {d['repo']} — {len(need)} file(s), {gb:.1f} GB", flush=True)
            rr = subprocess.run(cmd, input="\n".join(need) + "\n", capture_output=True, text=True)
            if rr.returncode not in (0, 23, 24):     # 23/24: some files vanished/partial — checked below
                print(f"    rsync rc={rr.returncode}: {rr.stderr.strip()[-200:]}", flush=True)
            (hr / "blobs").mkdir(parents=True, exist_ok=True)
            for b in need:
                f = inc / b
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
            for rel, b in files.items():
                if b in have and ".." not in rel.split("/"):
                    _hf_link(hr, commit, rel, b)
        # refs: newest wins across hosts (a host that pulled later saw a later main)
        mf = hr / ".cm-refs.json"
        try:
            meta = json.loads(mf.read_text())
        except Exception:
            meta = {}
        changed = False
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
    c = cfg()
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
        with spindle_lock("hf-harvest", what="hf-harvest"):
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
                          f"{', skipped roots ' + ', '.join(st['skipped']) if st['skipped'] else ''}", flush=True)
                except Exception as e:
                    bad += 1
                    st = {"ts": time.time(), "error": str(e)[:300]}
                    print(f"{h['name']}: FAILED {e}", flush=True)
                status["hosts"][h["name"]] = st
                status.update(ts=time.time(), totals=hf_totals(hub))
                sf.write_text(json.dumps(status))
            t = status.get("totals") or hf_totals(hub)
            print(f"store: {t['repos']} repos, {t['blobs']} files, {t['bytes'] / 1e12:.2f} TB")
            audit("hf-harvest", host=a.host or "*", failed=bad, totals=t)
            sys.exit(1 if bad else 0)
    except SpindleBusy:
        print(f"hf-harvest already running ({spindle_holder('hf-harvest').get('since')}) — skipped")


# ── recipe deployment ─────────────────────────────────────────────────────────
# A slot (one GPU box, or a TP pair's head + worker) serves one recipe at a time on a
# fixed port. Every deployment is a systemd unit on the slot's head: the one that served
# before Noctyx (adopt), or <recipe_unit_prefix>-<slot>.service rendered for a checkout at a
# pinned commit. Switching = disable the old unit(s), enable the new one, wait for
# /health and /v1/models; a start that does not come up switches back.
RECIPE_STATE = STATE_DIR / "recipes.json"
RECIPE_KINDS = ("spark", "systemd", "script")   # python3 -m spark | setup.sh + serve.sh | start.sh + stop.sh
_RNAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,47}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^(https://|ssh://)[A-Za-z0-9@._:/-]+$")
_ENVK = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
PREBUILD_MIN_GIB = 40      # a pair head builds the next image while serving only with this much free


def recipes_cfg(c):
    r = c.get("recipes") or {}
    return {"root": r.get("root") or P["recipe_root"], "hf_mirror": r.get("hf_mirror", True),
            "slots": r.get("slots") or {}, "catalog": r.get("catalog") or {}}


def recipe_slot(c, name):
    s = recipes_cfg(c)["slots"].get(name)
    if not s:
        sys.exit(f"unknown slot: {name} (slots: {', '.join(recipes_cfg(c)['slots']) or 'none'})")
    return s


def _recipe_state():
    try:
        return json.loads(RECIPE_STATE.read_text())
    except Exception:
        return {"slots": {}}


def _save_recipe_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RECIPE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, RECIPE_STATE)


def _adopted(s):
    a = s.get("adopt") or {}
    if not a.get("unit"):
        return None
    return {"recipe": None, "name": a.get("name") or a["unit"], "adopted": True, "unit": a["unit"],
            "spark_dir": a.get("spark_dir")}


def slot_records(c, slot):
    """(current, previous, history) of a slot; before any deploy, current is the adopted unit."""
    st = _recipe_state()["slots"].get(slot) or {}
    return st.get("current") or _adopted(recipe_slot(c, slot)), st.get("previous"), st.get("history") or []


def _record(slot, current=None, previous=None, event=None, keep_previous=False):
    st = _recipe_state()
    s = st["slots"].setdefault(slot, {})
    if current is not None:
        if not keep_previous:
            s["previous"] = s.get("current") or previous
        s["current"] = current
    if event:
        s["history"] = ([{"ts": time.time(), **event}] + (s.get("history") or []))[:30]
    _save_recipe_state(st)


def rec_label(r):
    if not r:
        return "nothing"
    return r.get("name") if r.get("adopted") else f"{r['recipe']}@{r['sha'][:12]}"


def recipe_live(c, slot, timeout=3):
    """What the slot's port answers right now, asked from here: {ok, models, error}."""
    s = recipe_slot(c, slot)
    h = host_by_name(c, s["head"])
    base = f"http://{addr(h)}:{s['port']}"
    out = {"ok": False, "models": [], "url": base}
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            hj = json.loads(r.read() or b"{}")
        out["status"] = hj.get("status")
        with urllib.request.urlopen(base + "/v1/models", timeout=timeout) as r:
            out["models"] = [m.get("id") for m in json.loads(r.read()).get("data", []) if m.get("id")]
        out["ok"] = hj.get("status", "ok") == "ok" and bool(out["models"])
        for k in ("max_batch_size", "warming"):
            if k in hj:
                out[k] = hj[k]
        if isinstance(hj.get("live"), dict):
            out["connections"] = hj["live"].get("connections")
    except Exception as e:
        out["error"] = str(getattr(e, "reason", e))[:120]
    return out


def _slot_user(c, h):
    return h.get("ssh_user") or c["server"]["ssh"]["user"]


def _root_stream(c, h, script):
    """A bash script as root on h, fed on stdin (nothing secret or quoted on a command line),
    its output streamed into ours. Returns (rc, output)."""
    if is_head(c, h):
        argv = ["bash", "-s"]
    else:
        s_ = c["server"]["ssh"]
        argv = ["ssh", *SSH_OPTS, "-i", s_["key"], f"{_slot_user(c, h)}@{addr(h)}", "sudo -n bash -s"]
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdin.write(script.encode())
    p.stdin.close()
    out = []
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").replace("\r", "\n")
        out.append(line)
        if len(out) > 4000:
            out = out[-2000:]
        sys.stdout.write("  " + line if not line.startswith("NX_") else "")
        sys.stdout.flush()
    return p.wait(), "".join(out)


def _home(c, h, user):
    r = _run_on(c, h, f"getent passwd {shlex.quote(user)}", timeout=30)
    home = (r.stdout or "").strip().split(":")[5:6]
    if r.returncode != 0 or not home or not home[0].startswith("/"):
        raise RuntimeError(f"cannot find {user}'s home on {h['name']}: {(r.stderr or '').strip()[:120]}")
    return home[0]


def _tilde(p, home):
    return home + p[1:] if p == "~" or p.startswith("~/") else p


def _resolve_ref(repo, ref):
    """A branch, tag or full sha -> the commit sha (git ls-remote from the head node)."""
    if _SHA.match(ref):
        return ref
    r = subprocess.run(["git", "ls-remote", repo, ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
                       capture_output=True, text=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    refs = dict(reversed(l.split("\t", 1)) for l in (r.stdout or "").splitlines() if "\t" in l)
    for k in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}", ref):
        if k in refs:
            return refs[k]
    raise RuntimeError(f"{repo}: no branch or tag {ref!r}" + (f" ({r.stderr.strip()[:160]})" if r.stderr.strip() else ""))


def _sh_env(env):
    return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())


def _toml(sections):
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return json.dumps(str(v))
    out = ["# written by `noctyx recipe-deploy` (slot settings + catalog overrides); redeploy to change"]
    for sec, kv in sections.items():
        out.append(f"\n[{sec}]")
        out += [f"{k} = {val(v)}" for k, v in kv.items()]
    return "\n".join(out) + "\n"


def _unit_text(rec, slot, s, env):
    d, user = rec["dir"], rec["user"]
    envl = "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
    if rec["kind"] == "script":
        return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
                f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
                f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot}, start.sh)\n"
                f"After=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nUser={user}\nWorkingDirectory={d}\n"
                f"Environment=HOME={rec['home']}\nEnvironment=PATH={rec['home']}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"{envl}ExecStart={d}/start.sh\nExecStop={d}/{rec.get('stop') or 'stop.sh'}\n"
                f"TimeoutStartSec={int(s.get('health_timeout_s', 3600))}\nTimeoutStopSec=300\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
    if rec["kind"] == "spark":
        return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
                f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
                f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot}, TP pair)\n"
                f"After=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nUser={user}\nWorkingDirectory={d}\n"
                f"Environment=HOME={rec['home']}\nEnvironment=PATH={rec['home']}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"{envl}ExecStart=/usr/bin/python3 -m spark up\nExecStop=/usr/bin/python3 -m spark down\n"
                f"TimeoutStartSec={int(s.get('health_timeout_s', 3600))}\nTimeoutStopSec=300\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
    return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
            f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
            f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot})\n"
            f"After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\nUser={user}\nWorkingDirectory={d}\nEnvironment=HOME={rec['home']}\n"
            f"{envl}ExecStart={d}/serve.sh\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=60\n"
            f"StandardOutput=append:{d}/serve.log\nStandardError=append:{d}/serve.log\n\n"
            f"[Install]\nWantedBy=multi-user.target\n")


def _spark_info(c, h, user, home, d):
    """The settings a spark recipe's checkout resolves to (cluster.toml + recipe.toml), or {}."""
    py = ("import json; from spark import config; s = config.load(); print('NX_INFO ' + json.dumps({"
          "'tf_cache': str(s.tf_cache), 'container': s.container, 'served': getattr(s, 'served_name', None),"
          "'port': s.port}))")
    r = _run_on(c, h, f"sudo -u {shlex.quote(user)} -H bash -lc "
                      + shlex.quote(f"cd {shlex.quote(d)} && python3 -c {shlex.quote(py)}"), timeout=60)
    m = re.search(r"^NX_INFO (.+)$", r.stdout or "", re.M)
    return json.loads(m.group(1)) if m else {}


# weights for the next recipe while the old one serves; never the folder the server reads
_SPARK_PREP = r'''
import sys
from spark import config, weights
from spark.nodes import Node
s = config.load(); head = Node("head"); worker = Node("worker", s.worker); sync = sys.argv[1] == "1"
if hasattr(weights, "ensure_converted"):
    if s.converted_repo or weights.converted_ready(s, head):
        weights.ensure_converted(s, head)
        if sync and hasattr(weights, "sync_converted"):
            weights.sync_converted(s, head, worker)
    else:
        weights.ensure_snapshot(s)
        print("the conversion needs the GPU: it runs during the switch (`spark up`)")
else:
    weights.ensure_snapshot(s)
    dfl = getattr(s, "drafts", False) and getattr(s, "drafter", "") == "dflash2" and hasattr(weights, "ensure_dflash2")
    if dfl:
        weights.ensure_dflash2(s)
    if sync and hasattr(weights, "sync_snapshot"):
        weights.sync_snapshot(s, head, worker)
        if dfl and hasattr(weights, "sync_dflash2"):
            weights.sync_dflash2(s, head, worker)
print("weights ready")
'''


def recipe_prepare(c, slot, name, ref=None):
    """Check out, configure, build and download — the old deployment keeps serving. Returns the record."""
    rc_ = recipes_cfg(c)
    s = recipe_slot(c, slot)
    cat = rc_["catalog"].get(name)
    if not cat:
        raise RuntimeError(f"unknown recipe: {name} (catalog: {', '.join(rc_['catalog'])})")
    if slot not in (cat.get("slots") or []):
        raise RuntimeError(f"{name} does not run on slot {slot} (its slots: {', '.join(cat.get('slots') or [])})")
    ref = ref or cat.get("ref") or "main"
    sha = _resolve_ref(cat["repo"], ref)
    h = host_by_name(c, s["head"])
    user = _slot_user(c, h)
    home = _home(c, h, user)
    root = _tilde(s.get("root") or rc_["root"], home)
    d = f"{root}/{name}/{sha[:12]}"
    rec = {"recipe": name, "kind": cat["kind"], "repo": cat["repo"], "ref": ref, "sha": sha, "dir": d,
           "user": user, "home": home, "unit": f"{P['recipe_unit_prefix']}-{slot}.service", "host": h["name"]}
    env = {**(s.get("env") or {}), **(cat.get("env") or {})}
    hfe = {}
    if rc_["hf_mirror"] and (c.get("hf_mirror") or {}).get("port"):
        hfe = {"HF_ENDPOINT": f"http://{c['server']['address']}:{hf_cfg(c)['port']}"}
    files = {}
    if cat["kind"] == "spark":
        sec = {k: dict(v) for k, v in (cat.get("overrides") or {}).items()}
        sec.setdefault("cluster", {}).update({**(s.get("cluster") or {}), "port": s["port"]})
        files["cluster.toml"] = _toml(sec)
        env = {**env, **hfe}
    elif cat["kind"] == "script":
        # start.sh recipes read their settings from the environment / ./.env (Mia-AiLab's convention)
        senv = {**(s.get("script_env") or {}), **(cat.get("env") or {}), "PORT": str(s["port"]), **hfe}
        files[".env"] = "# written by `noctyx recipe-deploy` (slot script_env + catalog env)\n" + \
            "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in senv.items())
        env = {**env, **senv}
    else:
        env = {"PORT": str(s["port"]), "MODELS": f"{root}/{name}/models", "TF_SRC": f"{d}/engine", **env}
    rec["unit_text"] = _unit_text(rec, slot, s, env)
    print(f"prepare {name}@{sha[:12]} ({ref}) on {h['name']}:{d}", flush=True)

    q = shlex.quote
    put = "".join(f"echo {__import__('base64').b64encode(t.encode()).decode()} | base64 -d > {q(d + '/' + f)}\n"
                  for f, t in files.items())
    script = f"""set -uo pipefail
cd /
asu() {{ sudo -u {q(user)} -H env HOME={q(home)} {_sh_env(hfe)} bash -lc "$1"; }}
D={q(d)}
if [ ! -d "$D/.git" ]; then
  echo "clone {cat['repo']} -> $D"
  asu {q(f'rm -rf {q(d)}.partial && mkdir -p {q(os.path.dirname(d))} && git clone -q --filter=blob:none {q(cat["repo"])} {q(d)}.partial && git -C {q(d)}.partial -c advice.detachedHead=false checkout -q {sha} && mv {q(d)}.partial {q(d)}')} || exit 10
fi
[ "$(sudo -u {q(user)} git -C "$D" rev-parse HEAD)" = {sha} ] || {{ echo "checkout $D is not at {sha}"; exit 11; }}
sudo -u {q(user)} bash -c {q(put)} || exit 12
"""
    if cat["kind"] == "spark":
        r1, _ = _root_stream(c, h, script + f"asu {q(f'cd {q(d)} && python3 -m spark validate')} || exit 13\n")
        if r1:
            raise RuntimeError(f"checkout/validate failed (rc={r1})")
        info = _spark_info(c, h, user, home, d)
        rec["expect"] = [info["served"]] if info.get("served") else []
        cur, _, _ = slot_records(c, slot)
        cd = (cur or {}).get("spark_dir") or ((cur or {}).get("dir") if (cur or {}).get("kind") == "spark" else None)
        cinfo = _spark_info(c, h, user, home, _tilde(cd, home)) if cd else {}
        # the server reads its own tf_cache: re-downloading or syncing into that folder under it is never safe
        shared = bool(cinfo) and cinfo.get("tf_cache") == info.get("tf_cache")
        mem = _run_on(c, h, "awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo", timeout=20)
        free_gib = int((mem.stdout or "0").strip() or 0)
        steps = []
        if free_gib >= PREBUILD_MIN_GIB:
            steps.append(f"asu {q(f'cd {q(d)} && python3 -m spark image')} || exit 14")
        else:
            print(f"  {h['name']} has {free_gib} GiB free (< {PREBUILD_MIN_GIB}): the image builds during the switch")
        if shared:
            print(f"  {name} shares {info.get('tf_cache')} with what serves now: weights update during the switch")
        else:
            steps.append(f"asu {q(f'cd {q(d)} && python3 - 1 <<' + chr(39) + 'PYEOF' + chr(39) + chr(10) + _SPARK_PREP + 'PYEOF')} || exit 15")
        r2, _ = _root_stream(c, h, script + "\n".join(steps) + "\n") if steps else (0, "")
        if r2:
            raise RuntimeError(f"prepare failed (rc={r2})")
    elif cat["kind"] == "script":
        chk = (f"cd {q(d)} && test -x start.sh && test -x stop.sh"
               f" && if grep -q DRY_RUN start.sh; then set -a; . ./.env; set +a; DRY_RUN=1 ./start.sh; fi")
        r1, _ = _root_stream(c, h, script + f"asu {q(chk)} || exit 17\n")
        if r1:
            raise RuntimeError(f"start.sh check failed (rc={r1}): needs start.sh + stop.sh, and its DRY_RUN must pass")
        rec["expect"] = []
    else:
        bld = f"cd {q(d)} && test -x setup.sh -a -x serve.sh && {_sh_env({k: v for k, v in env.items() if k != 'PORT'})} ./setup.sh"
        r1, _ = _root_stream(c, h, script + f"asu {q(bld)} || exit 16\n")
        if r1:
            raise RuntimeError(f"setup.sh failed (rc={r1})")
        rec["expect"] = []
    rec["prepared_ts"] = time.time()
    st = _recipe_state()
    st["slots"].setdefault(slot, {}).setdefault("prepared", {})[name] = rec
    _save_recipe_state(st)
    audit("recipe-prepare", slot=slot, recipe=name, sha=sha, host=h["name"])
    return rec


def _switch(c, slot, frm, to, wait_lock=0):
    """Under the slot's GPU lock on its head: stop frm (and every conflicting unit), check the port
    and GPU are free, start to, wait until it serves. rc 0 + models, or rc>0 (75: lock busy, nothing done)."""
    s = recipe_slot(c, slot)
    h = host_by_name(c, s["head"])
    user = _slot_user(c, h)
    home = (to or frm or {}).get("home") or _home(c, h, user)
    q = shlex.quote
    lock = _tilde(s.get("lock") or P["gpu_lock"], home)
    port = int(s["port"])
    stop = list(dict.fromkeys([*(s.get("conflicts") or []), f"{P['recipe_unit_prefix']}-{slot}.service"] + ([frm["unit"]] if frm else [])))
    downs = [x for x in {(frm or {}).get("spark_dir"), (frm or {}).get("dir") if (frm or {}).get("kind") == "spark" else None} if x]
    sdowns = [(frm or {})["dir"]] if (frm or {}).get("kind") == "script" else []
    ht = int(s.get("health_timeout_s", 1800))
    lines = [f"""set -uo pipefail
cd /
x() {{ echo "+ $*"; "$@"; }}
asu() {{ sudo -u {q(user)} -H env HOME={q(home)} bash -lc "$1"; }}
LOCK={q(lock)}
[ -e "$LOCK" ] || sudo -u {q(user)} sh -c 'mkdir -p "$(dirname "$1")" && touch "$1"' _ "$LOCK"
exec 9>>"$LOCK"
if ! flock -n 9; then
  echo "GPU lock $LOCK is held:"; ps -o pid=,etime=,args= -p "$(fuser "$LOCK" 2>/dev/null | tr -s ' ' ',' | sed 's/^,//')" 2>/dev/null | cut -c1-160
  {f'echo "waiting up to {wait_lock}s"; flock -w {wait_lock} 9 ||' if wait_lock else ''} exit 75
fi
alive() {{ curl -s -m 3 http://127.0.0.1:{port}/health 2>/dev/null | grep -q '"status"'; }}
echo "== stop ({rec_label(frm)})"
for u in {' '.join(q(u) for u in stop)}; do
  if systemctl is-enabled -q "$u" 2>/dev/null || systemctl is-active -q "$u" 2>/dev/null; then x systemctl disable --now "$u"; fi
done"""]
    for dd in downs:
        dd = _tilde(dd, home)
        lines.append(f"[ -d {q(dd)} ] && asu {q(f'cd {q(dd)} && python3 -m spark down')}")
    for dd in sdowns:
        lines.append(f"[ -x {q(dd)}/stop.sh ] && asu {q(f'cd {q(dd)} && set -a && . ./.env && set +a && ./stop.sh')}")
    lines.append(f"""for i in $(seq 120); do alive || break; sleep 1; done
if alive; then echo "port {port} still answers after the stop: a server Noctyx does not manage holds it"; exit 76; fi""")
    if s.get("gpu_idle_check"):
        lines.append("""for i in $(seq 120); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && break; sleep 1; done
busy=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)
if [ -n "$busy" ]; then echo "the GPU is still in use:"; echo "$busy"; exit 77; fi""")
    if not to:
        lines.append('echo "== stopped"; exit 0')
        rc, _ = _root_stream(c, h, "\n".join(lines) + "\n")
        return rc, []
    lines.append(f"sleep {int(s.get('settle_s', 5))}")
    lines.append(f'echo "== start ({rec_label(to)})"')
    unit = to["unit"]
    if not to.get("adopted"):
        b64 = __import__("base64").b64encode(to["unit_text"].encode()).decode()
        lines.append(f"[ -d {q(to['dir'])} ] || {{ echo 'checkout {to['dir']} is gone: prepare again'; exit 78; }}")
        lines.append(f"echo {b64} | base64 -d > /etc/systemd/system/{unit} && systemctl daemon-reload || exit 78")
    lines.append(f"""x systemctl enable {q(unit)} >/dev/null 2>&1; x systemctl start --no-block {q(unit)} || exit 78
t0=$(date +%s); n=0
while :; do
  if alive && curl -s -m 3 http://127.0.0.1:{port}/health | grep -q '"status": *"ok"' && curl -s -m 3 http://127.0.0.1:{port}/v1/models | grep -q '"id"'; then break; fi
  st=$(systemctl show -p ActiveState --value {q(unit)})
  if [ "$st" = failed ] || {{ [ "$st" = inactive ] && [ $(( $(date +%s) - t0 )) -gt 20 ]; }}; then
    echo "{unit} is $st:"; journalctl -u {q(unit)} -n 40 --no-pager -o cat; exit 79; fi
  if [ $(( $(date +%s) - t0 )) -gt {ht} ]; then echo "not serving after {ht}s"; journalctl -u {q(unit)} -n 20 --no-pager -o cat; exit 79; fi
  n=$((n+1)); [ $((n % 12)) -eq 0 ] && echo "  loading… $(( $(date +%s) - t0 ))s ($st)"
  sleep 5
done
echo "== serving after $(( $(date +%s) - t0 ))s"
echo "NX_MODELS $(curl -s -m 5 http://127.0.0.1:{port}/v1/models | python3 -c 'import json,sys; print(" ".join(m["id"] for m in json.load(sys.stdin)["data"]))')"
""")
    rc, out = _root_stream(c, h, "\n".join(lines) + "\n")
    m = re.findall(r"^NX_MODELS (.*)$", out, re.M)
    return rc, (m[-1].split() if m else [])


def _llm_refresh():
    subprocess.run(["systemctl", "start", "--no-block", "llm-discover.service"], capture_output=True)


def recipe_plan(c, slot, name=None, ref=None):
    s = recipe_slot(c, slot)
    cur, prev, _ = slot_records(c, slot)
    live = recipe_live(c, slot)
    hosts_ = [s["head"]] + ([s["worker"]] if s.get("worker") else [])
    plan = {"slot": slot, "hosts": hosts_, "port": s["port"], "current": cur, "previous": prev, "live": live,
            "impact": [], "blockers": []}
    if live["ok"]:
        plan["impact"].append(f"{', '.join(live['models'])} on {'+'.join(hosts_)}:{s['port']} stops serving during the switch")
    if name:
        cat = recipes_cfg(c)["catalog"].get(name)
        if not cat:
            plan["blockers"].append(f"unknown recipe {name}")
        elif slot not in (cat.get("slots") or []):
            plan["blockers"].append(f"{name} does not run on {slot}")
        else:
            try:
                plan["sha"] = _resolve_ref(cat["repo"], ref or cat.get("ref") or "main")
            except Exception as e:
                plan["blockers"].append(str(e)[:200])
    try:
        holder = spindle_holder(f"recipe-{slot}")
        if holder:
            plan["blockers"].append(f"another recipe job holds slot {slot}: {holder.get('what')} since {holder.get('since')}")
    except Exception:
        pass
    return plan


def _print_plan(p, what):
    print(f"{what} on slot {p['slot']} ({'+'.join(p['hosts'])}, port {p['port']})")
    print(f"  now:  {rec_label(p['current'])}  — " + (f"serving {', '.join(p['live']['models'])}" if p["live"]["ok"]
                                                   else f"not serving ({p['live'].get('error') or p['live'].get('status')})"))
    for i in p["impact"]:
        print(f"  ! {i}")
    for b in p["blockers"]:
        print(f"  ✗ {b}")


def cmd_recipe_deploy(a):
    c = cfg()
    p = recipe_plan(c, a.slot, a.recipe, a.ref)
    _print_plan(p, f"deploy {a.recipe}" + (f"@{p['sha'][:12]}" if p.get("sha") else ""))
    if p["blockers"]:
        sys.exit(1)
    if not (a.yes or a.prepare_only):
        print("\nnothing changed: add --yes to deploy (or --prepare-only to check out and build without switching)")
        sys.exit(2)
    try:
        with spindle_lock(f"recipe-{a.slot}", what=f"recipe-deploy {a.recipe} on {a.slot}"):
            t0 = time.time()
            try:
                new = recipe_prepare(c, a.slot, a.recipe, a.ref)
            except Exception as e:
                print(f"prepare FAILED: {e} — nothing was stopped")
                audit("recipe-deploy", slot=a.slot, recipe=a.recipe, rc=1, stage="prepare", error=str(e)[:200])
                sys.exit(1)
            print(f"prepared in {fmt_duration(time.time() - t0)}", flush=True)
            if a.prepare_only:
                return
            cur, _, _ = slot_records(c, a.slot)
            t1 = time.time()
            rc, models = _switch(c, a.slot, cur, new, wait_lock=a.wait_lock * 60)
            if rc == 0:
                new.update(models=models, deployed_ts=time.time(), state="serving")
                _record(a.slot, current=new, previous=cur,
                        event={"event": "deploy", "to": rec_label(new), "from": rec_label(cur), "rc": 0,
                               "down_s": round(time.time() - t1)})
                audit("recipe-deploy", slot=a.slot, recipe=a.recipe, sha=new["sha"], rc=0, models=models,
                      replaced=rec_label(cur), down_s=round(time.time() - t1))
                print(f"✓ {a.slot}: {rec_label(new)} serving {', '.join(models)} "
                      f"(switch took {fmt_duration(time.time() - t1)})")
                _llm_refresh()
                return
            if rc == 75:
                print("✗ the GPU lock is held (a harness run or another job): nothing was stopped — try again later"
                      " or pass --wait-lock MINUTES")
                audit("recipe-deploy", slot=a.slot, recipe=a.recipe, rc=75, stage="lock")
                sys.exit(75)
            print(f"✗ {rec_label(new)} did not come up (rc={rc}): switching back to {rec_label(cur)}", flush=True)
            back = 1
            if cur:
                back, models = _switch(c, a.slot, new, cur, wait_lock=600)
            _record(a.slot, event={"event": "deploy", "to": rec_label(new), "from": rec_label(cur), "rc": rc,
                                   "rolled_back": back == 0})
            audit("recipe-deploy", slot=a.slot, recipe=a.recipe, sha=new["sha"], rc=rc, rolled_back=back == 0)
            print(f"{'✓ rolled back: ' + rec_label(cur) + ' serving ' + ', '.join(models) if back == 0 else '✗ ROLLBACK FAILED: slot ' + a.slot + ' is serving nothing — check ' + new['host']}")
            _llm_refresh()
            sys.exit(rc)
    except SpindleBusy:
        print(f"✗ slot {a.slot} is busy: {spindle_holder(f'recipe-{a.slot}').get('what')}")
        sys.exit(1)


def _slot_op(a, what):
    c = cfg()
    p = recipe_plan(c, a.slot)
    cur, prev = p["current"], p["previous"]
    _print_plan(p, what)
    if p["blockers"]:
        sys.exit(1)
    if what == "rollback" and not prev:
        sys.exit("nothing to roll back to")
    if what == "start" and p["live"]["ok"]:
        print("already serving — nothing to do")
        return
    if what != "start" and not a.yes:
        print(f"\nnothing changed: add --yes to {what}" + (f" (back to {rec_label(prev)})" if what == "rollback" else ""))
        sys.exit(2)
    try:
        with spindle_lock(f"recipe-{a.slot}", what=f"recipe-{what} {a.slot}"):
            frm, to = {"stop": (cur, None), "start": (None, cur), "rollback": (cur, prev)}[what]
            rc, models = _switch(c, a.slot, frm, to, wait_lock=a.wait_lock * 60)
            if rc == 0 and what == "rollback":
                _record(a.slot, current={**prev, "models": models, "state": "serving"}, previous=cur,
                        event={"event": "rollback", "to": rec_label(prev), "from": rec_label(cur), "rc": 0})
            elif rc == 0 and cur:
                _record(a.slot, current={**cur, "state": "stopped" if what == "stop" else "serving",
                                         **({"models": models} if models else {})}, keep_previous=True,
                        event={"event": what, "to": rec_label(cur), "rc": 0})
            else:
                _record(a.slot, event={"event": what, "to": rec_label(to or cur), "rc": rc})
            audit(f"recipe-{what}", slot=a.slot, rc=rc, target=rec_label(to or cur), models=models)
            print(("✓ " if rc == 0 else "✗ ") + f"{what} {a.slot}: rc={rc}" + (f", serving {', '.join(models)}" if models else ""))
            _llm_refresh()
            sys.exit(rc)
    except SpindleBusy:
        print(f"✗ slot {a.slot} is busy: {spindle_holder(f'recipe-{a.slot}').get('what')}")
        sys.exit(1)


def cmd_recipe_stop(a):
    _slot_op(a, "stop")


def cmd_recipe_start(a):
    _slot_op(a, "start")


def cmd_recipe_rollback(a):
    _slot_op(a, "rollback")


def recipe_summary(c, live=True):
    """Slots (config, current/previous/history, live answer) and the catalog — CLI status and the UI."""
    rc_ = recipes_cfg(c)
    st = _recipe_state()["slots"]
    slots = {}
    for n, s in rc_["slots"].items():
        cur, prev, hist = slot_records(c, n)
        lv = recipe_live(c, n) if live else {}
        exp = (cur or {}).get("models") or (cur or {}).get("expect") or []
        drift = None
        if lv.get("ok") and exp and not set(exp) & set(lv["models"]):
            drift = f"serving {', '.join(lv['models'])}, but Noctyx last started {rec_label(cur)} ({', '.join(exp)})"
        elif not lv.get("ok") and (cur or {}).get("state") == "serving":
            drift = f"{rec_label(cur)} should be serving but the port does not answer"
        slots[n] = {"head": s["head"], "worker": s.get("worker"), "port": s["port"], "lock": s.get("lock"),
                    "current": cur, "previous": prev, "history": hist[:10], "live": lv, "drift": drift,
                    "prepared": {k: {x: v.get(x) for x in ("sha", "ref", "prepared_ts")} for k, v in
                                 ((st.get(n) or {}).get("prepared") or {}).items()},
                    "recipes": [k for k, v in rc_["catalog"].items() if n in (v.get("slots") or [])],
                    "busy": spindle_holder(f"recipe-{n}").get("what")}
    for r in slots.values():
        for k in ("current", "previous"):
            if r[k]:
                r[k] = {x: v for x, v in r[k].items() if x != "unit_text"}
    try:
        disc = json.loads(DISCOVER_FILE.read_text())
    except Exception:
        disc = {}
    return {"slots": slots, "catalog": rc_["catalog"], "kinds": list(RECIPE_KINDS), "discover": disc}


def cmd_recipe_status(a):
    c = cfg()
    d = recipe_summary(c)
    if a.json:
        print(json.dumps(d, indent=1, default=str))
        return
    for n, s in d["slots"].items():
        lv = s["live"]
        print(f"{n:<9} {s['head']}{'+' + s['worker'] if s['worker'] else ''}:{s['port']}  {rec_label(s['current'])}")
        print(f"          {'serving ' + ', '.join(lv['models']) if lv.get('ok') else 'NOT serving (' + str(lv.get('error') or lv.get('status')) + ')'}"
              + (f"  · previous: {rec_label(s['previous'])}" if s["previous"] else ""))
        if s["drift"]:
            print(f"          ! {s['drift']}")
        if s["busy"]:
            print(f"          busy: {s['busy']}")
    print("catalog:")
    for k, v in d["catalog"].items():
        print(f"  {k:<24} {v['kind']:<8} {v.get('ref', 'main'):<10} {', '.join(v.get('slots') or [])}  {v['repo']}")


def validate_catalog_entry(name, e, c):
    errs = []
    if not _RNAME.match(name or ""):
        errs.append("name: lowercase letters, digits, '.' and '-' (max 48)")
    if not isinstance(e, dict):
        return ["entry must be an object"]
    if not _REPO.match(str(e.get("repo", ""))):
        errs.append("repo: an https:// or ssh:// git URL")
    if e.get("kind") not in RECIPE_KINDS:
        errs.append(f"kind: one of {', '.join(RECIPE_KINDS)}")
    if not _REF.match(str(e.get("ref", "main"))):
        errs.append("ref: a branch, tag or commit sha")
    sl = e.get("slots")
    known = recipes_cfg(c)["slots"]
    if not isinstance(sl, list) or not sl or any(x not in known for x in sl):
        errs.append(f"slots: one or more of {', '.join(known)}")
    for k in ("env", "overrides"):
        if k in e and not isinstance(e[k], dict):
            errs.append(f"{k}: an object")
    if any(not _ENVK.match(k) for k in (e.get("env") or {})):
        errs.append("env: UPPER_CASE names")
    return errs


def cmd_recipe_catalog(a):
    """Add, replace or remove one catalog entry (validated; the site config's recipes block rewritten in place)."""
    c = cfg()
    r = dict(c.get("recipes") or {})
    cat = dict(r.get("catalog") or {})
    if a.remove:
        if a.name not in cat:
            sys.exit(f"no recipe {a.name}")
        using = [n for n, s in recipe_summary(c, live=False)["slots"].items() if (s["current"] or {}).get("recipe") == a.name]
        if using:
            sys.exit(f"{a.name} is deployed on {', '.join(using)}: deploy something else there first")
        cat.pop(a.name)
    else:
        e = json.loads(a.json)
        errs = validate_catalog_entry(a.name, e, c)
        if errs:
            sys.exit("invalid: " + "; ".join(errs))
        cat[a.name] = {k: e[k] for k in ("repo", "ref", "kind", "slots", "env", "overrides") if k in e}
    r["catalog"] = cat
    _write_block("recipes", r)
    audit("recipe-catalog", name=a.name, removed=bool(a.remove))
    print(f"catalog: {'removed' if a.remove else 'saved'} {a.name}")


# ── recipe discovery: public recipe repos that fit this cluster, fastest / newest ──
DISCOVER_FILE = STATE_DIR / "recipe-discover.json"
DISCOVER_QUERIES = ["tensorfold recipe", "dgx-sparks tensorfold", "2x-dgx-sparks", "dual dgx spark tensorfold",
                    "rtx-5090 tensorfold", "rtx 5090 recipe", "m5 ultra tensorfold", "mlx m5 ultra recipe"]
LEADERBOARD = "https://tensorfold.dev/api/benchmarks/v1/results"
_UA = {"User-Agent": "curl/8.5.0 noctyx-recipe-discover", "Accept": "application/json"}


def _http(url, timeout=20, raw=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        b = r.read()
    return b.decode("utf-8", "replace") if raw else json.loads(b)


def _gh_search(q, per_page=40):
    """Unauthenticated repository search (10/min: callers pace it). No token is involved anywhere."""
    u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "sort": "updated", "per_page": per_page})
    return _http(u).get("items") or []


def _raw(full, branch, path):
    try:
        return _http(f"https://raw.githubusercontent.com/{full}/{branch}/{path}", timeout=15, raw=True)
    except Exception:
        return None


def _hw_class(text):
    """What a recipe is built for, from its name, description and recipe.toml: (class, units)."""
    t = text.lower().replace("_", "-")
    n = None
    m = re.search(r"\b([1-8])x[- ]?(?:nvidia[- ])?(?:dgx[- ])?(?:spark|gb10)|\btp[= -]?([2-8])\b", t)
    if m:
        n = int(m.group(1) or m.group(2))
    elif re.search(r"\b(dual|two)[- ](?:nvidia[- ])?(?:dgx[- ])?sparks?\b", t):
        n = 2
    elif re.search(r"\b(three)[- ](?:dgx[- ])?sparks?", t):
        n = 3
    elif re.search(r"\b(four)[- ](?:dgx[- ])?sparks?", t):
        n = 4
    if re.search(r"\bm[345][- ]?(ultra|max|pro)\b|apple[- ]silicon|mac[- ]studio", t) and not re.search(r"spark|gb10", t):
        return "mac", 1
    if re.search(r"spark|gb10", t) and not re.search(r"jetson|thor", t):
        return "gb10", n or 1
    if re.search(r"5090", t) or (re.search(r"sm[_ =\"-]*120\b", t) and not re.search(r"pro[- ]?[0-9]{4}|4090|3090", t)):
        return "rtx5090", 1
    if re.search(r"\bmlx\b|metal", t):
        return "mac", 1
    return "other", n or 1


def _speed_sources(c):
    """{repo full name or HF model id (lower): [{tps, peak, source, hw, ts}]} from the harness ledger
    (measured here) and the tensorfold.dev leaderboard (community runs)."""
    out = {}
    hd = pathlib.Path((c.get("recipes") or {}).get("harness_dir") or "/srv/recipe-harness")
    for f in sorted((hd / "stats").glob("*.json")) if (hd / "stats").is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        tf = hd / "targets" / f"{d.get('target')}.toml"
        keys = set(re.findall(r"github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?[)/\s\"]", (tf.read_text() if tf.exists() else "") + " "))
        runs = [r for r in d.get("runs") or [] if r.get("rulers")]
        for r in runs:
            keys |= set(re.findall(r"github\.com/([\w.-]+/[\w.-]+?)(?:/|$)", (r.get("source") or "") + "/"))
        if not runs or not keys:
            continue
        r = runs[-1]
        rl = [x for v in r["rulers"].values() for x in v if isinstance(x, dict) and x.get("agg")]
        c1 = [x["agg"] for x in rl if x.get("c") == 1]
        cm = max((x.get("c") or 1 for x in rl), default=1)
        rec = {"tps": round(sum(c1) / len(c1), 1) if c1 else None, "peak": max((x["agg"] for x in rl if x.get("c") == cm), default=None),
               "streams": cm, "source": f"harness {d.get('target')}", "measured": True, "hw": r.get("hardware"),
               "ts": r.get("started")}
        for k in keys:
            out.setdefault(k.lower(), []).append(rec)
    try:
        cur, res = None, []
        for _ in range(40):
            j = _http(LEADERBOARD + "?limit=50" + (f"&cursor={urllib.parse.quote(cur)}" if cur else ""))
            res += j.get("results") or []
            cur = j.get("next_cursor")
            if not cur:
                break
        for r in res:
            rc = r.get("receipt") or {}
            mid = ((rc.get("model") or {}).get("repo_id") or "").lower()
            tps = [s["delivery_tps"]["median"] for s in r.get("summary") or [] if (s.get("delivery_tps") or {}).get("median")]
            if not mid or not tps:
                continue
            g = ((rc.get("hardware") or {}).get("gpus") or [{}])[0].get("name") or "?"
            out.setdefault(mid, []).append({"tps": round(sorted(tps)[len(tps) // 2], 1), "source": f"tensorfold.dev ({r.get('display_name')})",
                                            "measured": True, "hw": f"{(rc.get('settings') or {}).get('rank_count', 1)}x {g}",
                                            "ts": r.get("received_at")})
    except Exception as e:
        print(f"  leaderboard: {str(e)[:100]}")
    return out


def _hw_match(cls, units, hw):
    h = (hw or "").lower()
    if cls == "gb10":
        return "gb10" in h or "spark" in h
    if cls == "rtx5090":
        return "5090" in h
    if cls == "mac":
        return "apple" in h or re.search(r"\bm[345]\b", h) is not None
    return False


def recipe_discover(c, limit=80):
    rc_ = recipes_cfg(c)
    slots = rc_["slots"]
    pairs = [n for n, s in slots.items() if s.get("worker")]
    singles = {n for n, s in slots.items() if not s.get("worker")}
    owners = sorted({m.group(1) for v in rc_["catalog"].values() for m in [re.search(r"github\.com/([\w.-]+)/", v.get("repo", ""))] if m})
    seen = {}
    for q in DISCOVER_QUERIES + [f"user:{o}" for o in owners]:
        try:
            for it in _gh_search(q):
                seen.setdefault(it["full_name"], it)
        except Exception as e:
            print(f"  search {q!r}: {str(e)[:100]}")
        time.sleep(6.5)                      # unauthenticated search: 10 requests a minute
    have = {re.sub(r"(\.git)?/*$", "", v.get("repo", "")).lower(): k for k, v in rc_["catalog"].items()}
    speeds = _speed_sources(c)
    items = []
    for full, it in sorted(seen.items(), key=lambda kv: kv[1].get("pushed_at") or "", reverse=True):
        if it.get("archived") or len(items) >= limit:
            continue
        br = it.get("default_branch") or "main"
        toml_ = _raw(full, br, "recipe.toml") or ""
        cls, units = _hw_class(full.split("/", 1)[1])          # the repo's name says the most
        c2, u2 = _hw_class(" ".join([full, it.get("description") or ""]))
        if cls == "other":
            cls, units = c2, u2
        elif c2 == cls and units == 1:
            units = u2                                       # "…-DGX-Spark" named, "four Sparks" described
        if cls == "other" and toml_:
            cls, units = _hw_class(toml_[:4000])
        if cls == "other":
            continue
        has_spark = bool(toml_) and _raw(full, br, "spark/__main__.py") is not None
        has_sys = bool(toml_) and not has_spark and _raw(full, br, "setup.sh") is not None and _raw(full, br, "serve.sh") is not None
        has_script = not (has_spark or has_sys) and _raw(full, br, "start.sh") is not None and _raw(full, br, "stop.sh") is not None
        kind = "spark" if has_spark else "systemd" if has_sys else "script" if has_script else None
        models = re.findall(r'^\s*(?:repo|converted_repo|published_repo)\s*=\s*"([\w.-]+/[\w.-]+)"', toml_, re.M)
        fits, why = [], None
        if cls == "gb10":
            if units == 2 and kind in ("spark", "script"):
                fits = pairs
            elif units > 2:
                n_gb10 = sum(2 for s_ in slots.values() if s_.get("worker"))
                why = f"needs {units} Sparks: Noctyx's slots are pairs" + (f" (this site has {n_gb10} in pairs)" if units <= n_gb10 else "")
            elif units == 1:
                why = "one Spark: no single-GB10 slot (both pairs are TP=2)"
        elif cls == "rtx5090" and kind == "systemd":
            fits = [n for n in singles if n == "rtx5090"]
        elif cls == "mac":
            why = "Apple silicon: the Mac is monitored only, not a deploy slot"
        if not fits and not why:
            why = "no spark/, setup.sh + serve.sh or start.sh + stop.sh: needs an adapter"
        sp = [s for k in [full.lower(), *[m.lower() for m in models]] for s in speeds.get(k, [])
              if _hw_match(cls, units, s.get("hw")) or s["source"].startswith("harness")]
        best = max(sp, key=lambda s: s.get("tps") or 0) if sp else None
        claim = None
        if not best:
            rd = _raw(full, br, "README.md") or ""
            m = re.search(r"(\d{2,4}(?:\.\d+)?)\s*(?:tok/s|tokens/s|t/s|tps)\b", rd)
            if m:
                claim = float(m.group(1))
        items.append({"repo": full, "url": it.get("html_url"), "clone": it.get("clone_url"), "branch": br,
                      "description": (it.get("description") or "")[:200], "stars": it.get("stargazers_count", 0),
                      "pushed_at": it.get("pushed_at"), "fork": it.get("fork"), "hw": cls, "units": units, "kind": kind,
                      "models": models[:3], "slots": fits, "why": why, "speed": best, "claimed_tps": claim,
                      "in_catalog": have.get(f"https://github.com/{full}".lower())})
    return {"ts": time.time(), "items": items, "queries": DISCOVER_QUERIES, "owners": owners}


def discover_rank(x, sort):
    """fastest: measured tok/s (harness, leaderboard) first, README claims after them, unknown last;
    newest: last push. Ties: newest."""
    sp = (x.get("speed") or {}).get("tps")
    if sort == "newest":
        return (0, x.get("pushed_at") or "")
    return ((2, sp) if sp else (1, x.get("claimed_tps")) if x.get("claimed_tps") else (0, 0), x.get("pushed_at") or "")


def cmd_recipe_discover(a):
    c = cfg()
    d = recipe_discover(c)
    DISCOVER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISCOVER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, DISCOVER_FILE)
    rows = sorted(d["items"], key=lambda x: discover_rank(x, a.sort), reverse=True)
    print(f"{len(rows)} recipes for this cluster's hardware (of {len(d['items'])} found), by {a.sort}:")
    for x in rows:
        sp = x["speed"]
        s = f"{sp['tps']:>6} tok/s ({sp['source']})" if sp and sp.get("tps") else \
            (f"{x['claimed_tps']:>6} tok/s (README claim)" if x["claimed_tps"] else "     ? tok/s")
        fit = ("→ " + ", ".join(x["slots"])) if x["slots"] else f"✗ {x['why']}"
        print(f"  {x['repo']:<62} {x['hw']}{'x' + str(x['units']) if x['units'] > 1 else ''}  {(x['pushed_at'] or '')[:10]}  {s}  {fit}"
              + (f"  [catalog: {x['in_catalog']}]" if x["in_catalog"] else ""))
    audit("recipe-discover", found=len(d["items"]), deployable=sum(1 for x in d["items"] if x["slots"]))


# ── the console's display defaults (site config "console") ─────────────────────
CONSOLE_THEMES = ("noctyx", "ember", "mono", "github", "catppuccin", "custom")
_HEXCOL = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_console(s):
    """-> (clean, errors): the display settings every browser without its own starts from."""
    if not isinstance(s, dict):
        return None, ["settings: an object"]
    out, errs = {}, []
    if "mode" in s:
        if s["mode"] not in ("dark", "light", "system"):
            errs.append("mode: dark, light or system")
        out["mode"] = s["mode"]
    if "theme" in s:
        if s["theme"] not in CONSOLE_THEMES:
            errs.append(f"theme: one of {', '.join(CONSOLE_THEMES)}")
        out["theme"] = s["theme"]
    if "tunit" in s:
        if s["tunit"] not in ("F", "C"):
            errs.append("tunit: F or C")
        out["tunit"] = s["tunit"]
    if "custom" in s:
        cu = s["custom"]
        keys = ("bg", "panel", "text", "muted", "accent", "line", "ok", "warn", "bad")
        if not isinstance(cu, dict) or any(k not in keys or not _HEXCOL.match(str(v)) for k, v in cu.items()):
            errs.append("custom: #rrggbb colours for " + ", ".join(keys))
        else:
            out["custom"] = {k: cu[k].lower() for k in keys if k in cu}
    return out, errs


def cmd_user(a):
    """Console accounts. Passwords are read from the terminal (twice), or one line on stdin with
    --password-stdin: never from the command line."""
    import getpass
    from noctyx import accounts as ac

    def pw():
        if a.password_stdin:
            return sys.stdin.readline().rstrip("\n")
        p1 = getpass.getpass("password: ")
        if p1 != getpass.getpass("again: "):
            sys.exit("the passwords differ")
        return p1
    try:
        if a.op == "list":
            us = ac.users()
            for n, u in sorted(us.items()):
                print(f"{n:<20} {u['role']:<9} created {datetime.datetime.fromtimestamp(u.get('created', 0)).date()}"
                      + (f"  api token since {datetime.datetime.fromtimestamp(u['token_created']).date()}" if u.get("token_sha256") else ""))
            print(f"{len(us)} user(s); console mode: "
                  + ("accounts (sign-in required)" if us and cfg()["server"].get("console_auth", True) is not False
                     else "open access (sign-in optional)" if cfg()["server"].get("console_auth", True) is False
                     else "token"))
            return
        if not a.name:
            sys.exit("--name is required")
        if a.op == "add":
            errs = ac.check_new(a.name, a.role)
            if errs:
                sys.exit("; ".join(errs))
            ac.add(a.name, a.role, pw())
        elif a.op == "passwd":
            ac.set_password(a.name, pw())
        elif a.op == "role":
            ac.set_role(a.name, a.role)
        elif a.op == "del":
            ac.delete(a.name)
        elif a.op == "token":
            if a.revoke:
                had = ac.revoke_token(a.name)
                audit("user-token-revoke", name=a.name)
                print(f"api token of {a.name}: {'revoked' if had else 'there was none'}")
                return
            tok = ac.make_token(a.name)
            audit("user-token", name=a.name)       # never the token itself
            print(f"api token for {a.name} (shown once; any earlier one stops working):\n{tok}\n"
                  f"use it as:  Authorization: Bearer <token>   (acts as {a.name}, with {a.name}'s role)", file=sys.stderr)
            return
    except ValueError as e:
        sys.exit(str(e))
    audit(f"user-{a.op}", name=a.name, role=a.role if a.op in ("add", "role") else None)
    print(f"user {a.op}: {a.name}" + (f" ({a.role})" if a.op in ("add", "role") else ""))


def cmd_console_set(a):
    new, errs = validate_console(json.loads(a.json))
    if errs:
        sys.exit("invalid: " + "; ".join(errs))
    _write_block("console", {"settings": new})
    audit("console-set", **{k: v for k, v in new.items() if k != "custom"})
    print(f"console defaults: {new.get('theme', '?')} · {new.get('mode', '?')} · °{new.get('tunit', 'F')}")


# ── the UI's assistant LLM (the site config `assistant`) ───────────────────────
ASSISTANT_KEYS = ("enabled", "url", "model", "timeout_seconds", "fallback", "key_host")
_URL = re.compile(r"^https?://[A-Za-z0-9.:\[\]_-]+(/[A-Za-z0-9._~/-]*)?$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


def validate_assistant(a):
    """-> (clean, errors). url/model/timeout as the UI's assistant uses them; key_host names a
    file in the llm_keys directory (the key itself never passes through here)."""
    if not isinstance(a, dict):
        return None, ["assistant: an object"]
    errs, out = [], {}
    for k in ("enabled", "fallback"):
        if k in a:
            if not isinstance(a[k], bool):
                errs.append(f"{k}: true or false")
            out[k] = a[k]
    # only the keys given: assistant-set merges them over the stored block ("" clears url / model)
    url = str(a.get("url") or "").strip().rstrip("/")
    if url and not _URL.match(url):
        errs.append("url: http(s)://host:port/v1")
    if "url" in a:
        out["url"] = url
    m = str(a.get("model") or "").strip()
    if m and not _MODEL.match(m):
        errs.append("model: the served model id (letters, digits, . _ : / @ + -)")
    if "model" in a:
        out["model"] = m
    if "timeout_seconds" in a:
        to = a["timeout_seconds"]
        if type(to) is not int or not 5 <= to <= 600:
            errs.append("timeout_seconds: 5..600")
        out["timeout_seconds"] = to
    if "key_host" in a:      # "" clears it
        kh = a["key_host"] or ""
        if kh and not re.match(r"^[A-Za-z0-9._-]{1,64}$", str(kh)):
            errs.append("key_host: a key file name")
        out["key_host"] = kh
    if out.get("enabled") and "url" in a and not url and out.get("fallback") is False:
        errs.append("enabled with no url and no fallback: nothing to ask")
    return out, errs


def cmd_assistant_set(a):
    c = cfg()
    new, errs = validate_assistant(json.loads(a.json))
    if errs:
        sys.exit("invalid: " + "; ".join(errs))
    old = c.get("assistant") or {}
    blk = {k: v for k, v in old.items() if k.startswith("_")}
    blk.update({k: v for k, v in {**old, **new}.items() if k in ASSISTANT_KEYS and v not in (None, "")})
    if blk.get("enabled") and not blk.get("url") and blk.get("fallback") is False:
        sys.exit("invalid: enabled with no url and no fallback: nothing to ask")
    _write_block("assistant", blk, lines=True)
    audit("assistant-set", **{k: blk.get(k) for k in ("enabled", "url", "model", "timeout_seconds", "fallback")})
    print(f"assistant: {'on' if blk.get('enabled') else 'off'} · {blk.get('url') or '(discovered endpoints only)'}"
          f" · model {blk.get('model') or '(as served)'} · fallback {'on' if blk.get('fallback', True) else 'off'}")


# ── head-node setup and the nightly run ─────────────────────────────────────
def cmd_nightly(a):
    """Maintenance after the backup window: mirror harvest, recipe discovery; on Sundays the retention
    dry-run and verify. Each step runs in its own process so one failure never skips the rest."""
    me = [sys.executable, str(pathlib.Path(__file__).resolve())]
    env = {**os.environ, "NOCTYX_FOREGROUND": "1"}
    steps = [["hf-harvest"], ["recipe-discover"]]
    if datetime.datetime.now().isoweekday() == 7:
        steps += [["prune"], ["verify"]]
    print(f"=== noctyx nightly {datetime.datetime.now().isoformat(timespec='seconds')} ===", flush=True)
    bad = 0
    for s in steps:
        rc = subprocess.run(me + s, env=env, stdout=subprocess.DEVNULL if s == ["recipe-discover"] else None).returncode
        if rc:
            bad += 1
            print(f"WARN {' '.join(s)} failed (rc={rc})", flush=True)
    print(f"=== done {datetime.datetime.now().isoformat(timespec='seconds')} ===", flush=True)
    sys.exit(1 if bad else 0)


HEAD_UNITS = ("console.service", "directory.service", "hf-mirror.service", "tick.service", "tick.timer",
              "nightly.service", "nightly.timer", "retention.service", "retention.timer",
              "hygiene.service", "hygiene.timer")


def site_values(c):
    """Template values that come from the site config rather than paths.py (units, tools, the restore image)."""
    hc = hf_cfg(c)
    s = c["server"]
    head = next((h for h in c.get("hosts", []) if h.get("address") == s.get("address")), {})
    return {"NX_PY": sys.executable, "NX_PKG_PARENT": str(NX_HOME.parent), "NX_CONFIG": CFG_PATH,
            "UI_PORT": str(s.get("ui_port", 20996)), "HF_STORE": hc["store"], "HF_PORT": str(hc["port"]),
            "CONSOLE_AUTH": "0" if s.get("console_auth", True) is False else "1",
            "REPO_ROOT": s.get("repo_root") or "/", "HEAD_ADDR": s.get("address") or "",
            "HEAD_HOST": head.get("name") or "", "RESTIC_VERSION": str(c.get("restic_version") or "0.19.1"),
            "REST_PORT": str(s.get("rest_port", 20998)), "TLS_CERT": s.get("tls", {}).get("cert", ""),
            "TLS_KEY": s.get("tls", {}).get("key", ""), "NAS_REPO_ROOT": (s.get("nas_ssh") or {}).get("repo_root", "")}


def render_site(c, text):
    text = render(text)
    for k, v in site_values(c).items():
        text = text.replace(f"@{k}@", v)
    return text


def render_unit(c, name):
    return render_site(c, (NX_HOME / "units" / name).read_text())


def cmd_install_head(a):
    """Render the head node's units (console, directory, mirror, the four timers) for this site; with
    --apply write them to /etc/systemd/system and enable the timers and services."""
    c = cfg()
    out = {f"{UNIT}-{n}": render_unit(c, n) for n in HEAD_UNITS}
    if not a.apply:
        for n, txt in out.items():
            print(f"### /etc/systemd/system/{n}\n{txt}")
        print("(dry run: nothing written — --apply installs and enables them)")
        return
    for n, txt in out.items():
        pathlib.Path(f"/etc/systemd/system/{n}").write_text(txt)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    enable = [n for n in out if n.endswith(".timer")] + [f"{UNIT}-{n}" for n in ("console.service", "directory.service", "hf-mirror.service")]
    subprocess.run(["systemctl", "enable", "--now", *enable], check=False)
    audit("install-head", units=list(out))
    print(f"installed and enabled: {', '.join(enable)}")


# ── first-time setup of a site: service user, TLS, repositories, escrow, the endpoint ──────────
def _run(argv, **kw):
    r = subprocess.run(argv, capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit(f"{argv[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def _owner_exists():
    import pwd
    try:
        pwd.getpwnam(P["repo_owner"])
        return True
    except KeyError:
        return False


def setup_owner(c):
    """The local user that owns the repositories on the NAS mount and runs the endpoint and the directory.
    server.repo_owner_uid pins its uid: a NAS's NFS share ACLs often refuse unknown uids, even as owner."""
    if not _owner_exists():
        uid = c["server"].get("repo_owner_uid")
        _run(["useradd", "--system", *(["--uid", str(uid)] if uid else []), "--home-dir", P["state"],
              "--shell", "/usr/sbin/nologin", P["repo_owner"]])
        print(f"  created user {P['repo_owner']}")
    for d, mode in ((P["etc"], 0o750), (P["repo_keys"], 0o700), (P["repo_creds"], 0o700), (P["state"], 0o755)):
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)
        os.chmod(d, mode)
    shutil.chown(P["state"], P["repo_owner"], P["repo_owner"])
    shutil.chown(P["etc"], "root", P["repo_owner"])
    print(f"  {P['repo_owner']}: ok; {P['etc']}, {P['repo_keys']}, {P['repo_creds']}, {P['state']}")


def setup_tls(c, force=False):
    """A small CA and the head's server certificate (endpoint, directory, console). Never replaces an
    existing CA without --force: every node trusts it, and a new one breaks them all until re-installed."""
    tls = c["server"]["tls"]
    crt, key, ca = pathlib.Path(tls["cert"]), pathlib.Path(tls["key"]), pathlib.Path(tls["ca"])
    ca_key = ca.with_name("ca.key")
    ca.parent.mkdir(parents=True, exist_ok=True)
    s = c["server"]
    if ca_key.exists() and not force:
        print(f"  CA exists ({ca}): kept (--force replaces it, and every node must then be re-installed)")
    else:
        _run(["openssl", "genrsa", "-out", str(ca_key), "4096"])
        os.chmod(ca_key, 0o600)
        _run(["openssl", "req", "-x509", "-new", "-nodes", "-key", str(ca_key), "-sha256", "-days", "3650",
              "-subj", "/O=Noctyx/CN=Noctyx site CA", "-out", str(ca)])
        print(f"  CA made: {ca}")
        force = True
    if force or not crt.exists() or subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-checkend",
                                                    str(30 * 86400)], capture_output=True).returncode:
        san = [f"IP:{s['address']}", f"DNS:{s.get('hostname') or 'head'}", "DNS:localhost", "IP:127.0.0.1",
               *[str(x) for x in (tls.get("san") or [])]]
        ext = crt.with_suffix(".ext")
        ext.write_text("basicConstraints = CA:FALSE\nkeyUsage = critical, digitalSignature, keyEncipherment\n"
                       f"extendedKeyUsage = serverAuth\nsubjectAltName = {','.join(dict.fromkeys(san))}\n")
        csr = crt.with_suffix(".csr")
        _run(["openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes", "-keyout", str(key),
              "-subj", f"/O=Noctyx/CN={s.get('hostname') or 'head'}", "-out", str(csr)])
        # 825 days: the longest a modern TLS client accepts for a leaf
        _run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
              "-days", "825", "-sha256", "-extfile", str(ext), "-out", str(crt)])
        csr.unlink(); ext.unlink()
        print(f"  server certificate: {crt} ({', '.join(dict.fromkeys(san))})")
    else:
        print(f"  server certificate valid for 30+ days: kept ({crt})")
    os.chmod(ca.parent, 0o750)
    for f in ca.parent.glob("*.crt"):
        os.chmod(f, 0o644)
    if _owner_exists():          # the endpoint and the directory run as the repository owner
        shutil.chown(ca.parent, "root", P["repo_owner"]); shutil.chown(key, "root", P["repo_owner"])
        os.chmod(key, 0o640)
    else:
        os.chmod(key, 0o600)
        print(f"  note: no user {P['repo_owner']} yet; run `noctyx setup owner`, then this again")
    # the CA is public: put it where curl and friends look, so --cacert is not needed on the head
    anchor = pathlib.Path(f"/usr/local/share/ca-certificates/{P['unit_prefix']}-ca.crt")
    if anchor.parent.is_dir():
        shutil.copyfile(ca, anchor); os.chmod(anchor, 0o644)
        subprocess.run(["update-ca-certificates"], capture_output=True)
    end = _run(["openssl", "x509", "-in", str(crt), "-noout", "-enddate"]).stdout.strip().split("=", 1)[-1]
    print(f"  the certificate expires {end}: `noctyx setup tls` again renews it (the CA stays)")


def setup_repos(c):
    """One restic repository per entry in "repositories": its key (32 random bytes), its endpoint login
    (hex: it goes into a rest: URL, where base64's + / = break parsing) and a bcrypt htpasswd line
    (rest-server refuses htpasswd's default MD5-crypt and every request 401s). Existing ones are kept."""
    for tool in ("restic", "htpasswd"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not installed" + (" (apt install apache2-utils)" if tool == "htpasswd" else ""))
    if not _owner_exists():
        sys.exit(f"no user {P['repo_owner']}: run `noctyx setup owner` first")
    root = pathlib.Path(repo_root(c))
    if not root.is_dir():
        sys.exit(f"repo_root {root} is not there: is the NAS mounted?")
    htp = pathlib.Path(P["etc"]) / "htpasswd"
    if not htp.exists():
        htp.touch(mode=0o640)
    shutil.chown(htp, "root", P["repo_owner"]); os.chmod(htp, 0o640)
    made = kept = 0
    for r in c["repositories"]:
        rid, user = r["id"], f"{P['repo_dir_prefix']}{r['id']}"
        path = pathlib.Path(repo_dir(c, rid))
        keyf = pathlib.Path(P["repo_keys"]) / f"{rid}.key"
        passf = pathlib.Path(P["repo_creds"]) / f"{user}.pass"
        if not keyf.exists():
            fd = os.open(keyf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, (base64.b64encode(os.urandom(32)).decode() + "\n").encode()); os.close(fd)
        if (path / "config").exists():
            kept += 1
        else:
            # as the repository owner (an NFS share often squashes root); the root-only key goes in the
            # environment (readable by that user and root only), not a file the owner could not open
            _run(["setpriv", f"--reuid={P['repo_owner']}", f"--regid={P['repo_owner']}", "--clear-groups",
                  "sh", "-c", 'mkdir -p "$1" && exec restic init --repo "$1"', "_", str(path)],
                 env={**os.environ, "RESTIC_PASSWORD": keyf.read_text().strip(), "HOME": "/tmp"})
            made += 1
            print(f"  {rid}: repository made at {path}")
        if not passf.exists():
            fd = os.open(passf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, os.urandom(24).hex().encode()); os.close(fd)
        if not any(l.split(":", 1)[0] == user for l in htp.read_text().splitlines()):
            # -i: the password on stdin, never a command line; no trailing newline, or it is hashed too
            r_ = subprocess.run(["htpasswd", "-B", "-i", str(htp), user], input=passf.read_text().strip(),
                                capture_output=True, text=True)
            if r_.returncode:
                sys.exit(f"htpasswd {user}: {r_.stderr.strip()}")
    audit("setup-repos", made=made, kept=kept)
    print(f"  repositories: {made} made, {kept} already there")
    if made:
        print("  NEXT: `noctyx setup escrow` and keep the bundle OFF this machine. Without these keys every\n"
              "  backup is unreadable, permanently.")


def setup_escrow(c):
    """An encrypted bundle of every key and credential (AES-256, PBKDF2), its passphrase typed by you and
    stored nowhere. Copy it off this machine and off the NAS."""
    out = pathlib.Path(P["etc"]) / "escrow"
    out.mkdir(mode=0o700, exist_ok=True)
    bundle = out / f"noctyx-keys-{datetime.date.today():%Y%m%d}.tar.gz.enc"
    etc = pathlib.Path(P["etc"])
    parts = [str(pathlib.Path(p).relative_to(etc)) for p in (P["repo_keys"], P["repo_creds"], P["tls"],
                                                              f"{P['etc']}/htpasswd")
             if pathlib.Path(p).exists() and pathlib.Path(p).is_relative_to(etc)]
    tar = subprocess.Popen(["tar", "-C", str(etc), "-czf", "-", *parts], stdout=subprocess.PIPE)
    enc = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-out", str(bundle)],
                         stdin=tar.stdout)      # openssl asks for the passphrase on the terminal, twice
    tar.wait()
    if enc.returncode or tar.returncode:
        sys.exit("escrow failed")
    os.chmod(bundle, 0o600)
    audit("setup-escrow", bundle=str(bundle))
    print(f"  {bundle}\n  1. copy it off this machine and off the NAS\n"
          f"  2. check it opens somewhere else:  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in {bundle.name} | tar tz")


def setup_rest_server(c):
    """The repository endpoint on the head (when server.rest_host is empty or the head's own address).
    On a NAS it is `noctyx nas-rest deploy` instead."""
    if rest_on_nas(c):
        sys.exit(f"the endpoint runs on the NAS ({c['server']['rest_host']}): use `noctyx nas-rest deploy`")
    if not shutil.which("rest-server") and not pathlib.Path("/usr/local/bin/rest-server").exists():
        sys.exit("rest-server is not installed: put the release binary at /usr/local/bin/rest-server "
                 "(github.com/restic/rest-server/releases: the asset names use underscores, rest-server_<v>_linux_amd64)")
    name = f"{UNIT}-rest-server.service"
    pathlib.Path(f"/etc/systemd/system/{name}").write_text(render_unit(c, "rest-server.service"))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", name], check=False)
    time.sleep(2)
    up = rest_up(c)
    audit("setup-rest-server", up=up)
    print(f"  {name}: endpoint {rest_url(c)} {'UP' if up else 'DOWN: journalctl -u ' + name}")


def setup_status(c):
    """Where a site is in its setup, and the next step."""
    s, tls = c["server"], c["server"]["tls"]
    def crt_ok(f):
        return pathlib.Path(f).exists() and not subprocess.run(
            ["openssl", "x509", "-in", f, "-noout", "-checkend", str(30 * 86400)], capture_output=True).returncode
    repos = c.get("repositories") or []
    inited = [r["id"] for r in repos if pathlib.Path(repo_dir(c, r["id"]), "config").exists()]
    escrow = sorted((pathlib.Path(P["etc"]) / "escrow").glob("*.enc")) if (pathlib.Path(P["etc"]) / "escrow").is_dir() else []
    newest = max((f.stat().st_mtime for f in escrow), default=0)
    kd = pathlib.Path(P["repo_keys"])
    stale = [f for f in (kd.glob("*.key") if escrow and kd.is_dir() else []) if f.stat().st_mtime > newest]
    steps = [
        ("owner", _owner_exists(), f"the repository owner {P['repo_owner']}", "noctyx setup owner"),
        ("tls", pathlib.Path(tls["ca"]).exists() and crt_ok(tls["cert"]), "the site CA and the head's certificate", "noctyx setup tls"),
        ("repos", bool(repos) and len(inited) == len(repos), f"repositories ({len(inited)}/{len(repos)} made)", "noctyx setup repos"),
        ("escrow", bool(escrow) and not stale, "an escrow bundle of the keys" + (f" (newest {escrow[-1].name})" if escrow else "")
         + (f": {len(stale)} key(s) made since, re-run it" if stale else ""), "noctyx setup escrow"),
        ("endpoint", rest_up(c), f"the repository endpoint {rest_url(c)}",
         "noctyx nas-rest deploy" if rest_on_nas(c) else "noctyx setup rest-server"),
        ("units", bool(shutil.which("systemctl")) and subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{UNIT}-console.service"]).returncode == 0,
         "the head's services (console, directory, mirror, timers)", "noctyx install-head --apply"),
    ]
    nxt = None
    for name, ok, what, cmd in steps:
        print(f"  {'✓' if ok else '·'} {name:<9} {what}")
        nxt = nxt or (None if ok else cmd)
    print(f"\nnext: sudo {nxt}" if nxt else "\nthe head is set up: add nodes with  sudo noctyx install-agent --host <name>")


def cmd_setup(a):
    c = cfg()
    if a.step != "status" and os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    {"status": lambda: setup_status(c), "owner": lambda: setup_owner(c), "tls": lambda: setup_tls(c, a.force),
     "repos": lambda: setup_repos(c), "escrow": lambda: setup_escrow(c),
     "rest-server": lambda: setup_rest_server(c)}[a.step]()


SCHED_STATE = STATE_DIR / "schedule-state.json"


def _sched_state():
    try:
        return json.loads(SCHED_STATE.read_text())
    except Exception:
        return {}


def _sched_plan(c, state):
    sch = c.get("schedule") or {}
    slack = int(sch.get("tick_minutes", 15)) * 60
    retry = parse_duration(sch.get("retry_after", "1h"))
    last, errors = last_success(c)
    # a repo we could not read (NAS down/hung: the 2026-09-27 outage) says
    # NOTHING about its hosts — treating them as "never backed up" would launch
    # a backup of every host into a dead NAS the moment the window opens
    unreadable = {e.split(":", 1)[0] for e in errors}
    now, local_dt = time.time(), datetime.datetime.now()
    rows = []
    for h in backup_hosts(c):
        for prof, rule in effective_schedule(c, h["name"]).items():
            k = f"{h['name']}/{prof}"
            l = last.get((h["name"], prof))
            att = state.get(k) or {}
            failed_at = att["ts"] if att.get("ok") is False and (l is None or att["ts"] > l) else None
            if prof not in c.get("profiles", {}):
                ok, why = False, f"unknown profile '{prof}'"
            elif h.get("repo") in unreadable:
                ok, why = False, f"repo {h['repo']} unreadable this tick — not deciding blind"
            else:
                ok, why = due(now, local_dt, l, rule["every"], rule["window"], failed_at, retry, slack)
            rows.append({"host": h["name"], "profile": prof, "last": l, "due": ok, "why": why,
                         "every": rule["every"], "window": rule["window"],
                         "age": (now - l) if l else float("inf"),
                         "next": next_due(now, l, rule["every"], rule["window"], failed_at, retry, slack),
                         "health": health(now, l, rule["every"], rule["window"], slack)})
    return rows, errors


def cmd_tick(a):
    """Run whatever the schedule says is due, most overdue first, serially
    (one hot spindle). Meant for the tick timer; `--dry-run` = plan."""
    c = cfg()
    if not a.dry_run:
        ensure_rest(c)
    state = _sched_state()
    rows, errors = _sched_plan(c, state)
    now = time.time()
    print(f"Noctyx tick {datetime.datetime.now().isoformat(timespec='seconds')}")
    for e in errors:
        print(f"  WARN cannot read repo {e} — its hosts look 'never backed up' this tick")
    for r in rows:
        last = f"{fmt_duration(now - r['last'])} ago" if r["last"] else "never"
        nxt = "now" if r["due"] else time.strftime("%m-%d %H:%MZ", time.gmtime(r["next"]))
        print(f"  {'DUE ' if r['due'] else '    '} {r['host']:<10} {r['profile']:<9} "
              f"last {last:<12} every {fmt_duration(r['every']):<7} next {nxt:<12} "
              f"{r['health']:<6} {r['why']}")
    if a.dry_run:
        return
    queue = sorted((r for r in rows if r["due"]), key=lambda r: -r["age"])
    if not queue:
        return
    _sweep_root_owned_repo_files(c)
    failed = 0
    for r in queue:
        # re-evaluate: earlier items may have run for hours (window closed?)
        fresh = {(x["host"], x["profile"]): x for x in _sched_plan(c, state)[0]}
        cur = fresh.get((r["host"], r["profile"]))
        if not cur or not cur["due"]:
            print(f"  SKIP {r['host']}/{r['profile']}: no longer due ({cur and cur['why']})")
            continue
        k = f"{r['host']}/{r['profile']}"
        audit("tick-start", host=r["host"], profile=r["profile"], why=cur["why"])
        try:
            ok = _run_backup(c, host_by_name(c, r["host"]), r["profile"], False, False)
        except SpindleBusy as e:
            print(f"  SKIP {r['host']}: {e} — retried next tick")
            continue
        state[k] = {"ts": time.time(), "ok": bool(ok)}
        failed += 0 if ok else 1
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SCHED_STATE.write_text(json.dumps(state, indent=1))
        except Exception as e:
            print(f"  WARN cannot persist schedule state: {e}")
    sys.exit(1 if failed else 0)


def main():
    p = argparse.ArgumentParser(prog="noctyx", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("hosts")
    b = sub.add_parser("backup"); b.add_argument("--host"); b.add_argument("--profile", default="system")
    b.add_argument("--dry-run", action="store_true"); b.add_argument("--force-low-net", action="store_true")
    s = sub.add_parser("snapshots"); s.add_argument("--host"); s.add_argument("--limit", type=int)
    v = sub.add_parser("verify"); v.add_argument("--host"); v.add_argument("--subset")
    pr = sub.add_parser("prune", help="retention on the NAS repos (dry-run unless --apply)")
    pr.add_argument("--host"); pr.add_argument("--apply", action="store_true")
    pr.add_argument("--forget", action="append", metavar="ID",
                    help="forget these snapshot ids instead of applying the policy (repeatable)")
    hy = sub.add_parser("hygiene", help="host housekeeping report; --fix auto|manual|ID,ID applies fixes")
    hy.add_argument("--host"); hy.add_argument("--fix")
    rs = sub.add_parser("restore", help="restore a path from a snapshot to staging (NAS), the host, or in place")
    rs.add_argument("--host", required=True); rs.add_argument("--snapshot", default="latest")
    rs.add_argument("--path", required=True); rs.add_argument("--to", choices=["staging", "host"], default="staging")
    rs.add_argument("--in-place", action="store_true"); rs.add_argument("--yes", action="store_true")
    cp = sub.add_parser("copy"); cp.add_argument("--host"); cp.add_argument("--dry-run", action="store_true")
    sub.add_parser("capacity")
    pp = sub.add_parser("probe"); pp.add_argument("--host")
    i = sub.add_parser("install-agent"); i.add_argument("--host"); i.add_argument("--profile", default="system")
    i.add_argument("--with-client-timer", action="store_true")
    d = sub.add_parser("directory"); d.add_argument("--regen", action="store_true")
    t = sub.add_parser("tick", help="run scheduled backups that are due (timer); --dry-run shows the plan")
    t.add_argument("--dry-run", action="store_true")
    ss = sub.add_parser("schedule-set", help="change how often a profile is snapshotted")
    ss.add_argument("--profile", required=True); ss.add_argument("--host")
    ss.add_argument("--every", help="interval: 15m..90d, e.g. 6h, 1d")
    ss.add_argument("--window", help="HH:MM-HH:MM start window (head node local time), or 'any'")
    g = ss.add_mutually_exclusive_group()
    g.add_argument("--disable", action="store_true", help="stop scheduling this profile (for --host, or fleet-wide)")
    g.add_argument("--reset", action="store_true", help="drop --host's override, back to the default")
    nr = sub.add_parser("nas-rest", help="the repository endpoint running on the NAS")
    nr.add_argument("action", choices=["deploy", "start", "stop", "restart", "status"])
    rb = sub.add_parser("reboot", help="reboot a host (refused while a backup/restore/dpkg runs there)")
    rb.add_argument("--host", required=True); rb.add_argument("--yes", action="store_true")
    rb.add_argument("--plan", action="store_true", help="what blocks it and what goes down; changes nothing")
    rb.add_argument("--finish", action="store_true", help="post-boot half of a head-node reboot")
    ps_ = sub.add_parser("policy-set", help="replace backup_policy (+retention) from JSON, then apply")
    ps_.add_argument("--json", required=True)
    pa = sub.add_parser("policy-apply", help="push the backup policy (exclusions, dump hook) to hosts")
    pa.add_argument("--host"); pa.add_argument("--no-scan", action="store_true")
    psh = sub.add_parser("policy-show", help="print the files policy-apply would write"); psh.add_argument("--host")
    du = sub.add_parser("dumps", help="dump every discovered database now (no backup)"); du.add_argument("--host")
    hfh = sub.add_parser("hf-harvest", help="copy new Hugging Face model files from the hosts' caches into the mirror store")
    hfh.add_argument("--host"); hfh.add_argument("--repo", help="only repos whose dir name contains this")
    hfh.add_argument("--ignore-window", action="store_true", help="run inside the backup window / while the NAS is busy")
    rd = sub.add_parser("recipe-deploy", help="deploy a catalog recipe to a slot (prepare while serving, switch, roll back on failure)")
    rd.add_argument("--slot", required=True); rd.add_argument("--recipe", required=True)
    rd.add_argument("--ref", help="branch, tag or commit (default: the catalog's ref)")
    rd.add_argument("--prepare-only", action="store_true", help="check out, build and download; do not switch")
    rops = [rd]
    for nm, hl in (("recipe-stop", "stop what a slot serves"), ("recipe-start", "start a slot's current deployment again"),
                   ("recipe-rollback", "switch a slot back to its previous deployment")):
        sp = sub.add_parser(nm, help=hl); sp.add_argument("--slot", required=True); rops.append(sp)
    for sp in rops:
        sp.add_argument("--yes", action="store_true", help="really do it (without: print the plan)")
        sp.add_argument("--wait-lock", type=int, default=0, metavar="MIN", help="wait this long for the GPU lock (default: fail at once)")
    rst = sub.add_parser("recipe-status", help="slots, what they serve, and the catalog"); rst.add_argument("--json", action="store_true")
    rcat = sub.add_parser("recipe-catalog", help="add/replace (--json) or --remove a catalog recipe")
    rcat.add_argument("--name", required=True); g2 = rcat.add_mutually_exclusive_group(required=True)
    g2.add_argument("--json"); g2.add_argument("--remove", action="store_true")
    rdi = sub.add_parser("recipe-discover", help="search GitHub for recipes that fit this cluster; rank by measured speed or recency")
    rdi.add_argument("--sort", choices=["fastest", "newest"], default="fastest")
    us_ = sub.add_parser("user", help="console accounts: add | del | passwd | role | token | list")
    us_.add_argument("op", choices=["add", "del", "passwd", "role", "token", "list"]); us_.add_argument("--name")
    us_.add_argument("--revoke", action="store_true", help="token: remove the user's API token")
    us_.add_argument("--role", choices=["viewer", "operator", "admin"], default="viewer")
    us_.add_argument("--password-stdin", action="store_true", help="read the password as one line on stdin")
    stp = sub.add_parser("setup", help="first-time setup of the head, step by step (no step: where the site is, and what is next)")
    stp.add_argument("step", nargs="?", default="status", choices=["status", "owner", "tls", "repos", "escrow", "rest-server"])
    stp.add_argument("--force", action="store_true", help="tls: replace the CA too (every node must then be re-installed)")
    css = sub.add_parser("console-set", help="the console's display defaults (JSON: mode, theme, tunit, custom)")
    css.add_argument("--json", required=True)
    sub.add_parser("nightly", help="maintenance after the backup window (mirror harvest, recipe discovery, weekly prune dry-run + verify)")
    ih = sub.add_parser("install-head", help="render the head node's systemd units for this site (--apply installs them)")
    ih.add_argument("--apply", action="store_true")
    asg = sub.add_parser("assistant-set", help="set the UI assistant's LLM endpoint (JSON: enabled, url, model, timeout_seconds, fallback)")
    asg.add_argument("--json", required=True)
    f = sub.add_parser("follow", help="stream a detached job's log until it ends")
    f.add_argument("--unit", required=True); f.add_argument("--log", required=True)
    for sp in (b, v, pr, cp, rs, hy, rb, hfh, *rops):
        sp.add_argument("--foreground", action="store_true",
                        help="run in this process instead of a detached systemd unit")
        sp.add_argument("--no-follow", action="store_true",
                        help="start the detached job and return immediately")

    a = p.parse_args()
    if a.cmd in DETACHABLE and not (_inline() or a.foreground or (a.cmd == "reboot" and (a.plan or not (a.yes or a.finish)))
                                     or (a.cmd.startswith("recipe-") and a.cmd != "recipe-start"
                                         and not (a.yes or getattr(a, "prepare_only", False)))):
        detach(a)
    handler = {"status": cmd_status, "hosts": cmd_hosts, "backup": cmd_backup, "snapshots": cmd_snapshots,
               "verify": cmd_verify, "prune": cmd_prune, "copy": cmd_copy, "capacity": cmd_capacity,
               "probe": cmd_probe, "install-agent": cmd_install_agent, "directory": cmd_directory,
               "tick": cmd_tick, "follow": cmd_follow, "schedule-set": cmd_schedule_set,
               "nas-rest": cmd_nas_rest, "restore": cmd_restore, "hygiene": cmd_hygiene,
               "reboot": cmd_reboot, "policy-set": cmd_policy_set, "policy-apply": cmd_policy_apply,
               "policy-show": cmd_policy_show, "dumps": cmd_dumps, "hf-harvest": cmd_hf_harvest,
               "recipe-deploy": cmd_recipe_deploy, "recipe-stop": cmd_recipe_stop, "recipe-start": cmd_recipe_start,
               "recipe-rollback": cmd_recipe_rollback, "recipe-status": cmd_recipe_status,
               "recipe-catalog": cmd_recipe_catalog, "recipe-discover": cmd_recipe_discover, "nightly": cmd_nightly, "install-head": cmd_install_head, "console-set": cmd_console_set, "user": cmd_user, "setup": cmd_setup, "assistant-set": cmd_assistant_set}[a.cmd]
    rc = 0
    try:
        handler(a)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        rc = 1
        raise
    finally:
        # the follower's only source of truth for how a detached job ended
        if os.environ.get("NOCTYX_DETACHED"):
            print(f"NOCTYX_EXIT {rc}", flush=True)


def cmd_status(a):
    c = cfg()
    n = c["network"]
    print(f"Noctyx @ {c['server']['hostname']} ({c['server']['address']})")
    print(f"  repos : {', '.join(r['id'] for r in c['repositories'])}")
    print(f"  hosts : {len(hosts(c))} enabled")
    if n.get("degraded"):
        print(f"  NETWORK DEGRADED: {n['degraded_reason']}")
        print(f"            expected {n['expected_client_to_server_mbit']} Mbit/s, "
              f"measured {n['measured_client_to_server_mbit']} Mbit/s")
    recent = []
    if AUDIT.exists():
        recent = [json.loads(l) for l in AUDIT.read_text().splitlines()[-8:]]
    print("  last jobs:")
    for r in recent or [{"event": "none"}]:
        print(f"    {r.get('ts','')} {r.get('host',''):<10} {r.get('event','')} "
              f"rc={r.get('rc','')} snap={r.get('snapshot','')}")


if __name__ == "__main__":
    main()
