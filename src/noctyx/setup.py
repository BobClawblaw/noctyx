"""First-time setup of a head node, step by step, on Linux or macOS.

    noctyx setup               where the site is, and the next command
    noctyx setup all           every step in order; stops at the first that fails
    noctyx setup <step>        tools · owner · mount · tls · repos · endpoint · units · escrow
    noctyx setup verify        prove it end to end: services answer, every repository opens, a canary
                               snapshot goes in through the endpoint and comes back out byte for byte

Every step can be re-run: it keeps what exists. A step that fails asks the site's assistant (set up first by
`noctyx init`) what went wrong, with the error, this machine's facts and the install guide.
"""
import base64
import bz2
import datetime
import getpass
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from . import components as comp, hostos, llm
from . import orchestrator as cm
from .paths import P

STEPS = ("tools", "owner", "mount", "tls", "repos", "endpoint", "units", "metrics", "privacy", "escrow")
BIN = pathlib.Path("/usr/local/bin")

class StepFailed(Exception):
    pass


def fail(msg):
    raise StepFailed(msg)


def _run(argv, **kw):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        fail(f"{argv[0]} is not installed")
    if r.returncode:
        fail(f"{' '.join(map(str, argv[:3]))}… failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def spindles(c):
    """{name: spindle} of the storage targets; skips the "_comment"-style entries a site config may carry."""
    return {k: v for k, v in (c.get("spindles") or {}).items() if isinstance(v, dict) and not k.startswith("_")}


def owner_exists():
    import pwd
    try:
        pwd.getpwnam(P["repo_owner"])
        return True
    except KeyError:
        return False


def _service(name):
    return f"{cm.UNIT}-{name}"


def _label():
    return P["launchd_prefix"]


def _log_dir():
    return str(pathlib.Path(P["job_logs"]).parent)


# ── tools: restic and rest-server, pinned and checked ───────────────────────
def _version(binary):
    """restic says `restic 0.19.1 compiled…` to `version`; rest-server says the same to `--version`."""
    for arg in ("version", "--version"):
        try:
            r = subprocess.run([binary, arg], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        v = next((w.lstrip("v") for w in (r.stdout or r.stderr).split() if re.fullmatch(r"v?\d+\.\d+(\.\d+)?", w)), None)
        if r.returncode == 0 and v:
            return v
    return None


def _fetch(component, os_name=None, arch=None):
    """A pinned release for this machine (or the given one: a node's), checked against its pinned SHA-256."""
    name, url, want = comp.asset(component, os_name or hostos.os_name(), arch or hostos.arch())
    print(f"  downloading {name}…", flush=True)
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        fail(f"{name}: sha256 {got} is not the pinned {want}; refusing it")
    return data


def _from_tar(blob, members):
    """{basename: bytes} of the named files in a release tarball."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        for m in t.getmembers():
            if m.isfile() and os.path.basename(m.name) in members:
                out[os.path.basename(m.name)] = t.extractfile(m).read()
    missing = set(members) - set(out)
    if missing:
        fail(f"the release has no {', '.join(sorted(missing))}")
    return out


def _restart_if_running(*services):
    for n in services:
        if hostos.service_installed(_service(n), _label()) and hostos.restart_service(_service(n), _label()):
            print(f"  restarted {_service(n)} on the new binary")


def _install_bin(path, data):
    tmp = path.with_suffix(".new")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    if hostos.IS_MAC:        # a downloaded binary carries no quarantine flag here, but clear it if a copy did
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(path)], capture_output=True)


def setup_tools(c, force=False):
    """restic and rest-server at the pinned versions in /usr/local/bin, checked against their SHA-256 sums.
    An existing binary is kept (with a note if its version differs) unless --force."""
    BIN.mkdir(parents=True, exist_ok=True)
    pinned = comp.PINS["restic"]["version"]
    want = str(c.get("restic_version") or pinned)
    have = shutil.which("restic")
    v = _version(have) if have else None
    if have and not force:
        print(f"  restic {v or '?'} at {have}: kept" + (f" (the site pins {want})" if v != want else ""))
    else:
        if want != pinned:
            fail(f"restic_version {want}: setup only knows the sums for {pinned}; install it yourself")
        _install_bin(BIN / "restic", bz2.decompress(_fetch("restic")))
        print(f"  restic {_version(str(BIN / 'restic'))} -> {BIN / 'restic'}")
    rs = BIN / "rest-server"
    if cm.rest_on_nas(c):
        print("  rest-server: the endpoint runs on the NAS (`noctyx nas-rest deploy` puts the NAS's build there)")
    elif rs.exists() and not force:
        print(f"  rest-server {_version(str(rs)) or '?'} at {rs}: kept")
    else:
        _install_bin(rs, _from_tar(_fetch("rest-server"), ["rest-server"])["rest-server"])
        print(f"  rest-server {_version(str(rs))} -> {rs}")
        _restart_if_running("rest-server.service")
    missing = [t for t in ("openssl", "htpasswd", "ssh", "curl") if not shutil.which(t)]
    if missing:
        hint = "" if hostos.IS_MAC else " (apt install " + " ".join(
            {"htpasswd": "apache2-utils", "ssh": "openssh-client"}.get(t, t) for t in missing) + ")"
        fail(f"missing: {', '.join(missing)}{hint}")
    print("  openssl, htpasswd, ssh, curl: present")


# ── owner: the system user that owns the repositories ───────────────────────
def setup_owner(c):
    """The local user that owns the repositories and runs the endpoint and the directory.
    server.repo_owner_uid pins its uid: a NAS's NFS share ACLs often refuse unknown uids, even as owner."""
    if not owner_exists():
        uid = hostos.create_system_user(P["repo_owner"], c["server"].get("repo_owner_uid"),
                                        home="/var/empty" if hostos.IS_MAC else P["state"])
        print(f"  created user {P['repo_owner']} (uid {uid or 'system'})")
    for d, mode in ((P["etc"], 0o750), (P["repo_keys"], 0o700), (P["repo_creds"], 0o700), (P["state"], 0o755),
                    (P["job_logs"], 0o750), (str(pathlib.Path(P["audit"]).parent), 0o755), (P["console_state"], 0o755)):
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)
        os.chmod(d, mode)
    shutil.chown(P["state"], P["repo_owner"], P["repo_owner"])
    shutil.chown(P["etc"], "root", P["repo_owner"])
    # the directory service runs as the owner and reads the site config (no secrets in it: keys live apart)
    shutil.chown(cm.CFG_PATH, "root", P["repo_owner"])
    os.chmod(cm.CFG_PATH, 0o640)
    print(f"  {P['repo_owner']}: ok; {P['etc']}, {P['repo_keys']}, {P['repo_creds']}, {P['state']}")


# ── mount: spindles that are a NAS share ────────────────────────────────────
def share_source(sh):
    if sh["proto"] == "nfs":
        return f"{sh['server']}:{sh['path']}"
    return f"//{sh['user'] + '@' if sh.get('user') else ''}{sh['server']}/{sh['path'].lstrip('/')}"


def mount_argv(sh, mount, cred_file=None):
    """The command that mounts a share at mount, on this platform. SMB passwords never go on it: macOS's
    mount_smbfs asks on the terminal, Linux reads a 0600 credentials file."""
    opts = sh.get("options") or ""
    if sh["proto"] == "nfs":
        o = opts or ("resvport,hard" if hostos.IS_MAC else "hard,_netdev")
        return ["mount", "-t", "nfs", "-o", o, share_source(sh), mount]
    if sh["proto"] == "smb":
        if hostos.IS_MAC:
            return ["mount_smbfs", *(["-o", opts] if opts else []), "-f", "0770", "-d", "0770", share_source(sh), mount]
        o = ",".join(x for x in (f"credentials={cred_file}", f"uid={P['repo_owner']}", f"gid={P['repo_owner']}",
                                  "file_mode=0660,dir_mode=0770", opts) if x)
        return ["mount", "-t", "cifs", "-o", o, f"//{sh['server']}/{sh['path'].lstrip('/')}", mount]
    fail(f"share proto {sh['proto']!r}: nfs or smb")


def setup_mount(c):
    """Mount every spindle that names a share and is not mounted; create every local spindle's directory.
    Then make repo_root and the HF store, owned by the repository owner."""
    if not owner_exists():
        fail(f"no user {P['repo_owner']}: run `noctyx setup owner` first")
    for name, sp in spindles(c).items():
        mount, sh = pathlib.Path(sp["mount"]), sp.get("share")
        if not sh:
            mount.mkdir(parents=True, exist_ok=True)
            print(f"  {name}: local directory {mount}")
            continue
        if os.path.ismount(mount):
            print(f"  {name}: {share_source(sh)} already mounted at {mount}")
            continue
        mount.mkdir(parents=True, exist_ok=True)
        argv, cred = None, None
        if sh["proto"] == "smb" and hostos.IS_MAC:
            # smbfs files belong to whoever mounted the share: mount it as the repository owner, so the
            # endpoint (which runs as that user) can write the repositories
            shutil.chown(mount, P["repo_owner"], P["repo_owner"])
            argv = hostos.as_user(P["repo_owner"], mount_argv(sh, str(mount)))
            print(f"  {name}: mounting {share_source(sh)} (the share's password is asked once)")
        elif sh["proto"] == "smb":
            cred = pathlib.Path(P["etc"]) / f"smb-{name}.cred"
            if not cred.exists():
                pw = getpass.getpass(f"  password for {sh.get('user')}@{sh['server']}: ")
                fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, f"username={sh.get('user', '')}\npassword={pw}\n".encode())
                os.close(fd)
        argv = argv or mount_argv(sh, str(mount), cred)
        r = subprocess.run(argv, text=True, capture_output=not (sh["proto"] == "smb" and hostos.IS_MAC))
        if r.returncode:
            fail(f"mount {share_source(sh)} at {mount}: {(r.stderr or '').strip() or f'rc={r.returncode}'}")
        print(f"  {name}: {share_source(sh)} mounted at {mount}")
        if not hostos.IS_MAC:
            print(f"    to mount it at boot, add to /etc/fstab:\n    {_fstab(sh, mount, cred)}")
    for d in (cm.repo_root(c), cm.hf_cfg(c)["store"]):
        p = pathlib.Path(d)
        if not p.is_dir():
            r = subprocess.run(hostos.as_user(P["repo_owner"], ["mkdir", "-p", str(p)]), capture_output=True, text=True)
            if r.returncode:
                p.mkdir(parents=True, exist_ok=True)
                shutil.chown(p, P["repo_owner"], P["repo_owner"])
        if subprocess.run(hostos.as_user(P["repo_owner"], ["test", "-w", str(p)])).returncode:
            fail(f"{p} is not writable by {P['repo_owner']}" + (
                 " — the NAS refuses its uid: set server.repo_owner_uid to the NAS user's uid, or allow it on the share"
                 if any(sp.get("share") for sp in spindles(c).values()) else ""))
        print(f"  {p}: writable by {P['repo_owner']}")


def _fstab(sh, mount, cred):
    if sh["proto"] == "nfs":
        return f"{share_source(sh)} {mount} nfs {sh.get('options') or 'hard,_netdev'} 0 0"
    return (f"//{sh['server']}/{sh['path'].lstrip('/')} {mount} cifs credentials={cred},uid={P['repo_owner']},"
            f"gid={P['repo_owner']},file_mode=0660,dir_mode=0770,_netdev 0 0")


# ── tls ─────────────────────────────────────────────────────────────────────
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
        # CA extensions and key identifiers: Python 3.13+ verifies in strict X.509 mode, which refuses a chain
        # without them (and LibreSSL's `req -x509`, macOS's openssl, adds none by itself)
        conf = ca.with_suffix(".cnf")
        conf.write_text("[req]\ndistinguished_name = dn\nx509_extensions = v3_ca\n[dn]\n[v3_ca]\n"
                        "basicConstraints = critical, CA:TRUE\nkeyUsage = critical, keyCertSign, cRLSign\n"
                        "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n")
        _run(["openssl", "req", "-x509", "-new", "-nodes", "-key", str(ca_key), "-sha256", "-days", "3650",
              "-config", str(conf), "-subj", "/O=Noctyx/CN=Noctyx site CA", "-out", str(ca)])
        conf.unlink()
        print(f"  CA made: {ca}")
        force = True
    if force or not crt.exists() or subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-checkend",
                                                    str(30 * 86400)], capture_output=True).returncode or not _strict_ok(crt):
        san = [f"IP:{s['address']}", f"DNS:{s.get('hostname') or 'head'}", "DNS:localhost", "IP:127.0.0.1",
               *[str(x) for x in (tls.get("san") or [])]]
        ext = crt.with_suffix(".ext")
        ext.write_text("basicConstraints = CA:FALSE\nkeyUsage = critical, digitalSignature, keyEncipherment\n"
                       "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid, issuer\n"
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
    if owner_exists():          # the endpoint and the directory run as the repository owner
        shutil.chown(ca.parent, "root", P["repo_owner"]); shutil.chown(key, "root", P["repo_owner"])
        os.chmod(key, 0o640)
    else:
        os.chmod(key, 0o600)
        print(f"  note: no user {P['repo_owner']} yet; run `noctyx setup owner`, then this again")
    # the CA is public: put it where this machine's tools look, so --cacert is not needed on the head
    if hostos.trust_ca(ca, P["unit_prefix"]):
        print("  CA added to this machine's trust store")
    elif hostos.IS_MAC:
        print(f"  optional, for browsers and curl on this Mac (Noctyx itself does not need it), in Terminal:\n"
              f"    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain {ca}")
    end = _run(["openssl", "x509", "-in", str(crt), "-noout", "-enddate"]).stdout.strip().split("=", 1)[-1]
    print(f"  the certificate expires {end}: `noctyx setup tls` again renews it (the CA stays)")


def _strict_ok(crt):
    """Does a strict X.509 client (Python 3.13+) accept this leaf? It wants an Authority Key Identifier; a leaf
    made before setup added one is renewed under the same CA, so no node needs re-installing."""
    txt = subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-text"], capture_output=True, text=True).stdout
    return "Authority Key Identifier" in txt


# ── repos ───────────────────────────────────────────────────────────────────
def setup_repos(c):
    """One restic repository per entry in "repositories": its key (32 random bytes), its endpoint login
    (hex: it goes into a rest: URL, where base64's + / = break parsing) and a bcrypt htpasswd line
    (rest-server refuses htpasswd's default MD5-crypt and every request 401s). Existing ones are kept."""
    for tool in ("restic", "htpasswd"):
        if not shutil.which(tool):
            fail(f"{tool} is not installed: run `noctyx setup tools`")
    if not owner_exists():
        fail(f"no user {P['repo_owner']}: run `noctyx setup owner` first")
    root = pathlib.Path(cm.repo_root(c))
    if not root.is_dir():
        fail(f"repo_root {root} is not there: run `noctyx setup mount` (is the NAS mounted?)")
    htp = pathlib.Path(P["etc"]) / "htpasswd"
    if not htp.exists():
        htp.touch(mode=0o640)
    shutil.chown(htp, "root", P["repo_owner"]); os.chmod(htp, 0o640)
    made = kept = 0
    for r in c["repositories"]:
        rid, user = r["id"], f"{P['repo_dir_prefix']}{r['id']}"
        path = pathlib.Path(cm.repo_dir(c, rid))
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
            _run(hostos.as_user(P["repo_owner"], ["sh", "-c", 'mkdir -p "$1" && exec restic init --repo "$1"', "_", str(path)]),
                 env={**os.environ, "RESTIC_PASSWORD": keyf.read_text().strip(), "HOME": "/tmp",
                      "RESTIC_CACHE_DIR": "/tmp/noctyx-restic-cache"})
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
                fail(f"htpasswd {user}: {r_.stderr.strip()}")
    cm.audit("setup-repos", made=made, kept=kept)
    print(f"  repositories: {made} made, {kept} already there")
    if made:
        print("  NEXT: `noctyx setup escrow` and keep the bundle OFF this machine. Without these keys every\n"
              "  backup is unreadable, permanently.")


# ── escrow ──────────────────────────────────────────────────────────────────
def setup_escrow(c):
    """An encrypted bundle of every key and credential and the site config (AES-256, PBKDF2), its passphrase
    typed by you and stored nowhere. Copy it off this machine and off the NAS."""
    if not sys.stdin.isatty():
        fail("escrow asks for a passphrase on the terminal: run `sudo noctyx setup escrow` in one")
    out = pathlib.Path(P["etc"]) / "escrow"
    out.mkdir(mode=0o700, exist_ok=True)
    bundle = out / f"noctyx-keys-{datetime.date.today():%Y%m%d}.tar.gz.enc"
    etc = pathlib.Path(P["etc"])
    parts = [str(pathlib.Path(p).relative_to(etc)) for p in (P["repo_keys"], P["repo_creds"], P["tls"],
                                                              f"{P['etc']}/htpasswd")
             if pathlib.Path(p).exists() and pathlib.Path(p).is_relative_to(etc)]
    cfg = pathlib.Path(cm.CFG_PATH)
    if cfg.is_relative_to(etc):
        parts.append(str(cfg.relative_to(etc)))
        extra = []
    else:                    # a config kept elsewhere goes in at the bundle's top level
        extra = ["-C", str(cfg.parent), cfg.name]
    tar = subprocess.Popen(["tar", "-C", str(etc), "-czf", "-", *parts, *extra], stdout=subprocess.PIPE)
    enc = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-out", str(bundle)],
                         stdin=tar.stdout)      # openssl asks for the passphrase on the terminal, twice
    tar.wait()
    if enc.returncode or tar.returncode:
        fail("escrow failed")
    os.chmod(bundle, 0o600)
    cm.audit("setup-escrow", bundle=str(bundle))
    print(f"  {bundle}\n  1. copy it off this machine and off the NAS\n"
          f"  2. check it opens somewhere else:  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in {bundle.name} | tar tz")


# ── endpoint and units ──────────────────────────────────────────────────────
def setup_endpoint(c):
    """The repository endpoint: rest-server on the head as the repository owner, or on the NAS."""
    if cm.rest_on_nas(c):
        print(f"  the endpoint runs on the NAS ({c['server']['rest_host']}): deploying it there")
        r = subprocess.run([sys.executable, "-m", "noctyx.cli", "nas-rest", "deploy"],
                           env={**os.environ, "PYTHONPATH": str(cm.NX_HOME.parent)})
        if r.returncode:
            fail("noctyx nas-rest deploy failed (above)")
        return
    if not (BIN / "rest-server").exists():
        fail("rest-server is not installed: run `noctyx setup tools`")
    name = f"{cm.UNIT}-rest-server.service"
    hostos.install_services({name: cm.render_unit(c, "rest-server.service")}, cm.UNIT, _label(), _log_dir())
    for _ in range(10):
        time.sleep(1)
        if cm.rest_up(c):
            break
    up = cm.rest_up(c)
    cm.audit("setup-rest-server", up=up)
    if not up:
        fail(f"{name}: endpoint {cm.rest_url(c)} DOWN\n{_service_log(name)}")
    print(f"  {name}: endpoint {cm.rest_url(c)} UP")


def _service_log(name):
    """The service's last log lines and the service manager's view of it: what a person (or the assistant)
    needs to see why it is down."""
    hint = hostos.logs_hint(name, _label(), _log_dir())
    out = subprocess.run(["sh", "-c", hint + " 2>&1"], capture_output=True, text=True).stdout.strip()[-1500:]
    if hostos.IS_MAC:
        st = subprocess.run(["launchctl", "print", f"system/{hostos.launchd_label(name, _label())}"],
                            capture_output=True, text=True).stdout
        out += "\nlaunchctl: " + "; ".join(l.strip() for l in st.splitlines()
                                           if re.search(r"^\s*(state|last exit code|pid) =", l))[:300]
    return f"$ {hint}\n{out or '(empty)'}"


def head_units(c):
    return {f"{cm.UNIT}-{n}": cm.render_unit(c, n) for n in cm.HEAD_UNITS}


def setup_units(c):
    """The head's services and timers: console, directory, HF mirror; tick, nightly, hygiene, retention."""
    done = hostos.install_services(head_units(c), cm.UNIT, _label(), _log_dir())
    cm.audit("install-head", units=done)
    print(f"  installed and enabled: {', '.join(done)}")
    time.sleep(3)
    down = [n for n in ("console", "directory", "hf-mirror")
            if not hostos.service_active(_service(f"{n}.service"), _label())]
    if down:
        fail("not running: " + ", ".join(down) + "\n" + "\n".join(_service_log(_service(f"{n}.service")) for n in down))


# ── metrics: Prometheus for the console's live numbers ──────────────────────
PROM = "http://127.0.0.1:9090"
METRICS_UNITS = ("prometheus.service", "node-exporter.service", "textfile.service", "textfile.timer",
                 "llm-discover.service", "llm-discover.timer")
PROM_MARK = "# managed by noctyx setup metrics"


def _prom_ready():
    try:
        with urllib.request.urlopen(f"{PROM}/-/ready", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _head_name(c):
    s = c["server"]
    return next((h["name"] for h in c.get("hosts", []) if h.get("local") or h.get("address") == s.get("address")),
                s.get("hostname") or "head")


def _prom_dir():
    return pathlib.Path(P["prom_config"]).parent


def prometheus_yml(c):
    """The head's scrape config: the console, the head's own exporter, every node's exporter (targets kept by
    llm-discover), the LLM servers, and Noctyx's alert rules."""
    d, head = _prom_dir(), _head_name(c)
    ca = d / f"{P['unit_prefix']}-ca.crt"
    return f"""{PROM_MARK}: it rewrites this file; change the site config instead
global:
  scrape_interval: 15s
  evaluation_interval: 15s
rule_files:
  - {d}/{P['unit_prefix']}-rules.yml
scrape_configs:
  - job_name: {P['prom_job_console']}
    scheme: https
    tls_config:
      ca_file: {ca}
    static_configs:
      - targets: ['127.0.0.1:{c['server'].get('ui_port', 20996)}']
        labels:
          host: {head}
  - job_name: node
    static_configs:
      - targets: ['127.0.0.1:{cm.HEAD_EXPORTER_PORT}']
        labels:
          host: {head}
  - job_name: {P['prom_job_nodes']}
    scheme: https
    tls_config:
      ca_file: {ca}
    basic_auth:
      username: prometheus
      password_file: {d}/node-exporter.pass
    file_sd_configs:
      - files: ['{P['prom_node_targets']}']
  - job_name: llm
    scrape_interval: 15s
    scrape_timeout: 10s
    file_sd_configs:
      - files: ['{P['prom_llm_targets']}']
"""


def _install_release(component, members, force):
    """-> True when the binaries were (re)placed."""
    have = [m for m in members if (BIN / m).exists()]
    if len(have) == len(members) and not force:
        print(f"  {component}: kept ({', '.join(str(BIN / m) for m in members)})")
        return False
    for m, data in _from_tar(_fetch(component), members).items():
        _install_bin(BIN / m, data)
    print(f"  {component} {comp.PINS[component]['version']} -> {', '.join(str(BIN / m) for m in members)}")
    return True


def _write(path, text, mode, group=None):
    path = pathlib.Path(path)
    path.write_text(text)
    os.chmod(path, mode)
    if group:
        shutil.chown(path, "root", group)


def setup_metrics(c, force=False):
    """Prometheus on 127.0.0.1:9090 for the console's live numbers, the head's own node_exporter (loopback
    only), the GPU/power collector and LLM discovery. A head that already runs a Prometheus keeps it: the
    jobs to add to it are printed instead."""
    if not owner_exists():
        fail(f"no user {P['repo_owner']}: run `noctyx setup owner` first")
    ours = hostos.service_installed(_service("prometheus.service"), _label())
    if _prom_ready() and not ours:
        print(f"  a Prometheus already answers on {PROM}: kept. Give it these jobs (as in {P['prom_config']}):\n")
        print("\n".join("    " + l for l in prometheus_yml(c).split("scrape_configs:\n", 1)[1].splitlines()))
        return
    cfgf = pathlib.Path(P["prom_config"])
    if cfgf.exists() and PROM_MARK not in cfgf.read_text():
        if not force:
            fail(f"{cfgf} exists and is not Noctyx's: move it aside, or `noctyx setup metrics --force` replaces it")
        keep = cfgf.with_name(cfgf.name + ".pre-noctyx")
        shutil.copy2(cfgf, keep)
        print(f"  {cfgf} is not Noctyx's: kept a copy at {keep}")
    new_prom = _install_release("prometheus", ["prometheus", "promtool"], force)
    new_exp = _install_release("node_exporter", ["node_exporter"], force)
    d = _prom_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)
    owner = P["repo_owner"]
    for p, mode, who in ((P["prom_data"], 0o750, owner), (P["textfile_dir"], 0o755, "root")):
        pathlib.Path(p).mkdir(parents=True, exist_ok=True)
        os.chmod(p, mode)
        shutil.chown(p, who, owner if who == owner else None)
    _write(d / f"{P['unit_prefix']}-ca.crt", pathlib.Path(c["server"]["tls"]["ca"]).read_text(), 0o644)
    pw = d / "node-exporter.pass"
    if not pw.exists():
        import secrets
        _write(pw, secrets.token_hex(24) + "\n", 0o640, owner)
    else:
        shutil.chown(pw, "root", owner); os.chmod(pw, 0o640)
    for t in (P["prom_node_targets"], P["prom_llm_targets"]):
        if not pathlib.Path(t).exists():
            _write(t, "[]\n", 0o644)
    _write(d / f"{P['unit_prefix']}-rules.yml", cm.render_site(c, (cm.NX_HOME / "metrics" / "rules.yml").read_text()), 0o644)
    _write(cfgf, prometheus_yml(c), 0o644)
    collector = "mac-textfile.sh" if hostos.IS_MAC else "node-textfile.sh"
    _write(BIN / P["textfile_unit"], cm.render_site(c, (cm.NX_HOME / "metrics" / collector).read_text()), 0o755)
    r = subprocess.run([str(BIN / "promtool"), "check", "config", str(cfgf)], capture_output=True, text=True)
    if r.returncode:
        fail(f"promtool rejects {cfgf}: {(r.stdout + r.stderr).strip()[-600:]}")
    done = hostos.install_services({_service(n): cm.render_unit(c, n) for n in METRICS_UNITS}, cm.UNIT, _label(), _log_dir())
    print(f"  installed and started: {', '.join(done)}")
    if ours:                 # services that were already running keep the old binary until restarted
        _restart_if_running(*(["prometheus.service"] if new_prom else []), *(["node-exporter.service"] if new_exp else []))
    for _ in range(30):
        if _prom_ready():
            break
        time.sleep(1)
    else:
        fail(f"Prometheus is not ready on {PROM}\n{_service_log(_service('prometheus.service'))}")
    if ours:                 # a running Prometheus re-reads a changed config on /-/reload
        urllib.request.urlopen(urllib.request.Request(f"{PROM}/-/reload", method="POST"), timeout=10)
    if hostos.IS_MAC and not shutil.which("macmon"):
        print("  optional: `brew install macmon` adds CPU and GPU temperatures (powermetrics has none on Apple silicon)")
    time.sleep(20)           # one scrape round
    for job, host, health, err in _targets():
        print(f"  {'✓' if health == 'up' else '·'} {job:<14} {host:<14} {health}{': ' + err if err else ''}")


def _targets():
    """[(job, host, health, error)] of every Prometheus target."""
    try:
        with urllib.request.urlopen(f"{PROM}/api/v1/targets?state=active", timeout=8) as r:
            d = json.loads(r.read())
    except Exception:
        return []
    return sorted((t["labels"].get("job", "?"), t["labels"].get("host", t["labels"].get("instance", "?")),
                   t.get("health", "?"), (t.get("lastError") or "")[:120]) for t in d["data"]["activeTargets"])


# ── privacy (macOS): Full Disk Access for the daemons that read the repositories ──
def _privacy_needs(c):
    """[(what, binary)] that need Full Disk Access on this Mac; [] on Linux or when nothing does."""
    if not hostos.IS_MAC:
        return []
    if not any(hostos.off_boot_volume(sp["mount"]) for sp in spindles(c).values()):
        return []
    need = [] if cm.rest_on_nas(c) else [("the repository endpoint", str(BIN / "rest-server"))]
    need.append(("Noctyx's scheduler, retention, mirror and console (and the restic they run)",
                 hostos.real_executable(sys.executable)))
    return need


def _endpoint_reads(c):
    """Can the endpoint read a repository? An authenticated GET of its config: 200, or why not."""
    s = c["server"]
    r = (c.get("repositories") or [None])[0]
    if not r:
        return True, "no repository"
    user = f"{P['repo_dir_prefix']}{r['id']}"
    try:
        pw = (pathlib.Path(P["repo_creds"]) / f"{user}.pass").read_text().strip()
    except OSError:
        return False, "no endpoint login yet (noctyx setup repos)"
    req = urllib.request.Request(f"{cm.rest_url(c)}/{user}/{r['id']}/config",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()})
    try:
        _https_req(req, s["tls"]["ca"])
        return True, "reads the repositories"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}" + (" (it cannot read the share: no Full Disk Access)" if e.code == 500 and hostos.IS_MAC else "")
    except Exception as e:
        return False, str(getattr(e, "reason", e))


def _python_reads(c):
    out = hostos.launchd_probe(f"{_label()}.privacy-probe", P["repo_owner"] if owner_exists() else "root",
                               [sys.executable, "-c", "import os,sys\ntry:\n os.listdir(sys.argv[1]); print('ok')\n"
                                "except OSError as e:\n print('ERR', e)", cm.repo_root(c)])
    last = out.strip().splitlines()[-1]
    return last == "ok", "reads the share" if last == "ok" else last[-200:]


def privacy_state(c):
    """[(what, binary, ok, detail)] for every binary that needs Full Disk Access."""
    out = []
    for what, binary in _privacy_needs(c):
        ok, detail = _endpoint_reads(c) if binary.endswith("rest-server") else _python_reads(c)
        out.append((what, binary, ok, detail))
    return out


def setup_privacy(c):
    """macOS guards network shares, external disks and people's own folders from background processes. The
    daemons that read the repositories need Full Disk Access, which only a person at the Mac can give."""
    if not hostos.IS_MAC:
        print("  nothing to do on Linux")
        return
    st = privacy_state(c)
    if not st:
        print("  nothing needs Full Disk Access (the repositories are on this Mac's own disk)")
        return
    while True:
        for what, binary, ok, detail in st:
            print(f"  {'✓' if ok else '✗'} {binary}\n      {what}: {detail}")
        missing = [b for _, b, ok, _ in st if not ok]
        if not missing:
            print("  Full Disk Access: granted")
            return
        print("\n  macOS needs you at this Mac for this one (no command can grant it):\n"
              "    System Settings › Privacy & Security › Full Disk Access › + , then add:")
        for b in missing:
            print(f"      {b}")
        print("    (in the file picker, ⌘⇧G takes a path). Switch each one on.")
        if hostos.open_for_console_user(hostos.FULL_DISK_ACCESS_PANE):
            for b in missing:
                hostos.open_for_console_user("-R", b)
            print("    The settings pane and the binaries in Finder are open on the Mac's screen.")
        if not sys.stdin.isatty():
            fail("Full Disk Access is missing for: " + ", ".join(missing) + " — grant it, then `sudo noctyx setup privacy`")
        input("\n  press Enter once they are switched on… ")
        for b in missing:      # a running daemon keeps the verdict it got: restart them
            for n in ("rest-server", "hf-mirror", "console"):
                hostos.restart_service(_service(f"{n}.service"), _label())
        time.sleep(3)
        st = privacy_state(c)


# ── status ──────────────────────────────────────────────────────────────────
def _crt_ok(f):
    return pathlib.Path(f).exists() and not subprocess.run(
        ["openssl", "x509", "-in", f, "-noout", "-checkend", str(30 * 86400)], capture_output=True).returncode


def _escrow_state():
    d = pathlib.Path(P["etc"]) / "escrow"
    escrow = sorted(d.glob("*.enc")) if d.is_dir() else []
    newest = max((f.stat().st_mtime for f in escrow), default=0)
    kd = pathlib.Path(P["repo_keys"])
    stale = [f for f in (kd.glob("*.key") if escrow and kd.is_dir() else []) if f.stat().st_mtime > newest]
    return escrow, stale


def _spindles_ready(c):
    sps = spindles(c).values()
    return all((os.path.ismount(s["mount"]) if s.get("share") else os.path.isdir(s["mount"])) for s in sps) \
        and os.path.isdir(cm.repo_root(c))


def steps_state(c):
    tls = c["server"]["tls"]
    repos = c.get("repositories") or []
    inited = [r["id"] for r in repos if pathlib.Path(cm.repo_dir(c, r["id"]), "config").exists()]
    escrow, stale = _escrow_state()
    rs_ok = cm.rest_on_nas(c) or (BIN / "rest-server").exists()
    return [
        ("tools", bool(shutil.which("restic")) and rs_ok and bool(shutil.which("htpasswd")),
         f"restic {comp.PINS['restic']['version']}, rest-server {comp.PINS['rest-server']['version']}, openssl, htpasswd"),
        ("owner", owner_exists(), f"the repository owner {P['repo_owner']}"),
        ("mount", _spindles_ready(c), "storage: " + ", ".join(
            f"{n} {s.get('share') and share_source(s['share']) or 'local'} at {s['mount']}"
            for n, s in spindles(c).items())),
        ("tls", pathlib.Path(tls["ca"]).exists() and _crt_ok(tls["cert"]), "the site CA and the head's certificate"),
        ("repos", len(inited) == len(repos), f"repositories ({len(inited)}/{len(repos)} made)" if repos
         else "repositories: none yet (add the Linux hosts to back up: noctyx init)"),
        ("endpoint", cm.rest_up(c), f"the repository endpoint {cm.rest_url(c)}"),
        ("units", hostos.service_active(_service("console.service"), _label()),
         "the head's services (console, directory, mirror, timers)"),
        ("metrics", _prom_ready(), "Prometheus on 127.0.0.1:9090: the console's live numbers"),
        *([("privacy", all(ok for *_, ok, _ in privacy_state(c)), "Full Disk Access for the daemons that read the repositories")]
          if _privacy_needs(c) else []),
        ("escrow", bool(escrow) and not stale, "an escrow bundle of the keys"
         + (f" (newest {escrow[-1].name})" if escrow else "") + (f": {len(stale)} key(s) made since, re-run it" if stale else "")),
    ]


def status(c):
    a = llm.configured()
    print(f"  {'✓' if a else '·'} {'assistant':<9} " + (f"{a['url']} {a.get('model') or ''}".rstrip() if a
                                                          else "none (noctyx init sets one up first)"))
    nxt = None
    for name, ok, what in steps_state(c):
        print(f"  {'✓' if ok else '·'} {name:<9} {what}")
        nxt = nxt or (None if ok else name)
    if nxt:
        print(f"\nnext: sudo noctyx setup {nxt}      (or every remaining step: sudo noctyx setup all)")
    else:
        print("\nthe head is set up. Prove it:  sudo noctyx setup verify\n"
              "then add nodes:  sudo noctyx install-agent --host <name>")


# ── verify ──────────────────────────────────────────────────────────────────
class Checks:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail, fix=None, warn=False):
        mark = "✓" if ok else ("!" if warn else "✗")
        self.rows.append((mark, name, detail, fix))
        print(f"  {mark} {name:<22} {detail}" + (f"\n    → {fix}" if fix and not ok else ""), flush=True)
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if r[0] == "✗"]


def _https(url, ca, timeout=8):
    try:
        return _https_req(url, ca, timeout)
    except urllib.error.HTTPError as e:
        return e.code, b""


def _https_req(req, ca, timeout=8):
    with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=ca), timeout=timeout) as r:
        return r.status, r.read(4096)


def _repo_file(c, repo):
    """A 0600 temp file holding the repository's endpoint URL (it embeds the login: never on a command line)."""
    s = c["server"]
    user = f"{P['repo_dir_prefix']}{repo}"
    pw = (pathlib.Path(P["repo_creds"]) / f"{user}.pass").read_text().strip()
    fd, path = tempfile.mkstemp(prefix="noctyx-repo-")
    os.write(fd, f"rest:https://{user}:{pw}@{s.get('rest_host') or s['address']}:{s['rest_port']}/{user}/{repo}".encode())
    os.close(fd)
    return path


def _restic(c, repo, args, timeout=300, **kw):
    rf = _repo_file(c, repo)
    try:
        return subprocess.run(["restic", "--cacert", c["server"]["tls"]["ca"], "--repository-file", rf,
                               "--password-file", f"{P['repo_keys']}/{repo}.key", "--no-cache", *args],
                              capture_output=True, text=True, timeout=timeout, **kw)
    finally:
        os.unlink(rf)


def _time_machine():
    """-> (fine, what): this Mac's Time Machine destination and how old its newest backup is."""
    dest = next((l.split(":", 1)[1].strip() for l in subprocess.run(["tmutil", "destinationinfo"], capture_output=True,
                 text=True).stdout.splitlines() if l.startswith("Name")), None)
    if not dest:
        return False, "no destination set"
    latest = subprocess.run(["tmutil", "latestbackup"], capture_output=True, text=True).stdout.strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{6})", latest)
    if not m:
        return False, f"backs up to {dest!r}, no finished backup found"
    age = time.time() - datetime.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S").timestamp()
    return age < 2 * 86400, f"backs up to {dest!r}, last {cm.fmt_duration(age)} ago"


def canary(c, repo):
    """A small random tree backed up through the endpoint, restored, compared byte for byte; the snapshot
    is then forgotten on the head (the endpoint is append-only, so clients cannot)."""
    with tempfile.TemporaryDirectory(prefix="noctyx-canary-") as td:
        src = pathlib.Path(td, "src", "noctyx-canary")
        src.mkdir(parents=True)
        (src / "random.bin").write_bytes(os.urandom(1 << 20))
        (src / "note.txt").write_text(f"noctyx verify canary {datetime.datetime.now().isoformat()}\n")
        want = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in src.iterdir()}
        r = _restic(c, repo, ["backup", "--json", "--tag", "noctyx-verify", "--host", "noctyx-verify", str(src)])
        if r.returncode:
            return False, f"backup through the endpoint failed: {(r.stderr or r.stdout).strip()[-300:]}"
        snap = next((json.loads(l).get("snapshot_id") for l in r.stdout.splitlines()
                     if l.startswith("{") and '"summary"' in l), None)
        if not snap:
            return False, "backup gave no snapshot id"
        out = pathlib.Path(td, "out")
        r = _restic(c, repo, ["restore", snap, "--target", str(out)])
        if r.returncode:
            return False, f"restore of {snap[:8]} failed: {(r.stderr or r.stdout).strip()[-300:]}"
        back = next((p for p in out.rglob("noctyx-canary") if p.is_dir()), None)
        got = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in back.iterdir()} if back else {}
        if got != want:
            return False, f"restored files differ from the originals (snapshot {snap[:8]})"
        f = subprocess.run(cm.as_owner(["restic", "--repo", cm.repo_dir(c, repo), "--no-cache", "forget", snap]),
                           capture_output=True, text=True, env={**os.environ, "HOME": "/tmp",
                           "RESTIC_PASSWORD": pathlib.Path(P["repo_keys"], f"{repo}.key").read_text().strip()})
        return True, f"1 MiB in and back out of {repo} byte for byte (snapshot {snap[:8]}" + \
            (", forgotten)" if f.returncode == 0 else ", left in place: forget failed)")


def verify(c, run_canary=True):
    s = c["server"]
    ca = s["tls"]["ca"]
    k = Checks()
    print("site")
    head = [h for h in c.get("hosts", []) if h.get("address") == s.get("address") or h.get("local")]
    k.add("config", True, f"{cm.CFG_PATH}: {len(c.get('hosts', []))} host(s), {len(c.get('repositories', []))} repo(s)")
    rd = owner_exists() and subprocess.run(hostos.as_user(P["repo_owner"], ["test", "-r", cm.CFG_PATH])).returncode == 0
    k.add("config readable", rd, f"by {P['repo_owner']} (the directory service reads it)", "sudo noctyx setup owner")
    k.add("head host", bool(head), head[0]["name"] if head else "no host entry has the head's address",
          "add the head to hosts (noctyx init does)", warn=True)
    a = llm.configured()
    if a:
        t = llm.test(a["url"], a.get("model") or None, llm._key(a.get("key_host")), timeout=int(a.get("timeout_seconds", 60)))
        k.add("assistant", t["ok"], f"{t.get('model')} answered in {t.get('ms')} ms" if t["ok"] else t.get("error", ""),
              "noctyx init --assistant-only, or noctyx assistant-set", warn=True)
    else:
        k.add("assistant", False, "none configured", "noctyx init --assistant-only", warn=True)

    print("tools")
    v = _version("restic") if shutil.which("restic") else None
    k.add("restic", bool(v), f"{v} at {shutil.which('restic')}" if v else "missing", "sudo noctyx setup tools")
    if not cm.rest_on_nas(c):
        rv = _version(str(BIN / "rest-server"))
        k.add("rest-server", bool(rv), f"{rv}" if rv else "missing", "sudo noctyx setup tools")
    for t in ("openssl", "htpasswd"):
        k.add(t, bool(shutil.which(t)), shutil.which(t) or "missing", "sudo noctyx setup tools")

    print("storage")
    k.add("owner", owner_exists(), P["repo_owner"], "sudo noctyx setup owner")
    for n, sp in spindles(c).items():
        sh = sp.get("share")
        ok = os.path.ismount(sp["mount"]) if sh else os.path.isdir(sp["mount"])
        k.add(f"spindle {n}", ok, f"{share_source(sh) + ' at ' if sh else 'local '}{sp['mount']}"
              + ("" if ok else (" not mounted" if sh else " missing")), "sudo noctyx setup mount")
    rr = cm.repo_root(c)
    w = owner_exists() and subprocess.run(hostos.as_user(P["repo_owner"], ["test", "-w", rr])).returncode == 0
    k.add("repo_root", w, f"{rr} writable by {P['repo_owner']}" if w else f"{rr} not writable by {P['repo_owner']}",
          "sudo noctyx setup mount")

    print("tls")
    k.add("CA", os.path.exists(ca), ca, "sudo noctyx setup tls")
    ok = _crt_ok(s["tls"]["cert"])
    k.add("certificate", ok, "valid for 30+ days" if ok else "missing or expiring", "sudo noctyx setup tls")
    if ok and os.path.exists(ca):
        r = subprocess.run(["openssl", "verify", "-CAfile", ca, s["tls"]["cert"]], capture_output=True, text=True)
        k.add("chain", r.returncode == 0, "signed by the site CA" if r.returncode == 0 else r.stdout.strip()[-120:],
              "sudo noctyx setup tls --force")
        txt = subprocess.run(["openssl", "x509", "-in", s["tls"]["cert"], "-noout", "-text"], capture_output=True, text=True).stdout
        k.add("names", f"IP Address:{s['address']}" in txt, f"the certificate names {s['address']}",
              "the head's address changed: sudo noctyx setup tls --force")
        k.add("strict clients", "Authority Key Identifier" in txt, "key identifiers present (Python 3.13+ accepts it)",
              "sudo noctyx setup tls   (renews the certificate, keeps the CA)")

    print("repositories")
    up = cm.rest_up(c)
    k.add("endpoint", up, cm.rest_url(c), "sudo noctyx setup endpoint")
    if up:
        reads, why = _endpoint_reads(c)
        k.add("endpoint reads", reads, why, "sudo noctyx setup privacy" if hostos.IS_MAC else "sudo noctyx setup mount")
        up = reads
    for what, binary, ok, detail in privacy_state(c) if hostos.IS_MAC else []:
        if not binary.endswith("rest-server"):
            k.add("full disk access", ok, f"{binary}: {detail}", "sudo noctyx setup privacy")
    for r in c.get("repositories") or []:
        rid = r["id"]
        if not (pathlib.Path(P["repo_keys"]) / f"{rid}.key").exists():
            k.add(f"repo {rid}", False, "no key", "sudo noctyx setup repos")
            continue
        if not up:
            k.add(f"repo {rid}", False, "endpoint down: not tried")
            continue
        try:
            res = _restic(c, rid, ["cat", "config"], timeout=60)
        except subprocess.TimeoutExpired:
            k.add(f"repo {rid}", False, "restic got no answer in 60 s (it retries what the endpoint refuses)",
                  "sudo noctyx setup repos")
            continue
        k.add(f"repo {rid}", res.returncode == 0, "opens through the endpoint with its key and login" if res.returncode == 0
              else (res.stderr or res.stdout).strip()[-200:], "sudo noctyx setup repos")

    print("services")
    port = s.get("ui_port", 20996)
    for n in ("console", "directory", "hf-mirror", "rest-server"):
        if n == "rest-server" and cm.rest_on_nas(c):
            continue
        unit = _service(f"{n}.service")
        k.add(n, hostos.service_active(unit, _label()), "running" if hostos.service_active(unit, _label())
              else f"not running ({hostos.logs_hint(unit, _label(), _log_dir())})", "sudo noctyx setup units")
    for t in ("tick", "nightly", "hygiene", "retention"):
        k.add(f"{t} timer", hostos.service_installed(_service(f"{t}.timer" if not hostos.IS_MAC else f"{t}.service"), _label()),
              "scheduled", "sudo noctyx setup units")
    for name, url in (("console https", f"https://127.0.0.1:{port}/"),
                      ("directory /health", f"https://{s['address']}:{s.get('directory_port', 20999)}/health")):
        try:
            code, _ = _https(url, ca)
            k.add(name, code < 500, f"{url} → HTTP {code}", "sudo noctyx setup units")
        except Exception as e:
            k.add(name, False, f"{url}: {getattr(e, 'reason', e)}", "sudo noctyx setup units")
    hp = cm.hf_cfg(c)["port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{hp}/", timeout=5) as r_:
            code = r_.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = str(getattr(e, "reason", e))
    k.add("mirror http", isinstance(code, int) and code < 500, f"http://127.0.0.1:{hp}/ → {code}", "sudo noctyx setup units")
    if s.get("console_auth", True) is not False:
        from . import accounts
        n_admin = sum(1 for u in accounts.users().values() if u.get("role") == "admin")
        k.add("console admin", n_admin > 0, f"{n_admin} admin account(s)",
              "sudo noctyx user add --name <you> --role admin")

    print("metrics")
    ready = _prom_ready()
    k.add("prometheus", ready, PROM if ready else f"{PROM} not answering", "sudo noctyx setup metrics", warn=True)
    for job, host, health, err in _targets() if ready else []:
        k.add(f"scrape {job}", health == "up", f"{host}: {health}" + (f" ({err})" if err else ""),
              "sudo noctyx setup metrics" if job in ("node", P["prom_job_console"]) else
              f"sudo noctyx tool install-node-metrics --host {host}", warn=job == "llm")

    print("keys")
    escrow, stale = _escrow_state()
    k.add("escrow", bool(escrow) and not stale, escrow[-1].name if escrow else "no bundle: the keys exist only here",
          "sudo noctyx setup escrow   (then copy it off this machine)", warn=True)

    hs = [h for h in cm.hosts(c) if h.get("address")]
    if hs:
        print("hosts")
    for h in hs:
        if h.get("os") == "macos" and h.get("local") and hostos.IS_MAC:
            ok, detail = _time_machine()
            k.add(h["name"], ok, f"monitored; Time Machine: {detail}",
                  "System Settings › General › Time Machine (Noctyx never backs a Mac up)", warn=True)
            continue
        if h not in cm.backup_hosts(c):
            k.add(h["name"], True, "monitored only")
            continue
        try:
            r = cm.ssh(c, h, f"test -x {P['agent_bin']} && echo agent", timeout=15)
            ok = "agent" in (r.stdout or "")
            k.add(h["name"], ok, ("agent installed" if ok else "reachable, no agent") if r.returncode in (0, 1)
                  else f"unreachable: {(r.stderr or '').strip()[-120:]}", f"sudo noctyx install-agent --host {h['name']}")
        except Exception as e:
            k.add(h["name"], False, f"unreachable: {e}", f"sudo noctyx install-agent --host {h['name']}")

    if run_canary and up and not c.get("repositories"):
        print("round trip\n  · no repository yet: add the Linux hosts to back up (noctyx init), then verify again")
    if run_canary and up and c.get("repositories"):
        print("round trip")
        repo = next((h["repo"] for h in head if h.get("repo")), c["repositories"][0]["id"])
        ok, detail = canary(c, repo)
        k.add("canary backup+restore", ok, detail)

    bad = k.failed
    warn = [r for r in k.rows if r[0] == "!"]
    print(f"\n{len(k.rows) - len(bad) - len(warn)}/{len(k.rows)} checks pass"
          + (f", {len(warn)} warning(s)" if warn else "") + (f", {len(bad)} fail" if bad else ": the site works"))
    if bad:
        llm.setup_help("setup verify", "\n".join(f"{n}: {d}" for _, n, d, _ in bad))
    return not bad


# ── the command ─────────────────────────────────────────────────────────────
RUN = {"tools": lambda c, a: setup_tools(c, a.force), "owner": lambda c, a: setup_owner(c),
       "mount": lambda c, a: setup_mount(c), "tls": lambda c, a: setup_tls(c, a.force),
       "repos": lambda c, a: setup_repos(c), "endpoint": lambda c, a: setup_endpoint(c),
       "rest-server": lambda c, a: setup_endpoint(c), "units": lambda c, a: setup_units(c),
       "privacy": lambda c, a: setup_privacy(c), "escrow": lambda c, a: setup_escrow(c),
       "metrics": lambda c, a: setup_metrics(c, a.force)}


def run_step(name, c, a):
    print(f"── {name}", flush=True)
    try:
        RUN[name](c, a)
        return True
    except (StepFailed, subprocess.CalledProcessError, RuntimeError, OSError, urllib.error.URLError) as e:
        msg = str(e) if not isinstance(e, subprocess.CalledProcessError) else \
            f"{e.cmd[0]} rc={e.returncode}: {(e.stderr or e.stdout or '')!s}"[-600:]
        print(f"  ✗ {name}: {msg}", flush=True)
        llm.setup_help(f"setup {name}", msg)
        return False


def cmd_setup(a):
    c = cm.cfg()
    if a.step == "status":
        return status(c)
    if os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    if a.step == "verify":
        sys.exit(0 if verify(c, not a.no_canary) else 1)
    if a.step == "all":
        for name, ok, _ in steps_state(c):
            if ok:           # done already: re-running a step is safe, but `units` would restart every service
                print(f"── {name}\n  ✓ done (`sudo noctyx setup {name}` runs it again)")
                continue
            if name == "escrow" and not sys.stdin.isatty():
                print("── escrow\n  skipped: it asks for a passphrase. Run `sudo noctyx setup escrow` in a terminal.")
                continue
            if not run_step(name, cm.cfg(), a):
                sys.exit(f"\nstopped at {name}: fix it, then `sudo noctyx setup all` again (it keeps what is done)")
        print("\nall steps done. Prove it:  sudo noctyx setup verify")
        return
    if not run_step(a.step, c, a):
        sys.exit(1)
