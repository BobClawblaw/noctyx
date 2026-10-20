"""What differs between a Linux head node and a macOS one, in one place.

Linux: systemd units and transient units for jobs, useradd, setpriv, update-ca-certificates.
macOS: launchd daemons (made from the same unit templates), dscl, a setuid trampoline, the System keychain;
detached jobs are their own session with a pid file instead of a transient unit.

Everything else in Noctyx calls these and does not test the platform itself.
"""
import os
import pathlib
import platform
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
LAUNCHD_DIR = pathlib.Path("/Library/LaunchDaemons")
SYSTEMD_DIR = pathlib.Path("/etc/systemd/system")
ROOT_GROUP = "wheel" if IS_MAC else "root"


def os_name():
    return "macos" if IS_MAC else "linux"


def arch():
    """aarch64 | x86_64, as the site config spells them (macOS says arm64)."""
    m = platform.machine()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)


def go_arch():
    """amd64 | arm64, as restic's and rest-server's release assets spell them."""
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(arch(), arch())


def primary_address():
    """The LAN address this machine reaches its default route from (nothing is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("198.51.100.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def short_hostname():
    if IS_MAC:
        r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().lower()
    return socket.gethostname().split(".")[0].lower()


def port_free(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ── the repository owner: a system user ─────────────────────────────────────
def create_system_user(name, uid=None, home="/var/empty"):
    if IS_MAC:
        uid = uid or _free_mac_id()
        gid = _mac_group(name, uid)
        u = f"/Users/{name}"
        for k, v in (("UserShell", "/usr/bin/false"), ("RealName", "Noctyx repository owner"), ("UniqueID", str(uid)),
                     ("PrimaryGroupID", str(gid)), ("NFSHomeDirectory", home), ("IsHidden", "1"), ("Password", "*")):
            subprocess.run(["dscl", ".", "-create", u, k, v], check=True, capture_output=True)
        subprocess.run(["dscl", ".", "-append", f"/Groups/{name}", "GroupMembership", name], capture_output=True)
        return uid
    subprocess.run(["useradd", "--system", *(["--uid", str(uid)] if uid else []), "--home-dir", home,
                    "--shell", "/usr/sbin/nologin", name], check=True, capture_output=True, text=True)
    return uid


def _mac_ids(kind):
    r = subprocess.run(["dscl", ".", "-list", f"/{kind}", "UniqueID" if kind == "Users" else "PrimaryGroupID"],
                       capture_output=True, text=True)
    return {int(x.split()[-1]) for x in r.stdout.splitlines() if x.split() and x.split()[-1].lstrip("-").isdigit()}


def _free_mac_id():
    """A uid and gid both free below 500 (macOS keeps those for daemons and hides them)."""
    used = _mac_ids("Users") | _mac_ids("Groups")
    return next(i for i in range(450, 300, -1) if i not in used)


def _mac_group(name, gid):
    r = subprocess.run(["dscl", ".", "-read", f"/Groups/{name}", "PrimaryGroupID"], capture_output=True, text=True)
    if r.returncode == 0:
        return int(r.stdout.split()[-1])
    if gid in _mac_ids("Groups"):
        gid = _free_mac_id()
    for k, v in (("PrimaryGroupID", str(gid)), ("RealName", "Noctyx repository owner"), ("Password", "*")):
        subprocess.run(["dscl", ".", "-create", f"/Groups/{name}", k, v], check=True, capture_output=True)
    return gid


_RUNAS = ("import os,pwd,sys;u=pwd.getpwnam(sys.argv[1]);os.setgroups([]);os.setgid(u.pw_gid);"
          "os.setuid(u.pw_uid);os.execvp(sys.argv[2],sys.argv[2:])")


def as_user(user, argv):
    """argv run as user with no supplementary groups (anything root writes into a live repository is
    unreadable to the endpoint). The environment passes through, so a key can travel in it."""
    if IS_MAC:
        return [sys.executable, "-c", _RUNAS, user, *argv]
    return ["setpriv", f"--reuid={user}", f"--regid={user}", "--clear-groups", *argv]


# ── trust store ─────────────────────────────────────────────────────────────
def trust_ca(ca, name):
    """Put the site CA where this machine's tools look, so a client needs no --cacert on the head."""
    if IS_MAC:
        # the System keychain only takes a trust setting from someone at the screen: unattended (ssh, sudo
        # from a script) it adds the certificate untrusted and fails, leaving clutter. Noctyx passes the CA
        # explicitly everywhere, so this is a convenience the person does once, in Terminal.
        return False
    anchor = pathlib.Path(f"/usr/local/share/ca-certificates/{name}-ca.crt")
    if not anchor.parent.is_dir():
        return False
    anchor.write_bytes(pathlib.Path(ca).read_bytes())
    os.chmod(anchor, 0o644)
    return subprocess.run(["update-ca-certificates"], capture_output=True).returncode == 0


# ── services: systemd units, or launchd daemons made from them ──────────────
def _unit_fields(text):
    """[Section] Key=Value pairs of a unit, continuation lines joined; repeated keys kept in order."""
    out, sect = [], None
    for line in re.sub(r"\\\n\s*", " ", text).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            sect = line.strip("[]")
        elif "=" in line:
            k, v = line.split("=", 1)
            out.append((sect, k.strip(), v.strip()))
    return out


def _calendar(spec):
    """The OnCalendar forms Noctyx's timers use -> launchd StartCalendarInterval or StartInterval."""
    m = re.fullmatch(r"\*:0/(\d+)", spec)
    if m:
        return {"StartInterval": int(m.group(1)) * 60}
    m = re.fullmatch(r"\*-\*-(\*|\d+) (\d+):(\d+)(?::\d+)?", spec)
    if not m:
        raise ValueError(f"OnCalendar {spec!r}: no launchd form")
    cal = {"Hour": int(m.group(2)), "Minute": int(m.group(3))}
    if m.group(1) != "*":
        cal["Day"] = int(m.group(1))
    return {"StartCalendarInterval": cal}


def launchd_plist(label, service, timer=None, log_dir="/var/log/noctyx"):
    """A launchd daemon doing what a systemd service (+ its timer) does: the same command, user, environment
    and restart policy. The sandboxing options have no launchd equivalent and are dropped."""
    f = _unit_fields(service)
    svc = {k: v for s, k, v in f if s == "Service" and k != "Environment"}
    env = {}
    for s, k, v in f:
        if s == "Service" and k == "Environment":
            for kv in shlex.split(v):
                if "=" in kv:
                    a, b = kv.split("=", 1)
                    env[a] = b
    p = {"Label": label, "ProgramArguments": shlex.split(svc["ExecStart"]),
         "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", **env},
         "StandardOutPath": f"{log_dir}/{label}.log", "StandardErrorPath": f"{log_dir}/{label}.log",
         # detached jobs are their own session; launchd must not reap them when this job exits or restarts
         "AbandonProcessGroup": True}
    if svc.get("User") and svc["User"] != "root":
        p["UserName"] = svc["User"]
        p["GroupName"] = svc.get("Group") or svc["User"]
    if svc.get("Nice"):
        p["Nice"] = int(svc["Nice"])
    if timer:
        cal = next((v for s, k, v in _unit_fields(timer) if s == "Timer" and k == "OnCalendar"), None)
        p.update(_calendar(cal))
    else:
        p["RunAtLoad"] = True
        if svc.get("Restart") in ("always", "on-failure"):
            p["KeepAlive"] = True if svc["Restart"] == "always" else {"SuccessfulExit": False}
            p["ThrottleInterval"] = int(svc.get("RestartSec") or 5)
    return plistlib.dumps(p).decode()


def service_path(name, label_prefix):
    """Where a head service lives: /etc/systemd/system/<name> or /Library/LaunchDaemons/<label>.plist."""
    if IS_MAC:
        return LAUNCHD_DIR / f"{launchd_label(name, label_prefix)}.plist"
    return SYSTEMD_DIR / name


def launchd_label(name, label_prefix):
    """noctyx-console.service -> dev.noctyx.console (the unit prefix is dropped: the label prefix says it)."""
    stem = name.rsplit(".", 1)[0]
    stem = stem.split("-", 1)[1] if "-" in stem else stem
    return f"{label_prefix}.{stem}"


def install_services(units, unit_prefix, label_prefix, log_dir="/var/log/noctyx"):
    """units: {unit file name: rendered text}. Writes and starts them; returns what is now enabled.
    Linux: the units as given, daemon-reload, enable --now (timers and the long-running services).
    macOS: one daemon per service; a service with a timer becomes a calendar job, a oneshot without one is
    skipped (it has nothing to run it)."""
    if not IS_MAC:
        for n, txt in units.items():
            (SYSTEMD_DIR / n).write_text(txt)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        enable = [n for n in units if n.endswith(".timer")] + \
                 [n for n in units if n.endswith(".service") and n.replace(".service", ".timer") not in units
                  and "Type=oneshot" not in units[n]]
        subprocess.run(["systemctl", "enable", "--now", *enable], check=False)
        return enable
    pathlib.Path(log_dir).mkdir(parents=True, exist_ok=True)
    done = []
    for n, txt in units.items():
        if not n.endswith(".service"):
            continue
        timer = units.get(n.replace(".service", ".timer"))
        if not timer and "Type=oneshot" in txt:
            continue
        label = launchd_label(n, label_prefix)
        path = LAUNCHD_DIR / f"{label}.plist"
        new = launchd_plist(label, txt, timer, log_dir)
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"system/{label}"], capture_output=True)
        path.write_text(new)
        os.chmod(path, 0o644)
        user = plistlib.loads(new.encode()).get("UserName")
        if user:             # launchd opens the log as the job's user: a file it cannot write is EX_CONFIG (78)
            log = pathlib.Path(log_dir, f"{label}.log")
            log.touch(exist_ok=True)
            shutil.chown(log, user)
        r = subprocess.run(["launchctl", "bootstrap", "system", str(path)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"launchctl bootstrap {label}: {(r.stderr or r.stdout).strip()}")
        subprocess.run(["launchctl", "enable", f"system/{label}"], capture_output=True)
        done.append(label)
    return done


def service_active(name, label_prefix):
    """Is a long-running head service up? (systemctl is-active / launchd has a pid for it)."""
    if IS_MAC:
        r = subprocess.run(["launchctl", "print", f"system/{launchd_label(name, label_prefix)}"],
                           capture_output=True, text=True)
        return r.returncode == 0 and re.search(r"^\s*pid = \d+", r.stdout, re.M) is not None
    return subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0


def service_installed(name, label_prefix):
    return service_path(name, label_prefix).exists()


def restart_service(name, label_prefix):
    if IS_MAC:
        return subprocess.run(["launchctl", "kickstart", "-k", f"system/{launchd_label(name, label_prefix)}"],
                              capture_output=True).returncode == 0
    return subprocess.run(["systemctl", "restart", name], capture_output=True).returncode == 0


def logs_hint(name, label_prefix, log_dir="/var/log/noctyx"):
    if IS_MAC:
        return f"tail -50 {log_dir}/{launchd_label(name, label_prefix)}.log"
    return f"journalctl -u {name} -n 50"


# ── detached jobs ───────────────────────────────────────────────────────────
def spawn_detached(unit, argv, log, env):
    """macOS: a job that outlives whoever asked for it — its own session, output appended to log, its pid in
    <log>.pid (what job_running reads). Linux uses systemd-run instead (orchestrator.detach)."""
    with open(log, "ab") as out, open(os.devnull, "rb") as dn:
        p = subprocess.Popen(argv, stdin=dn, stdout=out, stderr=out, env=env, start_new_session=True)
    pathlib.Path(f"{log}.pid").write_text(f"{p.pid}\n")
    return p.pid


def pid_alive(pid):
    try:
        pid = int(pid)
        try:                 # our own finished child is a zombie until reaped, and kill(0) succeeds on a zombie
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return False
        except ChildProcessError:
            pass
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def job_running(log):
    try:
        return pid_alive(pathlib.Path(f"{log}.pid").read_text().strip())
    except OSError:
        return False


# ── macOS privacy (TCC): what a daemon may read ─────────────────────────────
def real_executable(path):
    """The binary TCC sees for a launchd job: the interpreter behind a venv's symlinks."""
    return os.path.realpath(path)


def off_boot_volume(path):
    """Is path on another volume than the Mac's data volume (a network share or an external disk)? macOS
    guards both from background processes until they have Full Disk Access."""
    try:
        return os.stat(path).st_dev != os.stat("/private/var").st_dev
    except OSError:
        return True


def open_for_console_user(*argv):
    """Run `open …` in the logged-in user's session (System Settings, Finder), from root over ssh or sudo."""
    try:
        uid = os.stat("/dev/console").st_uid
    except OSError:
        return False
    if uid == 0:
        return False
    return subprocess.run(["launchctl", "asuser", str(uid), "sudo", "-u", f"#{uid}", "open", *argv],
                          capture_output=True).returncode == 0


FULL_DISK_ACCESS_PANE = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"


def launchd_probe(label, user, argv, timeout=20):
    """Run argv once as a launchd daemon (the context TCC judges Noctyx's services in) -> output. argv must end
    by printing one line, `ok` or `ERR …`: launchd's own record of the exit is not dependable for a one-shot."""
    out = pathlib.Path(f"/var/tmp/{label}.out")
    out.unlink(missing_ok=True)
    out.touch()
    shutil.chown(out, user)
    plist = pathlib.Path(f"/var/tmp/{label}.plist")
    plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": argv, "UserName": user, "RunAtLoad": True,
                                      "LaunchOnlyOnce": True, "StandardOutPath": str(out), "StandardErrorPath": str(out)}))
    os.chmod(plist, 0o644)
    subprocess.run(["launchctl", "bootout", f"system/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", "system", str(plist)], capture_output=True)
    import time
    text = ""
    for _ in range(timeout * 4):
        text = out.read_text(errors="replace").strip()
        if re.search(r"^(ok|ERR\b.*)$", text, re.M):
            break
        time.sleep(0.25)
    subprocess.run(["launchctl", "bootout", f"system/{label}"], capture_output=True)
    plist.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    return text or "ERR no output (the probe did not start)"


# ── boot identity ───────────────────────────────────────────────────────────
# one shell snippet for any host, either platform: Linux's boot_id, or macOS's boot session UUID
BOOT_ID_CMD = ("cat /proc/sys/kernel/random/boot_id 2>/dev/null || "
               "sysctl -n kern.bootsessionuuid 2>/dev/null | tr 'A-Z' 'a-z'")


def boot_time():
    """Seconds since the epoch at which this machine booted."""
    if IS_MAC:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout
        return float(re.search(r"sec = (\d+)", out).group(1))
    return float(open("/proc/stat").read().split("btime ")[1].split()[0])
