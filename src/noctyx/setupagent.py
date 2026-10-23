"""The web setup's assistant: an LLM that helps the operator install Noctyx and add machines to it.

It works through a fixed set of tools (the OpenAI tools API), never a shell. Read-only tools (probe a machine,
scan the LAN, list NFS exports, read the guide) run as soon as it calls them. Every tool that changes something
(the head, the storage, sign-in, a node, a setup step, an agent or metrics install) becomes a proposal the
operator sees with its arguments, may edit, and approves or rejects; only an approved proposal runs, through the
same code `noctyx init` and `noctyx setup` run. Passwords never reach the LLM: the first admin's and an SMB
share's are typed into forms the web setup handles itself.
"""
import concurrent.futures
import ipaddress
import itertools
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

from . import firstrun, hostos, llm
from .paths import CONFIG

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")
USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SETUP_STEPS = ("all", "verify", "tools", "owner", "mount", "tls", "repos", "endpoint", "units", "metrics", "privacy")
GUIDES = ("install", "troubleshooting", "site-config", "accounts", "assistant", "backups", "console", "upgrade")
MAX_SCAN = 1024           # addresses: a /22 at most


class Bad(ValueError):
    """An argument a tool refuses: said back to the LLM (or the operator) as the tool's result."""


# ── argument checks: everything an LLM sends is checked before it touches anything ──
def _address(v):
    v = str(v or "").strip()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        if HOSTNAME.match(v):
            return v
    raise Bad(f"not an address or host name: {v!r}")


def _name(v):
    v = str(v or "").strip().lower()
    if not NAME.match(v):
        raise Bad(f"a machine name is lower-case letters, digits and '-', up to 63: {v!r}")
    return v


def _path(v):
    v = str(v or "").strip()
    if not v.startswith("/") or ".." in v.split("/") or any(ch in v for ch in "\n\r\t\0 '\"`$;&|<>\\"):
        raise Bad(f"an absolute path without spaces or shell characters: {v!r}")
    return v.rstrip("/") or "/"


def _port(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Bad(f"a port number: {v!r}")
    if not 1024 <= v <= 65535 or v in (21000, 21001):
        raise Bad(f"port {v}: use 1024-65535 (and never 21000/21001)")
    return v


def _user(v):
    v = str(v or "").strip()
    if not USER.match(v):
        raise Bad(f"a Unix user name: {v!r}")
    return v


def _text(v, n=200):
    v = str(v or "").strip()
    if len(v) > n or any(ch in v for ch in "\n\r\0"):
        raise Bad("text too long or with line breaks")
    return v


# ── the site as it stands ──
def load():
    return firstrun.load(CONFIG)


def save(c):
    firstrun.write(CONFIG, c)


def ssh_pub(c):
    """The head's public key (made if missing): what each node authorises."""
    if not (c.get("server") or {}).get("ssh"):
        return None
    try:
        return firstrun._ssh_key(c)
    except Exception:
        return None


def admin_exists():
    from . import accounts
    try:
        return any(u.get("role") == "admin" for u in accounts.users().values())
    except Exception:
        return False


def steps(c):
    """[(step, done, what)] of `noctyx setup`, or [] before the site has a head and storage."""
    if not (c.get("server") or {}).get("repo_root"):
        return []
    from . import setup
    try:
        return [(n, bool(ok), what) for n, ok, what in setup.steps_state(c)]
    except Exception as e:
        return [("status", False, f"could not read the setup state: {e}")]


def state(c=None):
    """What the site has so far, for the operator's checklist and the LLM's context."""
    c = c if c is not None else load()
    s = c.get("server") or {}
    sp = firstrun.spindles(c)
    a = c.get("assistant") or {}
    return {
        "config": CONFIG, "exists": os.path.exists(CONFIG),
        "this_machine": {"hostname": hostos.short_hostname(), "address": hostos.primary_address(),
                         "os": hostos.os_name(), "arch": hostos.arch()},
        "assistant": {"url": a.get("url"), "model": a.get("model")} if a.get("enabled") else None,
        "head": {k: s.get(k) for k in ("hostname", "address", "ui_port")} | {"ssh_user": (s.get("ssh") or {}).get("user")}
        if s.get("hostname") else None,
        "storage": {n: {"mount": v.get("mount"), "share": v.get("share")} for n, v in sp.items()} or None,
        "repo_root": s.get("repo_root"),
        "hosts": [{k: h.get(k) for k in ("name", "address", "arch", "os", "repo", "backup", "local") if k in h}
                  for h in c.get("hosts") or []],
        "sign_in": None if "console_auth" not in s else s["console_auth"] is not False,
        "admin": admin_exists(),
        "setup_steps": [{"step": n, "done": ok, "what": w} for n, ok, w in steps(c)],
        "head_public_key": ssh_pub(c) if c.get("hosts") and len(c["hosts"]) > 1 else None,
    }


# ── read-only tools ──
def t_site_state(sess, a):
    return state(sess.config())


def t_read_guide(sess, a):
    page = a.get("page")
    if page not in GUIDES:
        raise Bad(f"page is one of {', '.join(GUIDES)}")
    text = llm._guide_page(f"{page}.md")
    sec = _text(a.get("section") or "", 80).lower()
    if sec:
        parts = [p for p in re.split(r"(?m)^(?=#{1,3} )", text) if p.strip()]
        hit = [p for p in parts if sec in p.splitlines()[0].lower()]
        text = "\n".join(hit) or f"(no heading matching {sec!r}; headings: " + "; ".join(
            p.splitlines()[0].strip("# ") for p in parts if p.startswith("#")) + ")"
    return text[:12000] or "(the guide is not available on this machine)"


def t_find_llm_servers(sess, a):
    addrs = [_address(x) for x in (a.get("addresses") or [])][:16]
    found = llm.discover(["127.0.0.1", hostos.primary_address(), *addrs])
    return [{"url": f["url"], "models": f["models"][:4], "ms": f["ms"]} for f in found]


def _open(addr, port, timeout=0.6):
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            return True
    except OSError:
        return False


def t_scan_network(sess, a):
    cidr = a.get("cidr") or f"{hostos.primary_address()}/24"
    try:
        net = ipaddress.ip_network(str(cidr), strict=False)
    except ValueError:
        raise Bad(f"not a network: {cidr!r}")
    if net.version != 4 or not net.is_private:
        raise Bad("a private IPv4 network, e.g. 192.0.2.0/24")
    if net.num_addresses > MAX_SCAN:
        raise Bad(f"{net} has {net.num_addresses} addresses; scan at most {MAX_SCAN} (a /22) at a time")
    known = {h.get("address"): h.get("name") for h in sess.config().get("hosts") or []}

    def one(ip):
        ip = str(ip)
        ports = [p for p in (22, 9100) if _open(ip, p)]
        if not ports:
            return None
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            name = None
        return {"address": ip, "name": name, "ssh": 22 in ports, "node_exporter": 9100 in ports,
                "in_site": known.get(ip)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        found = [r for r in ex.map(one, net.hosts()) if r]
    return {"network": str(net), "machines": found}


def probe(c, addr):
    """What the head finds at addr: SSH open, logging in with its key, arch, OS, passwordless sudo."""
    s = (c.get("server") or {}).get("ssh") or {"user": "root", "key": "/var/root/.ssh/id_ed25519" if hostos.IS_MAC
                                                 else "/root/.ssh/id_ed25519"}
    out = {"address": addr, "ssh_port_open": _open(addr, 22, 2), "ssh_user": s["user"]}
    if not out["ssh_port_open"]:
        return out | {"ok": False, "why": "nothing answers on port 22"}
    if not pathlib.Path(s["key"]).exists():
        return out | {"ok": False, "why": f"the head has no SSH key yet ({s['key']}): configure the head first"}
    script = ("uname -sm; (. /etc/os-release 2>/dev/null && echo \"$ID $VERSION_ID\") || sw_vers -productVersion; "
              "hostname -s; if sudo -n true 2>/dev/null; then echo SUDO_OK; else echo SUDO_NO; fi")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new",
                            "-i", s["key"], f"{s['user']}@{addr}", script], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return out | {"ok": False, "why": "ssh timed out"}
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["ssh failed"])[-1][:200]
        return out | {"ok": False, "login": False, "why": why,
                      "fix": f"authorise the head's key for {s['user']} on that machine (the public key is in site_state)"}
    txt = r.stdout.splitlines()
    kernel, _, arch = (txt[0] if txt else "").partition(" ")
    arch = {"arm64": "aarch64"}.get(arch, arch) or None
    sudo = "SUDO_OK" in r.stdout
    return out | {"ok": sudo, "login": True, "arch": arch, "mac": kernel == "Darwin",
                  "os": ("macOS " if kernel == "Darwin" else "") + (txt[1] if len(txt) > 1 else ""),
                  "hostname": txt[2] if len(txt) > 2 else None, "passwordless_sudo": sudo,
                  **({} if sudo else {"why": f"{s['user']} has no passwordless sudo there",
                                      "fix": f"on that machine: echo '{s['user']} ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/noctyx"})}


def t_probe_host(sess, a):
    return probe(sess.config(), _address(a.get("address")))


def t_list_nfs_exports(sess, a):
    server = _address(a.get("server"))
    if not shutil.which("showmount"):
        return "showmount is not installed on the head (Debian/Ubuntu: nfs-common)"
    r = subprocess.run(["showmount", "-e", server], capture_output=True, text=True, timeout=15)
    return (r.stdout.strip() or r.stderr.strip() or "no exports listed")[:4000]


def t_job_output(sess, a):
    j = sess.jobs.get(str(a.get("job")))
    if not j:
        raise Bad("no such job")
    return {"job": j["id"], "what": j["what"], "running": j["rc"] is None, "exit": j["rc"], "tail": "\n".join(j["log"][-80:])}


# ── tools that change something: proposals the operator approves ──
def _apply(sess, steps_, **values):
    with sess.lock:
        c = sess.config()
        said = firstrun.apply(c, steps_, **values)
        save(c)
        sess.c = c
        sess._st = (0, None)
    return said or "done"


def t_configure_head(sess, a):
    vals = {"hostname": _name(a.get("hostname")), "address": _address(a.get("address")),
            "ui_port": _port(a.get("ui_port") or 20996), "ssh_user": _user(a.get("ssh_user") or "root")}
    sess.head_backup = bool(a.get("back_up_head", True))
    said = _apply(sess, ["head"], **vals)
    with sess.lock:          # init keeps an existing site's ssh block; here the operator just chose the user
        c = sess.config()
        if c["server"]["ssh"].get("user") != vals["ssh_user"]:
            c["server"]["ssh"]["user"] = vals["ssh_user"]
            save(c)
            sess.c = c
    if firstrun.spindles(sess.config()):       # storage already there: the head's backup choice lands now
        said += "\n" + _apply(sess, ["hosts"], no_head_backup=not sess.head_backup)
    return said


def t_configure_storage(sess, a):
    c = sess.config()
    if not (c.get("server") or {}).get("hostname"):
        raise Bad("configure the head first")
    kind = a.get("kind")
    if kind not in ("local", "nfs", "smb"):
        raise Bad("kind is local, nfs or smb")
    v = {"storage": kind}
    if kind == "local":
        v["path"] = _path(a.get("path") or ("/Library/Noctyx" if hostos.IS_MAC else "/srv/noctyx"))
    else:
        v["nas_server"] = _address(a.get("nas_server"))
        v["nas_path"] = _text(a.get("nas_path"), 200)
        if not v["nas_path"] or any(ch in v["nas_path"] for ch in "'\"`$;&|<>\\ "):
            raise Bad("nas_path: the export (NFS, e.g. /volume1/backups) or the share (SMB), no spaces")
        v["mount"] = _path(a.get("mount") or ("/Volumes/noctyx-nas" if hostos.IS_MAC else "/mnt/noctyx-nas"))
        v["subdir"] = _text(a.get("subdir") if a.get("subdir") is not None else "noctyx", 60).strip("/")
        if v["subdir"] and not NAME.match(v["subdir"].replace("_", "-").lower()):
            raise Bad("subdir: one folder name")
        if kind == "smb":
            v["nas_user"] = _user(a.get("nas_user"))
        if kind == "nfs" and a.get("owner_uid") not in (None, ""):
            try:
                v["owner_uid"] = str(int(a["owner_uid"]))
            except ValueError:
                raise Bad("owner_uid: a number")
    return _apply(sess, ["storage", "hosts"], no_head_backup=not sess.head_backup, **v)


def t_set_sign_in(sess, a):
    if not (sess.config().get("server") or {}).get("hostname"):
        raise Bad("configure the head first")
    return _apply(sess, ["console"], no_auth=not bool(a.get("required", True)))


def t_add_node(sess, a):
    c = sess.config()
    if not firstrun.spindles(c):
        raise Bad("configure the head and the storage first")
    name, addr = _name(a.get("name")), _address(a.get("address"))
    if any(h.get("name") == name for h in c.get("hosts") or []):
        raise Bad(f"{name} is already in the site")
    p = probe(c, addr)
    if not p.get("login"):
        raise Bad(f"the head cannot log into {addr} yet: {p.get('why')}. {p.get('fix', '')}".strip())
    if p.get("mac"):         # a Mac is monitored, never backed up (Time Machine is its backup): no repository
        with sess.lock:
            c = sess.config()
            c.setdefault("hosts", []).append({"name": name, "address": addr, "arch": p.get("arch") or "aarch64",
                                              "os": "macos", "backup": False, "ssh_user": p["ssh_user"]})
            save(c)
            sess.c, sess._st = c, (0, None)
        return f"{name}: a Mac, added as monitored (not backed up); install_node_metrics puts it on the dashboard"
    said = _apply(sess, ["hosts"], node=[f"{name}={addr}"], no_head_backup=not sess.head_backup)
    warn = "" if p.get("passwordless_sudo") else f"\nwarning: {p.get('why')}; {p.get('fix')}"
    return said + warn


def t_run_setup(sess, a):
    step = a.get("step")
    if step == "escrow":
        raise Bad("escrow asks for a passphrase at a terminal: the operator runs `sudo noctyx setup escrow` there")
    if step not in SETUP_STEPS:
        raise Bad(f"step is one of {', '.join(SETUP_STEPS)}")
    if not firstrun.spindles(sess.config()):
        raise Bad("configure the head and the storage first")
    return sess.start_job(f"setup {step}", ["setup", step])


def _host(sess, a):
    name = _name(a.get("host"))
    h = next((h for h in sess.config().get("hosts") or [] if h.get("name") == name), None)
    if not h:
        raise Bad(f"{name} is not in the site: add_node first")
    return h


def t_install_agent(sess, a):
    h = _host(sess, a)
    if h.get("os", "linux") != "linux" or h.get("backup") is False or not h.get("repo"):
        raise Bad(f"{h['name']} is not backed up (a Mac, or a host with backup off): it gets no agent")
    if not any(n == "endpoint" and ok for n, ok, _ in steps(sess.config())):
        raise Bad("the head's repository endpoint is not up yet: run_setup all first")
    return sess.start_job(f"install the agent on {h['name']}", ["install-agent", "--host", h["name"]])


def t_install_node_metrics(sess, a):
    h = _host(sess, a)
    if h.get("local"):
        raise Bad(f"{h['name']} is the head: `setup metrics` gives it its exporter")
    if h.get("os") == "macos":           # over SSH from the head, like the Linux one (Homebrew on the Mac)
        return sess.start_job(f"install Mac metrics on {h['name']}", ["tool", "install-mac-metrics", h["name"]])
    return sess.start_job(f"install node metrics on {h['name']}", ["tool", "install-node-metrics", "--host", h["name"]])


WHY = {"type": "string", "description": "one or two sentences to the operator, shown on the proposal: what this "
                                        "does and why these values"}


def _fn(name, desc, props, required=(), readonly=True, run=None):
    if not readonly:         # a proposal carries its reason: models often send a tool call with no text beside it
        props, required = {**props, "why": WHY}, (*required, "why")
    return name, {"readonly": readonly, "run": run,
                  "schema": {"type": "function", "function": {"name": name, "description": desc, "parameters": {
                      "type": "object", "properties": props, "required": list(required)}}}}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = dict([
    _fn("site_state", "The site config so far: this machine, the assistant, head, storage, hosts, sign-in, whether "
        "an admin exists, each `noctyx setup` step's state, and the head's SSH public key once nodes exist.",
        {}, run=t_site_state),
    _fn("read_guide", "A page of the Noctyx guide (optionally only the sections whose heading contains `section`).",
        {"page": {"type": "string", "enum": list(GUIDES)}, "section": S}, ["page"], run=t_read_guide),
    _fn("find_llm_servers", "OpenAI-compatible LLM servers on the usual ports of this machine and the given addresses.",
        {"addresses": {"type": "array", "items": S}}, run=t_find_llm_servers),
    _fn("scan_network", "Machines on a private IPv4 network (default: this machine's /24) with SSH (22) or a "
        "node_exporter (9100) open, with their reverse-DNS names and whether they are in the site already.",
        {"cidr": S}, run=t_scan_network),
    _fn("probe_host", "Whether the head can manage a machine: SSH port, login with the head's key, arch, OS, "
        "passwordless sudo, and the fix for whatever is missing.", {"address": S}, ["address"], run=t_probe_host),
    _fn("list_nfs_exports", "The NFS exports a NAS offers (showmount -e).", {"server": S}, ["server"], run=t_list_nfs_exports),
    _fn("job_output", "The output of a setup or install job so far.", {"job": S}, ["job"], run=t_job_output),
    _fn("configure_head", "PROPOSAL. Set this machine up as the head: its short name, LAN address (the wired one "
        "nodes reach), console HTTPS port (default 20996), the user it logs into nodes as (passwordless sudo "
        "there; default root), and whether the head backs itself up (Linux heads only).",
        {"hostname": S, "address": S, "ui_port": I, "ssh_user": S, "back_up_head": B}, ["hostname", "address"],
        readonly=False, run=t_configure_head),
    _fn("configure_storage", "PROPOSAL. Where backups and the model mirror live: kind local (path on this machine), "
        "nfs (nas_server, nas_path = the export, mount, subdir, owner_uid = the uid the NAS lets write; Synology's "
        "first user is 1026) or smb (nas_server, nas_path = the share, nas_user, mount, subdir; the operator types "
        "the share's password into a form, never you).",
        {"kind": {"type": "string", "enum": ["local", "nfs", "smb"]}, "path": S, "nas_server": S, "nas_path": S,
         "nas_user": S, "mount": S, "subdir": S, "owner_uid": I}, ["kind"], readonly=False, run=t_configure_storage),
    _fn("set_sign_in", "PROPOSAL. Whether the console requires sign-in (recommended: true). The first admin's "
        "account is made by the operator in a form, never by you.", {"required": B}, ["required"],
        readonly=False, run=t_set_sign_in),
    _fn("add_node", "PROPOSAL. Add a machine by its short name and address: a Linux one is backed up and "
        "monitored, a Mac monitored only. The head must already log into it (probe_host first).", {"name": S, "address": S}, ["name", "address"],
        readonly=False, run=t_add_node),
    _fn("run_setup", "PROPOSAL. Run a `noctyx setup` step on the head (all = every step not done yet, in order; "
        "verify = every check and a backup+restore round trip). Runs as a job; its output comes back to you.",
        {"step": {"type": "string", "enum": list(SETUP_STEPS)}}, ["step"], readonly=False, run=t_run_setup),
    _fn("install_agent", "PROPOSAL. Install the backup agent on a Linux host of the site (after run_setup all).",
        {"host": S}, ["host"], readonly=False, run=t_install_agent),
    _fn("install_node_metrics", "PROPOSAL. Put a host of the site on the console's dashboard: node_exporter and "
        "the GPU collector, installed over SSH from the head (a Mac needs Homebrew; Linux and Mac alike need "
        "passwordless sudo for the ssh user).", {"host": S}, ["host"],
        readonly=False, run=t_install_node_metrics),
])
JOB_TOOLS = ("run_setup", "install_agent", "install_node_metrics")

SYSTEM = """You are the setup assistant of Noctyx, a self-hosted operator for small AI clusters: a head node \
runs the web console, a directory service, a Hugging Face mirror and restic backups (to a NAS share or a local \
disk), and Linux GPU nodes run a backup agent and node_exporter. You are helping the operator set a site up in \
a browser, step by step: the head, the storage, sign-in, `noctyx setup all` on the head, then the nodes (each: \
the head's key authorised with passwordless sudo, add_node, install_agent, install_node_metrics).

How you work:
- Find facts with the read-only tools before you propose anything: probe_host before add_node, \
list_nfs_exports before an NFS storage, site_state when unsure, read_guide for details.
- Tools marked PROPOSAL only run when the operator approves; they may edit the values first. Propose one \
change at a time, with values that fit this site, and fill its `why` for the operator.
- You never see or ask for passwords or keys. The first admin's password and an SMB share's password go into \
forms on the page. Escrow's passphrase is typed in a terminal (`sudo noctyx setup escrow`).
- To let the head manage a node, the operator runs on that node, as the node's admin: \
`echo '<head public key>' | sudo tee -a ~<ssh_user>/.ssh/authorized_keys` and gives <ssh_user> passwordless \
sudo. Give them that exact line with the real key and user from site_state.
- When a job fails, read its output and say what went wrong and the fix, briefly.
- Never suggest turning off a firewall, TLS verification or any other protection, and never install restic \
or rest-server by hand (`noctyx setup tools` does).
- Be brief: a few sentences, commands in backticks. Macs are monitored, never backed up (Time Machine does)."""


class Session:
    """One operator's setup: the conversation, the pending proposals and the running jobs. The LLM's turns
    run on a worker thread; the page polls `view()`."""

    def __init__(self):
        self.lock = threading.RLock()
        self.c = load()
        self.head_backup = True
        self.items = []               # what the page shows
        self.msgs = []                # what the LLM sees (after the system prompt)
        self.waiting = {}             # tool_call_id -> proposal id or job id the LLM's last message waits on
        self.props = {}
        self.jobs = {}
        self.notes = []               # what the operator did through forms, told to the LLM with the next message
        self.busy = False
        self.ids = itertools.count(1)
        self.revs = itertools.count(1)
        self.work = threading.Condition(self.lock)
        self.queue = []
        self._st = (0, None)
        threading.Thread(target=self._worker, daemon=True).start()

    def config(self):
        c = load()
        return c if c else self.c

    def _item(self, **kw):
        kw["id"] = next(self.ids)
        kw["t"] = time.time()
        kw["rev"] = next(self.revs)
        self.items.append(kw)
        return kw

    def _touch(self, key, val, **kw):
        """Update the items whose key is val; the page fetches what changed since its last revision."""
        for i in self.items:
            if i.get(key) == val:
                i.update(kw, rev=next(self.revs))

    def state(self, fresh=False):
        """state(), at most 5 s old: the setup-step checks ask services and ports."""
        if fresh or time.time() - self._st[0] > 5:
            self._st = (time.time(), state(self.config()))
        return self._st[1]

    def view(self, since=0):
        st = self.state()
        with self.lock:
            return {"items": [i for i in self.items if i["rev"] > since], "rev": max((i["rev"] for i in self.items), default=0),
                    "busy": self.busy or bool(self.queue), "state": st,
                    "jobs": {k: {"what": j["what"], "rc": j["rc"], "tail": j["log"][-400:]} for k, j in self.jobs.items()}}

    # ── the operator ──
    def say(self, text):
        with self.lock:
            if self.busy or self.queue or any(j in self.jobs for j in self.waiting.values()):
                raise Bad("the assistant is still at work: wait for it to finish")
            for cid, pid in list(self.waiting.items()):        # moving on answers what was still open
                if pid in self.props and self.props[pid]["status"] == "pending":
                    self._decide(pid, False, "the operator moved on without deciding")
            text = text.strip()[:4000]
            self._item(kind="user", text=text)
            notes = "".join(f"(meanwhile the operator, through the page: {n})\n" for n in self.notes)
            self.notes.clear()
            self.msgs.append({"role": "user", "content": notes + text})
            self._kick()

    def decide(self, pid, approve, args=None):
        with self.lock:
            p = self.props.get(pid)
            if not p or p["status"] != "pending":
                raise Bad("no such open proposal")
            if args is not None:
                p["args"] = {k: v for k, v in args.items() if k in TOOLS[p["tool"]]["schema"]["function"]["parameters"]["properties"]}
            if not approve:
                return self._decide(pid, False, "rejected by the operator")
            p["status"] = "running"
        out = self._run(p["tool"], p["args"], p["call"])        # outside the lock: a probe or an install takes a while
        with self.lock:
            self._decide(pid, True, out)

    def act(self, tool, args):
        """A change the operator makes through a form, without the LLM: runs at once, and the LLM hears of it."""
        if tool not in TOOLS or TOOLS[tool]["readonly"]:
            raise Bad("no such action")
        out = self._run(tool, args, None)
        with self.lock:
            self.notes.append(f"{tool}({json.dumps(args)}) -> {str(out)[:300]}")
        return out

    # ── the LLM ──
    def _kick(self):
        self.queue.append(1)
        self.work.notify()

    def _worker(self):
        while True:
            with self.lock:
                while not self.queue:
                    self.work.wait()
                self.queue.clear()
                self.busy = True
            try:
                self._turns()
            except Exception as e:      # an LLM that is down must not take the page with it
                with self.lock:
                    self._item(kind="error", text=f"the assistant could not answer: {getattr(e, 'reason', e)}")
            finally:
                with self.lock:
                    self.busy = False

    def _turns(self):
        a = self.config().get("assistant") or {}
        if not a.get("enabled") or not a.get("url"):
            with self.lock:
                self._item(kind="error", text="no assistant configured: set one up first (step 1)")
            return
        for n in range(12):           # read-only tool rounds; the last goes without tools, so it must say something
            with self.lock:
                if self.waiting:
                    return
                msgs = [{"role": "system", "content": SYSTEM + "\n\nThe site now:\n" + json.dumps(self.state(fresh=True))}] + self.msgs
            m, model = llm.complete(a["url"], msgs, model=a.get("model") or None, key=llm._key(a.get("key_host")),
                                    timeout=int(a.get("timeout_seconds", 120)), max_tokens=900,
                                    tools=[t["schema"] for t in TOOLS.values()] if n < 11 else None)
            calls = m.get("tool_calls") or []
            text = (m.get("content") or "").strip()
            with self.lock:
                self.msgs.append({"role": "assistant", "content": m.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
                if text:
                    self._item(kind="assistant", text=text, model=model)
            if not calls:
                return
            for call in calls:
                self._call(call)

    def _call(self, call):
        f = call.get("function") or {}
        name, cid = f.get("name"), call.get("id") or f"call-{next(self.ids)}"
        try:
            args = json.loads(f.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = None
        if args is None or name not in TOOLS:
            with self.lock:
                return self._result(cid, f"error: no tool {name!r}" if args is not None else
                                    "error: the arguments are not a JSON object")
        if TOOLS[name]["readonly"]:
            out = self._run(name, args, cid, quiet=False)
            with self.lock:
                return self._result(cid, out)
        with self.lock:
            pid = f"p{next(self.ids)}"
            self.props[pid] = {"tool": name, "args": args, "status": "pending", "call": cid}
            self.waiting[cid] = pid
            self._item(kind="proposal", pid=pid, tool=name, args=args)

    def _result(self, cid, out):
        self.msgs.append({"role": "tool", "tool_call_id": cid,
                          "content": out if isinstance(out, str) else json.dumps(out)[:12000]})

    def _run(self, name, args, cid, quiet=True):
        try:
            out = TOOLS[name]["run"](self, args)
            ok = True
        except Bad as e:
            out, ok = f"refused: {e}", False
        except Exception as e:
            out, ok = f"error: {e}", False
        if not TOOLS[name]["readonly"] or not quiet:
            with self.lock:
                self._item(kind="tool", tool=name, args=args, ok=ok, out=out if isinstance(out, str) else json.dumps(out, indent=1)[:6000],
                       by="assistant" if cid else "operator")
        return out

    def _decide(self, pid, approve, out):
        """Close a proposal (lock held): its result goes to the LLM, or, for a job, when the job ends."""
        p = self.props[pid]
        p["status"] = "approved" if approve else "rejected"
        self._touch("pid", pid, status=p["status"], args=p["args"])
        cid = p["call"]
        if not approve:
            self._result(cid, out)
            self.waiting.pop(cid, None)
        else:
            if p["tool"] in JOB_TOOLS and isinstance(out, dict) and out.get("job"):
                self.waiting[cid] = out["job"]
                self.jobs[out["job"]]["call"] = cid
            else:
                self._result(cid, out)
                self.waiting.pop(cid, None)
        if not self.waiting:
            self._kick()

    # ── jobs: setup steps and installs, as `noctyx` subprocesses ──
    def start_job(self, what, argv):
        with self.lock:
            if any(j["rc"] is None for j in self.jobs.values()):
                raise Bad("a job is still running: wait for it")
            jid = f"j{next(self.ids)}"
            env = {**os.environ, "NOCTYX_NO_ASSIST": "1", "NOCTYX_CONFIG": CONFIG, "PYTHONUNBUFFERED": "1",
                   "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1])}
            proc = subprocess.Popen([sys.executable, "-m", "noctyx.cli", *argv], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, env=env)
            self.jobs[jid] = {"id": jid, "what": what, "argv": argv, "log": [], "rc": None, "call": None}
            self._item(kind="job", job=jid, what=what)
            threading.Thread(target=self._pump, args=(jid, proc), daemon=True).start()
            return {"job": jid, "started": what}

    def _pump(self, jid, proc):
        j = self.jobs[jid]
        for line in proc.stdout:
            with self.lock:
                j["log"].append(line.rstrip("\n"))
                del j["log"][:-3000]
        rc = proc.wait()
        with self.lock:
            j["rc"] = rc
            self._st = (0, None)
            self._touch("job", jid, rc=rc)
            cid = j.get("call")
            tail = "\n".join(j["log"][-60:])
            if cid and self.waiting.get(cid) == jid:
                self._result(cid, f"{j['what']}: exit {rc}\n{tail}")
                self.waiting.pop(cid)
                if not self.waiting:
                    self._kick()
            elif not cid:
                self.notes.append(f"{j['what']} finished with exit {rc}")
