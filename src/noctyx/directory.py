#!/usr/bin/env python3
"""Noctyx directory + discovery service (head node, stdlib only).

Three jobs:

  1. UDP discovery. The restore volume boots on a machine with *no* network
     configuration and no idea who we are. Rather than requiring DHCP option
     tweaks on a router we do not manage, the volume broadcasts
     `NX_DISCOVER?` (or an older stick's `CM_DISCOVER?`) on port 8977 and we answer. On a flat L2 LAN (every node on-link, no gateway hop) a broadcast
     probe is the cheapest thing that can possibly work. Static fallback exists
     in the restore image for the day the LAN stops being flat.

  2. Read-only JSON: host inventory, snapshot listings, and the CA certificate,
     so a rescue environment can bootstrap trust without a config file.

  3. /api/v1/probe — a byte sink/source used by the orchestrator to measure the
     REAL client→server path with the REAL client before committing to a job
     that might take 40 hours. Measuring with curl on the real path beats any
     amount of theorising about protocol choice.

Also serves /api/v1/restore-script so a fix to the restore logic is picked up by
an existing USB volume instead of requiring every stick to be rebuilt.

Trust note: discovery and these endpoints are unauthenticated on purpose — they
expose hostnames, sizes and a public CA cert. Nothing here decrypts anything.
The repo keys are never served by this service under any circumstance.
"""
from __future__ import annotations
import json, os, socket, ssl, threading, datetime, pathlib, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from noctyx.paths import P, CONFIG   # noqa: E402

NX_HOME = pathlib.Path(__file__).resolve().parent
CFG = json.load(open(CONFIG))
S = CFG["server"]
STATE = pathlib.Path(P["state"])
CERT, KEY, CA = S["tls"]["cert"], S["tls"]["key"], S["tls"]["ca"]
PORT = int(S["directory_port"])
UDP_PORT = int(S["discovery_udp_port"])
PROBE_MAX = 200_000_000


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load(p, default=None):
    try:
        return json.loads((STATE / p).read_text())
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("NOCTYX_DIR_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, body: bytes, ctype="application/json", code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # a probe that gives up mid-transfer is normal and not an error

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, indent=2).encode(), code=code)

    def do_GET(self):
        p = self.path.split("?")[0]
        q = dict(kv.split("=", 1) for kv in self.path.split("?")[1].split("&") if "=" in kv) \
            if "?" in self.path else {}

        if p == "/health":
            return self._json({"ok": True, "ts": now(), "server": S["hostname"],
                               "network": CFG["network"]})

        if p == "/api/v1/hosts":
            d = load("hosts.json")
            return self._json(d or {"error": "run: noctyx directory --regen"})

        if p == "/api/v1/ca":
            try:
                return self._send(pathlib.Path(CA).read_bytes(),
                                  "application/x-pem-file")
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/v1/restore-script":
            f = NX_HOME / "restore" / "noctyx-restore.sh"   # the bare-metal restore image (being ported)
            try:
                return self._send(f.read_bytes(), "text/x-shellscript")
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/v1/probe":
            # Deterministic filler, not urandom: we are measuring the path, not
            # the CSPRNG. Compressible data is also a fairer proxy for backups,
            # which are usually not random.
            n = min(int(q.get("bytes", 20_000_000)), PROBE_MAX)
            chunk = (b"noctyx-probe-" + bytes(range(256))) * 32
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(n))
            self.end_headers()
            try:
                left = n
                while left > 0:
                    b = chunk[:left]
                    self.wfile.write(b)
                    left -= len(b)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if p.startswith("/api/v1/snapshots/"):
            name = p.rsplit("/", 1)[-1]
            d = load(f"snapshots-{name}.json")
            return self._json(d or {"host": name, "snapshots": [],
                                    "note": "listing not generated; run noctyx snapshots"})

        return self._json({"error": "not found", "endpoints": [
            "/health", "/api/v1/hosts", "/api/v1/ca", "/api/v1/probe?bytes=N",
            "/api/v1/restore-script", "/api/v1/snapshots/<host>"]}, 404)


def udp_discovery():
    """Answer a broadcast 'NX_DISCOVER?' (or 'CM_DISCOVER?') with where to find us.

    Reply is unicast back to the requester. bind(0.0.0.0) receives both directed
    and limited broadcast on Linux. Keep the payload tiny: a rescue initrd that
    has to parse 4 KB of JSON at 3 a.m. is a rescue initrd that fails."""
    so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    so.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    so.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    so.bind(("0.0.0.0", UDP_PORT))
    reply = json.dumps({
        "service": "noctyx",
        "server": S["hostname"],
        "address": S["address"],
        "rest_url": f"https://{S['address']}:{S['rest_port']}",
        "directory_url": f"https://{S['address']}:{PORT}",
        "hosts": [h["name"] for h in (load("hosts.json", {}) or {}).get("hosts", [])],
        "ts": now(),
    }).encode()
    while True:
        try:
            data, peer = so.recvfrom(2048)
        except OSError:
            continue
        if data.strip().startswith((b"NX_DISCOVER", b"CM_DISCOVER")):
            try:
                so.sendto(reply, peer)
            except OSError:
                pass


def main():
    for p, why in ((CERT, "server cert"), (KEY, "server key"), (CA, "CA")):
        if not os.path.exists(p):
            sys.exit(f"missing {why} {p} — run server/tls-setup.sh first")
    threading.Thread(target=udp_discovery, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:
        pass
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"noctyx-directory: https://0.0.0.0:{PORT}  udp-discovery:{UDP_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
