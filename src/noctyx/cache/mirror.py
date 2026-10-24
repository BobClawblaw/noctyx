#!/usr/bin/env python3
"""Noctyx HF mirror — a Hugging Face endpoint for the LAN, backed by the
models already harvested from the cluster's caches (`noctyx hf-harvest`).

  HF_ENDPOINT=http://<head>:20995  huggingface-cli download …  /  vllm serve …

Local first, upstream for everything else:
  /{repo}/resolve/{rev}/{path}   served from the store when that exact file at that
                                 commit was harvested (branch names are resolved
                                 against huggingface.co first, so "main" still means
                                 the current main; offline → the harvested ref)
  /api/{models,datasets}/{repo}[/revision/{rev}|/tree/{rev}[/path]]
                                 proxied; if huggingface.co is unreachable or says
                                 401/403/404 (offline, gated without a token, deleted)
                                 and the store has the repo, answered from the store
  anything else                  streamed through to huggingface.co unchanged
                                 (redirects to its CDN / xet storage are passed on)

Read-only on the store; stdlib only. Plain HTTP on the trusted LAN.
"""
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE = os.environ.get("NOCTYX_HF_STORE", "/srv/noctyx/hf-mirror/hub")
PORT = int(os.environ.get("NOCTYX_HF_PORT", "20995"))
UP_HOST = os.environ.get("NOCTYX_HF_UPSTREAM", "huggingface.co")
CHUNK = 4 << 20
HEX40 = re.compile(r"^[0-9a-f]{40}$")
KINDS = {"models": "models", "datasets": "datasets", "spaces": "spaces"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
STATS = {"hit": 0, "miss": 0, "fallback": 0, "hit_bytes": 0, "miss_bytes": 0, "errors": 0}
SLOCK = threading.Lock()
_UPC, _UPL = {}, threading.Lock()     # (kind, repo, rev, authed) -> (ts, sha|None)


def count(k, n=1):
    with SLOCK:
        STATS[k] += n


_IDX = {"ts": 0, "map": {}}


def repo_dir(kind, repo):
    """HF cache layout: models--org--name, datasets--org--name, spaces--org--name.
    Repo ids are case-insensitive upstream (qdrant/x-q redirects to Qdrant/x-Q), so a
    miss on the exact name falls back to a lowercase index of the store (60 s old at most)."""
    name = f"{kind}--" + repo.replace("/", "--")
    exact = os.path.join(STORE, name)
    if os.path.isdir(exact):
        return exact
    if time.time() - _IDX["ts"] > 60:
        try:
            _IDX["map"] = {n.lower(): n for n in os.listdir(STORE)}
        except OSError:
            pass
        _IDX["ts"] = time.time()
    return os.path.join(STORE, _IDX["map"].get(name.lower(), name))


REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_part(p):
    """A URL path component after unquoting: never a separator (%2F, %5C), a parent or current directory, a NUL."""
    return p not in ("", ".", "..") and not any(c in p for c in "/\\\0")


def local_ref(rd, rev):
    """The commit a revision names in the store, or None. Only a plain ref name is looked up (a PR ref such as
    refs/pr/1 goes upstream), and only a 40-hex sha is ever returned: it becomes a path and a header."""
    if HEX40.match(rev):
        return rev if os.path.isdir(os.path.join(rd, "snapshots", rev)) else None
    if not REV.match(rev):
        return None
    try:
        sha = open(os.path.join(rd, "refs", rev)).read().strip()
    except OSError:
        return None
    return sha if HEX40.match(sha) else None


def upstream(method, path, headers=None, body=None, timeout=30):
    c = http.client.HTTPSConnection(UP_HOST, timeout=timeout)
    h = {k: v for k, v in (headers or {}).items() if k.lower() not in HOP}
    h["Host"] = UP_HOST
    c.request(method, path, body=body, headers=h)
    return c, c.getresponse()


def upstream_sha(kind, repo, rev, auth):
    """Current commit of a branch upstream (cached 5 min; failures 1 min). None = unknown."""
    if HEX40.match(rev):
        return rev
    key = (kind, repo, rev, bool(auth))
    with _UPL:
        hit = _UPC.get(key)
    if hit and time.time() - hit[0] < (300 if hit[1] else 60):
        return hit[1]
    sha = None
    try:
        c, r = upstream("GET", f"/api/{kind}/{repo}/revision/{urllib.parse.quote(rev, safe='')}",
                        {"Authorization": auth} if auth else {}, timeout=4)
        if r.status == 200:
            sha = json.loads(r.read()).get("sha")
        c.close()
    except Exception:
        pass
    with _UPL:
        _UPC[key] = (time.time(), sha)
    return sha


def snapshot_files(rd, commit):
    base = os.path.join(rd, "snapshots", commit)
    out = []
    for dp, _, fns in os.walk(base):
        for f in fns:
            p = os.path.join(dp, f)
            try:
                blob = os.path.basename(os.readlink(p)) if os.path.islink(p) else None
                size = os.stat(p).st_size
            except OSError:
                continue                      # dangling: blob not harvested
            out.append({"path": os.path.relpath(p, base), "blob": blob, "size": size})
    return sorted(out, key=lambda x: x["path"])


def parse_repo(parts):
    """parts after the kind prefix -> (repo_id, rest). Repo ids are 'org/name' or legacy 'name'."""
    kw = {"resolve", "revision", "tree", "paths-info", "refs", "commits", "xet-read-token", "blob"}
    if len(parts) >= 2 and parts[1] not in kw:
        return "/".join(parts[:2]), parts[2:]
    return parts[0], parts[1:]


class H(BaseHTTPRequestHandler):
    server_version = "cm-hf-mirror/1"
    protocol_version = "HTTP/1.0"     # one response per connection: streaming without chunking

    def log_message(self, fmt, *a):
        if os.environ.get("NOCTYX_HF_DEBUG"):
            print(f"{self.address_string()} {fmt % a}", file=sys.stderr, flush=True)

    def do_HEAD(self):
        self.route(head=True)

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.proxy()

    # ── routing ──
    def route(self, head=False):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
        if not all(safe_part(p) for p in parts):
            return self.proxy()       # never a local path from it; upstream judges it (a PR ref arrives as refs%2Fpr%2F1)
        if u.path == "/metrics":
            return self.metrics()
        if u.path in ("/", "/cm-status"):
            return self.status()
        try:
            if parts and parts[0] == "api" and len(parts) >= 3 and parts[1] in KINDS:
                return self.api(parts[1], *parse_repo(parts[2:]), head=head)
            kind = "models"
            if parts and parts[0] in ("datasets", "spaces"):
                kind, parts = parts[0], parts[1:]
            if "resolve" in parts:
                i = parts.index("resolve")
                if 0 < i <= 2 and len(parts) > i + 2:
                    return self.resolve(kind, "/".join(parts[:i]), parts[i + 1], "/".join(parts[i + 2:]), head)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            count("errors")
            print(f"hf-mirror: {self.path}: {e!r}", file=sys.stderr, flush=True)
        self.proxy()

    def resolve(self, kind, repo, rev, path, head):
        rd = repo_dir(kind, repo)
        if not os.path.isdir(rd) or ".." in path.split("/"):
            return self.proxy()
        auth = self.headers.get("Authorization")
        commit = upstream_sha(kind, repo, rev, auth) or local_ref(rd, rev)
        if commit and not HEX40.match(commit):
            commit = None                 # it becomes a path below and a response header
        f = os.path.join(rd, "snapshots", commit, path) if commit else None
        if f and os.path.isfile(f):
            blob = os.path.basename(os.readlink(f)) if os.path.islink(f) else None
        else:
            # Not harvested at THIS commit — but a new commit usually only touched a README:
            # ask upstream for the file's etag (one HEAD, no body) and serve our copy of that
            # exact blob if we have it. Content-addressed, so identical bytes by definition.
            blob = self.upstream_etag(kind, repo, commit, path, auth) if commit else None
            f = os.path.join(rd, "blobs", blob) if blob else None
            if not f or not os.path.isfile(f):
                return self.proxy()
        size = os.stat(f).st_size
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and size:
            if m.group(1):
                start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Repo-Commit", commit)
        if blob:
            self.send_header("ETag", f'"{blob}"')
            self.send_header("X-Linked-Etag", f'"{blob}"')
        self.send_header("X-Linked-Size", str(size))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("X-CM-Mirror", "hit")
        self.end_headers()
        count("hit")
        if head:
            return
        n = 0
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                b = fh.read(min(CHUNK, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b); n += len(b)
        count("hit_bytes", n)

    def upstream_etag(self, kind, repo, commit, path, auth):
        pre = "" if kind == "models" else f"/{kind}"
        try:
            c, r = upstream("HEAD", f"{pre}/{repo}/resolve/{commit}/{urllib.parse.quote(path)}",
                            {"Authorization": auth} if auth else {}, timeout=6)
            et = r.getheader("X-Linked-Etag") or r.getheader("ETag") or ""
            c.close()
        except Exception:
            return None
        et = et.replace("W/", "").strip('"')
        return et if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", et) else None

    def api(self, kind, repo, rest, head):
        """Proxy; on failure (offline / 401 / 403 / 404) answer from the store if it has the repo."""
        rd = repo_dir(kind, repo)
        if not os.path.isdir(rd) or (rest and rest[0] not in ("revision", "tree")):
            return self.proxy()
        try:
            c, r = upstream(self.command, self.path, dict(self.headers), timeout=8)
            if r.status < 500 and r.status not in (401, 403, 404):
                return self.relay(c, r)
            c.close()
        except Exception:
            pass
        rev = rest[1] if len(rest) > 1 else "main"
        commit = local_ref(rd, rev)
        if not commit or not os.path.isdir(os.path.join(rd, "snapshots", commit)):
            return self.json(404, {"error": f"{repo}@{rev}: not reachable upstream and not in the mirror"})
        files = snapshot_files(rd, commit)
        if rest and rest[0] == "tree":
            sub = "/".join(rest[2:])
            items, dirs = [], set()
            for x in files:
                if sub and not x["path"].startswith(sub.rstrip("/") + "/"):
                    continue
                for i, _ in enumerate(x["path"].split("/")[:-1]):
                    dirs.add("/".join(x["path"].split("/")[:i + 1]))
                e = {"type": "file", "oid": x["blob"], "size": x["size"], "path": x["path"]}
                if x["blob"] and len(x["blob"]) == 64:
                    e["lfs"] = {"oid": x["blob"], "size": x["size"], "pointerSize": 134}
                items.append(e)
            items += [{"type": "directory", "oid": "", "size": 0, "path": d} for d in sorted(dirs)
                      if not sub or d.startswith(sub.rstrip("/") + "/")]
            count("fallback")
            return self.json(200, items, commit)
        mt = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(os.path.getmtime(os.path.join(rd, "snapshots", commit))))
        sib = []
        for x in files:
            s = {"rfilename": x["path"], "size": x["size"], "blobId": x["blob"]}
            if x["blob"] and len(x["blob"]) == 64:
                s["lfs"] = {"sha256": x["blob"], "size": x["size"], "pointerSize": 134}
            sib.append(s)
        count("fallback")
        return self.json(200, {"_id": repo, "id": repo, "modelId": repo, "sha": commit, "lastModified": mt,
                               "private": False, "disabled": False, "gated": False, "tags": [], "downloads": 0,
                               "likes": 0, "cardData": {}, "siblings": sib, "noctyx_mirror": "offline-fallback"}, commit)

    # ── plumbing ──
    def proxy(self):
        body = None
        if self.command == "POST":
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            c, r = upstream(self.command, self.path, dict(self.headers), body=body, timeout=60)
        except Exception as e:
            count("errors")
            return self.json(502, {"error": f"huggingface.co unreachable: {e}"})
        self.relay(c, r)

    def relay(self, c, r):
        self.send_response(r.status, r.reason)
        for k, v in r.getheaders():
            if k.lower() in HOP:
                continue
            if k.lower() == "location" and v.startswith(f"https://{UP_HOST}/"):
                v = v[len(f"https://{UP_HOST}"):]      # keep the client on the mirror
            self.send_header(k, v)
        if r.getheader("Content-Length"):
            self.send_header("Content-Length", r.getheader("Content-Length"))
        self.send_header("X-CM-Mirror", "miss")
        self.end_headers()
        count("miss")
        n = 0
        try:
            if self.command != "HEAD":
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    self.wfile.write(b)
                    n += len(b)
        finally:
            c.close()
            count("miss_bytes", n)

    def json(self, code, obj, commit=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        if commit:
            self.send_header("X-Repo-Commit", commit)
        self.send_header("X-CM-Mirror", "fallback" if code == 200 else "none")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def metrics(self):
        with SLOCK:
            s = dict(STATS)
        pfx = os.environ.get("NOCTYX_PATH_METRIC_PREFIX") or "noctyx"
        lines = ["# TYPE noctyx_hfmirror_requests_total counter"]
        lines += [f'noctyx_hfmirror_requests_total{{result="{k}"}} {s[k]}' for k in ("hit", "miss", "fallback", "errors")]
        lines += ["# TYPE noctyx_hfmirror_bytes_total counter",
                  f'noctyx_hfmirror_bytes_total{{result="hit"}} {s["hit_bytes"]}',
                  f'noctyx_hfmirror_bytes_total{{result="miss"}} {s["miss_bytes"]}']
        b = ("\n".join(lines) + "\n").replace("noctyx_", pfx + "_").encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def status(self):
        try:
            st = json.load(open(os.path.join(os.path.dirname(STORE), "status.json")))
        except Exception:
            st = {}
        with SLOCK:
            s = dict(STATS)
        self.json(200, {"service": "noctyx hf-mirror", "store": STORE, "stats": s,
                        "store_totals": st.get("totals"), "last_harvest": st.get("ts")})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print(f"hf-mirror: serving {STORE} on :{PORT}, upstream {UP_HOST}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
