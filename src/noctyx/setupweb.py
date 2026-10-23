"""noctyx init --web: the first-time setup in a browser, with the site's LLM beside every step.

It serves one page on the head (HTTPS, port 20992 by default) to whoever opens the one-time link printed in
the terminal; the link sets a cookie and nothing else gets in. The page sets the assistant up first (a form:
find, test, save), then the operator talks to it: it probes machines and proposes each change (setupagent.py),
the operator edits and approves. The first admin's password and an SMB share's password are typed into forms
here and never reach the LLM. Finishing revokes the link and stops the server; the console takes over.

The certificate is the site's own when `noctyx setup tls` has made one, else a self-signed one made for this
run (the browser warns once).
"""
import hmac
import http.server
import json
import os
import pathlib
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.parse

from . import firstrun, hostos, llm, setupagent
from .paths import CONFIG, P

ASSETS = pathlib.Path(__file__).resolve().parent / "assets"
TOKEN = None
SESSION = None
SERVER = None
COOKIE = "noctyx_setup"


def _cert(tmp):
    """(cert, key): the site's when it has them, else a fresh self-signed pair naming this machine."""
    tls = (firstrun.load(CONFIG).get("server") or {}).get("tls") or {}
    if tls.get("cert") and tls.get("key") and os.path.exists(tls["cert"]) and os.path.exists(tls["key"]):
        return tls["cert"], tls["key"]
    crt, key = f"{tmp}/setup.crt", f"{tmp}/setup.key"
    addr = hostos.primary_address()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "2", "-subj", "/CN=noctyx-setup", "-addext", f"subjectAltName=IP:{addr},DNS:{hostos.short_hostname()}",
                    "-keyout", key, "-out", crt], check=True, capture_output=True)
    return crt, key


def _page(nonce):
    return (ASSETS / "setup.html").read_text().replace("{{NONCE}}", nonce)


class H(http.server.BaseHTTPRequestHandler):
    server_version = "noctyx-setup"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=()):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        if not TOKEN:
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/favicon.svg":
            return self._send(200, (ASSETS / "favicon.svg").read_bytes(), "image/svg+xml")
        if u.path == "/" and q.get("t") and TOKEN and hmac.compare_digest(q["t"][0], TOKEN):
            return self._send(303, b"", "text/plain", [("Location", "/"), (
                "Set-Cookie", f"{COOKIE}={TOKEN}; Path=/; Secure; HttpOnly; SameSite=Strict")])
        if not self._authed():
            return self._send(403, "<!doctype html><title>Noctyx setup</title><body style='font:16px sans-serif;"
                              "background:#0b1016;color:#cfe;padding:40px'>Open the link <code>noctyx init --web</code> "
                              "printed in its terminal.", "text/html; charset=utf-8")
        if u.path == "/":
            nonce = secrets.token_urlsafe(16)
            return self._send(200, _page(nonce), "text/html; charset=utf-8", [(
                "Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")])
        if u.path == "/api/view":
            return self._send(200, SESSION.view(int((q.get("since") or ["0"])[0] or 0)))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed() or self.headers.get("X-Noctyx-Setup") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "a JSON object"})
        try:
            return self._send(200, ROUTES[urllib.parse.urlsplit(self.path).path](body) or {"ok": True})
        except KeyError:
            return self._send(404, {"error": "not found"})
        except setupagent.Bad as e:
            return self._send(400, {"error": str(e)})


# ── what the page posts ──
def r_discover(b):
    addrs = [setupagent._address(x) for x in (b.get("addresses") or [])][:16]
    return {"found": llm.discover(["127.0.0.1", hostos.primary_address(), *addrs])}


def r_assistant(b):
    """Test an endpoint and, when it answers, save it as the site's assistant (a new site's config holds just
    this until the head is set up, as with `noctyx init`)."""
    url = str(b.get("url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")) or any(ch in url for ch in " \n\r\"'<>"):
        raise setupagent.Bad("a URL like http://host:port/v1")
    key = str(b.get("key") or "").strip() or None
    model = setupagent._text(b.get("model") or "", 128) or None
    t = llm.test(url, model, key, timeout=120)
    if not t["ok"]:
        return {"ok": False, "error": t.get("error"), "models": t.get("models")}
    with SESSION.lock:
        c = SESSION.config()
        firstrun.save_assistant(c, url, model or t["model"], key)
        setupagent.save(c)
        SESSION.c = c
        SESSION._st = (0, None)
    return {"ok": True, "model": t["model"], "ms": t["ms"], "text": t["text"]}


def r_chat(b):
    text = str(b.get("text") or "").strip()
    if not text:
        raise setupagent.Bad("say something")
    SESSION.say(text[:4000])


def r_decide(b):
    SESSION.decide(str(b.get("pid")), bool(b.get("approve")), b.get("args") if isinstance(b.get("args"), dict) else None)


def r_act(b):
    return {"out": SESSION.act(str(b.get("tool")), b.get("args") if isinstance(b.get("args"), dict) else {})}


def r_admin(b):
    """The first admin: made here, from a form; the password never reaches the LLM."""
    from . import accounts
    name, pw = str(b.get("name") or "").strip(), str(b.get("password") or "")
    if setupagent.admin_exists():
        raise setupagent.Bad("an admin exists already")
    errs = accounts.check_new(name, "admin", pw)
    if errs:
        raise setupagent.Bad("; ".join(errs))
    pathlib.Path(P["etc"]).mkdir(parents=True, exist_ok=True)
    accounts.add(name, "admin", pw)
    with SESSION.lock:
        SESSION.notes.append(f"made the first admin account, {name}")
        SESSION._st = (0, None)


def r_smb(b):
    """An SMB share's password, written where `noctyx setup mount` looks for it (Linux heads; a Mac mounts as
    the repository owner and asks in its terminal)."""
    pw = str(b.get("password") or "")
    if not pw or "\n" in pw:
        raise setupagent.Bad("the share's password, on one line")
    if hostos.IS_MAC:
        raise setupagent.Bad("on a Mac the share's password is asked by `sudo noctyx setup mount` in a terminal")
    c = SESSION.config()
    done = []
    for name, sp in firstrun.spindles(c).items():
        sh = sp.get("share") or {}
        if sh.get("proto") != "smb":
            continue
        cred = pathlib.Path(P["etc"]) / f"smb-{name}.cred"
        cred.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, f"username={sh.get('user', '')}\npassword={pw}\n".encode())
        os.close(fd)
        done.append(name)
    if not done:
        raise setupagent.Bad("the storage is not an SMB share")
    with SESSION.lock:
        SESSION.notes.append("typed the SMB share's password into the page (saved for `setup mount`)")
    return {"saved": done}


def r_finish(b):
    global TOKEN
    c = SESSION.config()
    s = c.get("server") or {}
    if not s.get("hostname") or not firstrun.spindles(c):
        raise setupagent.Bad("the head and the storage are not set up yet")
    if s.get("console_auth", True) is not False and not setupagent.admin_exists():
        raise setupagent.Bad("sign-in is on and there is no admin yet: make one first")
    TOKEN = None                                  # the link is spent
    threading.Thread(target=SERVER.shutdown, daemon=True).start()
    return {"console": f"https://{s['address']}:{s.get('ui_port', 20996)}"}


ROUTES = {"/api/discover": r_discover, "/api/assistant": r_assistant, "/api/chat": r_chat, "/api/decide": r_decide,
          "/api/act": r_act, "/api/admin": r_admin, "/api/smb": r_smb, "/api/finish": r_finish}


def main(port=20992):
    global TOKEN, SESSION, SERVER
    if port in (21000, 21001) or not hostos.port_free(port):
        sys.exit(f"port {port} is in use (or reserved): noctyx init --web --port N")
    if not shutil.which("openssl"):
        sys.exit("openssl is needed for the setup page's certificate")
    tmp = tempfile.mkdtemp(prefix="noctyx-setup-")
    try:
        cert, key = _cert(tmp)
        TOKEN = secrets.token_urlsafe(24)
        SESSION = setupagent.Session()
        SERVER = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        SERVER.socket = ctx.wrap_socket(SERVER.socket, server_side=True)
        from .banner import render
        print(render())
        print(f"\nNoctyx setup in your browser. Open this link (it works once per browser, until setup is finished):\n\n"
              f"  https://{hostos.primary_address()}:{port}/?t={TOKEN}\n\n"
              "The certificate is " + ("the site's own." if not cert.startswith(tmp) else
                                       "self-signed for this run: your browser warns once.")
              + "  Ctrl-C stops the setup server.", flush=True)
        try:
            SERVER.serve_forever()
        except KeyboardInterrupt:
            pass
        c = firstrun.load(CONFIG)
        s = c.get("server") or {}
        print("\nsetup server stopped." + (f" The console: https://{s['address']}:{s.get('ui_port', 20996)}"
                                           if s.get("address") else ""))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
