"""noctyx init: a site config from nothing, by questions with good defaults, on Linux or macOS.

The assistant comes first: once an LLM endpoint answers, every later question takes `?` for help from it, and
every setup step that fails asks it what went wrong. Then the head (detected), the storage (a local directory,
or a NAS share over NFS or SMB), the console and its first admin. Re-running it edits the existing config:
its values are the defaults.

    sudo noctyx init                       the questions
    sudo noctyx init --assistant-only      just the LLM link (works before there is a config)
    sudo noctyx init --yes [flags]         no questions: flags and detected values (see noctyx init -h);
                                           an assistant API key comes from NOCTYX_ASSISTANT_KEY, never a flag
"""
import argparse
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys

from . import hostos, llm
from .paths import CONFIG, P

INTERACTIVE = True
ASSISTANT = None          # the assistant block once it answers: what `?` asks


def say(text=""):
    print(text, flush=True)


def ask(question, default="", why="", secret=False, choices=None):
    """One question. Enter takes the default; `?` asks the assistant about it, with why as context."""
    if not INTERACTIVE:
        return default
    while True:
        shown = "" if secret or default in ("", None) else f" [{default}]"
        try:
            v = (getpass.getpass(f"  {question}{shown}: ") if secret else input(f"  {question}{shown}: ")).strip()
        except EOFError:
            v = ""
        if v == "?":
            help_with(question, why, default)
            continue
        v = v or (default if default is not None else "")
        if choices and v not in choices:
            say(f"    one of: {', '.join(choices)}")
            continue
        return v


def yes(question, default=True, why=""):
    v = ask(f"{question} ({'Y/n' if default else 'y/N'})", "", why)
    return default if not v else v.lower().startswith("y")


def help_with(question, why, default):
    if not ASSISTANT:
        say(f"    {why or 'no more to say about this one'}   (no assistant yet)")
        return
    q = (f"The person is answering this question in `noctyx init` (the first-time setup):\n  {question}\n"
         f"The default offered is: {default!r}\nWhat the question is for: {why}\nThis machine: {llm.facts()}; "
         f"address {hostos.primary_address()}; hostname {hostos.short_hostname()}.\n"
         "Explain in at most 5 lines what to enter, and recommend a value for this machine.")
    say("    asking the assistant…")
    text, model = llm.ask(q, a=ASSISTANT, max_tokens=300)
    for line in (text or f"(no answer: {model})").splitlines():
        say(f"    │ {line}")


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write(path, c):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(c, indent=1) + "\n")
    if p.exists():           # keep what `setup owner` gave it (0640, the owner's group: the directory reads it)
        st = p.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, p)


# ── 1. the assistant ────────────────────────────────────────────────────────
def step_assistant(c, a):
    global ASSISTANT
    say("\n1. The assistant (an LLM that helps with the rest of the install, then triages alerts)")
    old = c.get("assistant") or {}
    if a.assistant:
        cands = [{"url": a.assistant.rstrip("/"), "models": []}]
    else:
        scan = ["127.0.0.1", hostos.primary_address(), *a.scan]
        if INTERACTIVE:
            more = ask("other machines to look on for an LLM server (addresses, comma separated)", "",
                       "Noctyx looks for OpenAI-compatible servers on the usual ports of these machines")
            scan += [x.strip() for x in more.split(",") if x.strip()]
        say(f"  looking on {', '.join(dict.fromkeys(scan))}…")
        cands = llm.discover(scan)
    if old.get("url") and not any(x["url"] == old["url"] for x in cands):
        cands.insert(0, {"url": old["url"], "models": [old.get("model")] if old.get("model") else []})
    for i, x in enumerate(cands, 1):
        say(f"    {i}) {x['url']:<36} {', '.join(m for m in x['models'][:3] if m)}")
    if not cands:
        say("    none found")
    while True:
        pick = ask("which one (a number, a URL like http://host:port/v1, or 'none')", "1" if cands else "none",
                   "the assistant explains failures during setup and alerts afterwards; any OpenAI-compatible server")
        if pick.lower() in ("none", "skip", "n"):
            say("  no assistant: setup works without one, it just cannot explain failures")
            c["assistant"] = {**old, "enabled": False}
            return
        url = cands[int(pick) - 1]["url"] if pick.isdigit() and 0 < int(pick) <= len(cands) else pick.rstrip("/")
        if not url.startswith("http"):
            say("    a number from the list or an http(s):// URL")
            continue
        key = os.environ.get("NOCTYX_ASSISTANT_KEY") or (ask("API key (Enter for none)", "", "only if the server needs a Bearer token",
                                       secret=True) if INTERACTIVE else "")
        served = []
        try:
            served = llm.models(url, key or None, timeout=8)
        except Exception:
            pass
        model = a.model or old.get("model") or ""
        if len(served) > 1 and not a.model:
            say(f"    it serves: {', '.join(served)}")
            model = ask("model", model if model in served else served[0], "which of the served models answers")
        say(f"  testing {url}…")
        t = llm.test(url, model or None, key or None, timeout=int(old.get("timeout_seconds", 120)))
        if t["ok"]:
            say(f"  ✓ {t['model']} answered in {t['ms']} ms: {t['text']!r}")
            break
        say(f"  ✗ {t.get('error')}")
        if not INTERACTIVE and url == old.get("url") and not a.assistant:
            say("  kept as configured: it answered before and may be restarting (`noctyx setup verify` checks it)")
            ASSISTANT = old
            return
        if not INTERACTIVE:
            sys.exit("the assistant did not answer (--assistant URL, or leave it out)")
    blk = {"enabled": True, "url": url, "model": model or t.get("model") or "",
           "timeout_seconds": old.get("timeout_seconds", 120), "fallback": old.get("fallback", True)}
    if key:
        d = pathlib.Path(P["llm_keys"])
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        f = d / "assistant"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, (key.strip() + "\n").encode())
        os.close(fd)
        blk["key_host"] = "assistant"
    elif old.get("key_host"):
        blk["key_host"] = old["key_host"]
    c["assistant"] = blk
    ASSISTANT = blk
    say("  saved. From here on, answer any question with ? to ask it")


# ── 2. the head ─────────────────────────────────────────────────────────────
def step_head(c, a):
    say("\n2. This machine, the head node (console, scheduler, directory, model mirror)")
    s = c.setdefault("server", {})
    s["hostname"] = a.hostname or ask("name", s.get("hostname") or hostos.short_hostname(),
                                      "the head's short name: shown in the console and put in its TLS certificate")
    s["address"] = a.address or ask("LAN address", s.get("address") or hostos.primary_address(),
                                    "the address nodes reach this machine on (the certificate names it); use the wired one")
    while True:
        port = int(a.ui_port or ask("console port", str(s.get("ui_port", 20996)), "the HTTPS port of the web console"))
        if hostos.port_free(port) or port == s.get("ui_port") or not INTERACTIVE:
            break
        say(f"    port {port} is in use on this machine")
    s["ui_port"] = port
    for k, v in (("rest_port", 20998), ("directory_port", 20999), ("discovery_udp_port", 20997)):
        s.setdefault(k, v)
    s.setdefault("ssh", {"user": a.ssh_user or "root",
                         "key": "/var/root/.ssh/id_ed25519" if hostos.IS_MAC else "/root/.ssh/id_ed25519"})
    s["tls"] = s.get("tls") or {k: f"{P['tls']}/{f}" for k, f in
                                (("cert", "server.crt"), ("key", "server.key"), ("ca", "ca.crt"))}


# ── 3. storage ──────────────────────────────────────────────────────────────
def step_storage(c, a):
    say("\n3. Where backups and the model mirror live")
    s = c["server"]
    old = next(iter((c.get("spindles") or {}).items()), (None, {}))
    kind0 = (old[1].get("share") or {}).get("proto") or ("local" if old[0] else "")
    say("    local  a directory or disk on this machine\n    nfs    a NAS share over NFS (a Synology, TrueNAS…)\n"
        "    smb    a NAS share over SMB")
    kind = a.storage or ask("storage", kind0 or "local", "where restic repositories go", choices=["local", "nfs", "smb"])
    if kind == "local":
        base = a.path or ask("directory", old[1].get("mount") if kind0 == "local" else
                             ("/Library/Noctyx" if hostos.IS_MAC else "/srv/noctyx"),
                             "a directory on a disk with room for every node's backups (an external disk is fine)")
        c["spindles"] = {"local": {"mount": base, "kind": "local"}}
        s["repo_root"] = f"{base.rstrip('/')}/repos"
        store = f"{base.rstrip('/')}/hf-mirror"
    else:
        sh0 = old[1].get("share") or {}
        server = a.nas_server or ask("NAS address", sh0.get("server", ""), "the NAS's LAN address or name")
        if kind == "nfs" and INTERACTIVE and shutil.which("showmount"):
            r = subprocess.run(["showmount", "-e", server], capture_output=True, text=True, timeout=15)
            say("    " + ((r.stdout.strip() or r.stderr.strip()).replace("\n", "\n    ") or "no exports listed"))
        path = a.nas_path or ask("export path" if kind == "nfs" else "share (and folder)", sh0.get("path", ""),
                                 "NFS: the export, e.g. /volume1/backups. SMB: the share name, optionally /folder")
        sh = {"proto": kind, "server": server, "path": path}
        if kind == "smb":
            sh["user"] = a.nas_user or ask("NAS user", sh0.get("user", ""), "the NAS account that owns the share")
        mount = a.mount or ask("mount it at", old[1].get("mount") or
                               ("/Volumes/noctyx-nas" if hostos.IS_MAC else "/mnt/noctyx-nas"), "an empty directory on this machine")
        sub = a.subdir if a.subdir is not None else ask("folder for Noctyx on the share", "noctyx", "repositories go in <mount>/<folder>/repos")
        c["spindles"] = {"nas": {"mount": mount, "kind": "nas", "share": sh}}
        base = f"{mount.rstrip('/')}/{sub.strip('/')}" if sub.strip("/") else mount.rstrip("/")
        s["repo_root"] = f"{base}/repos"
        store = f"{base}/hf-mirror"
        if kind == "nfs":
            uid = a.owner_uid or ask("uid the NAS gives write access to (Enter: any)", str(s.get("repo_owner_uid") or ""),
                                     "an NFS share checks the writer's uid; match the NAS user that owns the folder "
                                     "(Synology: the first user is 1026)")
            if uid:
                s["repo_owner_uid"] = int(uid)
    hf = c.setdefault("hf_mirror", {})
    hf.update({"store": hf.get("store") if hf.get("store") and kind == kind0 else store,
               "port": hf.get("port", 20995), "harvest": hf.get("harvest", True), "scan_roots": hf.get("scan_roots", ["/"])})


# ── 4. what to back up ──────────────────────────────────────────────────────
# configuration only: a first backup in seconds. Never home directories (a root's can hold hundreds of GB of models)
LINUX_CONFIG_PATHS = ["/etc", "/usr/local/etc"]


def _time_machine():
    """The Mac's Time Machine destination's name, or None."""
    r = subprocess.run(["tmutil", "destinationinfo"], capture_output=True, text=True)
    return next((l.split(":", 1)[1].strip() for l in r.stdout.splitlines() if l.startswith("Name")), None)


def _ssh_key(c):
    """Root's key the head reaches nodes with: made if missing; -> its public half."""
    key = pathlib.Path(c["server"]["ssh"]["key"])
    if not key.exists():
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"noctyx@{c['server']['hostname']}",
                        "-f", str(key)], check=True)
    return pathlib.Path(f"{key}.pub").read_text().strip()


def _node_arch(c, addr):
    s = c["server"]["ssh"]
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
                        "-i", s["key"], f"{s['user']}@{addr}", "uname -m"], capture_output=True, text=True, timeout=15)
    return {"arm64": "aarch64"}.get(r.stdout.strip(), r.stdout.strip()) if r.returncode == 0 else None


def step_hosts(c, a):
    """The machines: the head itself, and the Linux boxes Noctyx backs up (one repository each). A Mac is
    monitored, never backed up: it has Time Machine, and the agent is a Linux one."""
    s = c["server"]
    name = s["hostname"]
    say("\n4. The machines Noctyx backs up (Linux only; Macs are monitored)")
    old = {h["name"]: h for h in c.get("hosts") or []}
    head = {"name": name, "address": s["address"], "arch": hostos.arch(), "os": hostos.os_name(), "local": True}
    if hostos.IS_MAC:
        tm = _time_machine()
        say(f"  this Mac is monitored, not backed up" + (f" (Time Machine backs it up to {tm!r})" if tm else ""))
        head["backup"] = False
    elif a.no_head_backup or not yes("back this machine up too", old.get(name, {}).get("backup", True) is not False,
                                     "the head holds the keys and the site config: backing it up is what lets you rebuild it"):
        head["backup"] = False
    else:
        head["repo"] = name
    hosts = [head] + [h for n, h in old.items() if n != name and h.get("address") != s["address"]]
    nodes = [x.split("=", 1) for x in a.node]
    if INTERACTIVE:
        if any(h.get("repo") for h in hosts[1:]):
            say("  already: " + ", ".join(f"{h['name']} ({h['address']})" for h in hosts[1:] if h.get("repo")))
        say("  Linux machines to back up, one per line as NAME ADDRESS (Enter on an empty line to finish):")
        while True:
            line = ask("node", "", "a GPU box or any Linux machine: its short name and LAN address, e.g. gpu-01 192.0.2.21")
            if not line:
                break
            parts = line.replace("=", " ").split()
            if len(parts) != 2:
                say("    NAME ADDRESS, e.g. gpu-01 192.0.2.21")
                continue
            nodes.append(parts)
    if nodes:
        s["ssh"]["user"] = a.ssh_user or ask("user the head logs into the nodes as (it needs passwordless sudo there)",
                                             s["ssh"].get("user") or "root", "Noctyx runs `sudo -n` on each node as this user")
        pub = _ssh_key(c)
    for n, addr in nodes:
        arch = _node_arch(c, addr) or old.get(n, {}).get("arch") or "x86_64"
        hosts = [h for h in hosts if h["name"] != n] + [{"name": n, "address": addr, "arch": arch, "os": "linux", "repo": n}]
    c["hosts"] = hosts
    keep = {r["id"]: r for r in c.get("repositories") or []}
    spindle = next(iter(c["spindles"]))
    c["repositories"] = [keep.get(h["repo"]) or {"id": h["repo"], "spindle": spindle}
                         for h in hosts if h.get("repo") and h.get("os", "linux") == "linux" and h.get("backup", True) is not False]
    pr = c.setdefault("profiles", {})
    pr.setdefault("system", {"description": "the whole root filesystem minus the noise list", "paths": ["/"],
                             "exclusions": ["common", "per-host"]})
    pr.setdefault("config", {"description": "system configuration only: small and quick (a first backup)",
                             "paths": LINUX_CONFIG_PATHS, "exclusions": ["common"]})
    sch = c.setdefault("schedule", {"tick_minutes": 15, "retry_after": "1h"})
    sch.setdefault("defaults", {"system": {"every": "24h", "window": "01:00-07:00"}})
    c.setdefault("retention", {"daily": 14, "weekly": 8, "monthly": 6})
    c.setdefault("network", {"expected_client_to_server_mbit": 1000, "measured_client_to_server_mbit": None,
                             "min_throughput_mbit": 40})
    c.setdefault("recipes", {"slots": {}, "catalog": {}})
    c.setdefault("common_exclusions_extra", [])
    c.setdefault("paths", {})
    c["version"] = 1
    backed = [h["name"] for h in hosts if h.get("repo") and h.get("backup", True) is not False]
    say(f"  backed up: {', '.join(backed) or 'nothing yet'}; profile system (/, nightly 01:00–07:00)")
    if nodes:
        say(f"  on each node, authorise the head's key for {s['ssh']['user']} (and give it passwordless sudo):\n"
            f"    echo '{pub}' >> ~{s['ssh']['user']}/.ssh/authorized_keys")


# ── 5. the console ──────────────────────────────────────────────────────────
def step_console(c, a):
    say("\n5. Who may use the console")
    s = c["server"]
    auth = not a.no_auth if not INTERACTIVE else yes(
        "require sign-in", s.get("console_auth", True) is not False,
        "on: everyone signs in (viewer, operator, admin). off: open to anyone on the LAN, for a network you trust")
    s["console_auth"] = auth


def make_admin(a):
    from . import accounts
    if any(u.get("role") == "admin" for u in accounts.users().values()):
        return
    name = a.admin or ask("first admin's user name", getpass.getuser() if os.geteuid() else "admin",
                          "a console account with every right")
    if not name:
        return
    pw = sys.stdin.readline().rstrip("\n") if a.admin_password_stdin else None
    while not pw and INTERACTIVE:
        p1 = ask("password (10+ characters)", "", "", secret=True)
        if p1 == ask("again", "", "", secret=True) and len(p1) >= accounts.MIN_PASSWORD:
            pw = p1
        else:
            say("    they differ, or it is shorter than 10 characters")
    if not pw:
        say("  no admin made: `sudo noctyx user add --name NAME --role admin` later")
        return
    errs = accounts.check_new(name, "admin", pw)
    if errs:
        say(f"  no admin made: {'; '.join(errs)}")
        return
    accounts.add(name, "admin", pw)
    say(f"  admin {name}: made")


def main(argv):
    global INTERACTIVE, ASSISTANT
    p = argparse.ArgumentParser(prog="noctyx init", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="no questions: flags, then detected values and defaults")
    p.add_argument("--assistant-only", action="store_true", help="only the LLM link")
    p.add_argument("--assistant", help="the LLM endpoint, http://host:port/v1"); p.add_argument("--model")
    p.add_argument("--scan", nargs="*", default=[], help="more machines to look on for an LLM server")
    p.add_argument("--hostname"); p.add_argument("--address"); p.add_argument("--ui-port", type=int)
    p.add_argument("--ssh-user", help="the user the head reaches nodes as (passwordless sudo there)")
    p.add_argument("--storage", choices=["local", "nfs", "smb"]); p.add_argument("--path", help="local: the directory")
    p.add_argument("--nas-server"); p.add_argument("--nas-path"); p.add_argument("--nas-user")
    p.add_argument("--mount", help="nfs/smb: where to mount the share"); p.add_argument("--subdir", help="folder on the share")
    p.add_argument("--owner-uid", help="nfs: the uid the NAS lets write")
    p.add_argument("--node", action="append", default=[], metavar="NAME=ADDRESS", help="a Linux machine to back up (repeatable)")
    p.add_argument("--no-head-backup", action="store_true", help="a Linux head: do not back the head itself up")
    p.add_argument("--no-auth", action="store_true", help="console open on the LAN, no sign-in")
    p.add_argument("--admin", help="the first admin's name"); p.add_argument("--admin-password-stdin", action="store_true")
    p.add_argument("--setup", action="store_true", help="then run `noctyx setup all`")
    a = p.parse_args(argv)
    INTERACTIVE = not a.yes and sys.stdin.isatty()
    if os.geteuid() != 0:
        sys.exit("run as root: sudo noctyx init (it writes the config, keys and system user)")
    c = load(CONFIG)
    from .banner import render
    say(render())
    say(f"\nNoctyx first-time setup · {hostos.os_name()} {hostos.arch()} · config {CONFIG}"
        + (" (exists: its values are the defaults)" if c else ""))
    say("Enter takes the [default].")
    ASSISTANT = c.get("assistant") if (c.get("assistant") or {}).get("enabled") else None
    step_assistant(c, a)
    write(CONFIG, c) if not c.get("server") else None      # a new site: the assistant answers `noctyx ask` already
    if a.assistant_only:
        if c.get("server"):
            write(CONFIG, c)
        say(f"\nsaved to {CONFIG}. Ask it anything about the install:  noctyx ask \"…\"")
        return 0
    step_head(c, a)
    step_storage(c, a)
    step_hosts(c, a)
    step_console(c, a)
    write(CONFIG, c)
    say(f"\nwrote {CONFIG}")
    if c["server"]["console_auth"]:
        pathlib.Path(P["etc"]).mkdir(parents=True, exist_ok=True)
        make_admin(a)
    first = next((h["name"] for h in c["hosts"] if h.get("repo") and h.get("backup", True) is not False), "<node>")
    say(f"""
next:
  sudo noctyx setup all          tools, owner, {'mount, ' if any(s.get('share') for s in c['spindles'].values()) else ''}TLS, repositories, endpoint, services, escrow
  sudo noctyx install-agent      the agent on every Linux host to back up
  sudo noctyx backup --host {first} --profile config
  sudo noctyx setup verify       every check, and a backup+restore round trip
console: https://{c['server']['address']}:{c['server']['ui_port']}""")
    if a.setup or (INTERACTIVE and yes("run `noctyx setup all` now", True, "it runs every setup step in order and stops at the first that fails")):
        os.execv(sys.executable, [sys.executable, "-m", "noctyx.cli", "setup", "all"])
    return 0
