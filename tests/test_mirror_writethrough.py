"""Tests for HF mirror write-through caching and cache/lister.py."""
import hashlib
import http.client
import http.server
import json
import os
import threading

import pytest

from noctyx.cache import lister, mirror


def test_mirror_write_through_caches_and_serves_offline(tmp_path, monkeypatch):
    payload = b"hello model weights payload!" * 100
    sha256 = hashlib.sha256(payload).hexdigest()
    commit = "a" * 40
    bad_etag = "b" * 64

    class FakeUpstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/api/models/org/demo/revision/"):
                b = json.dumps({"sha": commit}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if self.path == "/org/demo/resolve/main/model.bin":
                # Redirect like Hugging Face Hub -> CDN
                self.send_response(302)
                self.send_header("X-Repo-Commit", commit)
                self.send_header("X-Linked-Etag", f'"{sha256}"')
                self.send_header("Location", f"http://127.0.0.1:{up_port}/cdn/blob")
                self.end_headers()
                return
            if self.path == "/cdn/blob":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/org/demo/resolve/main/corrupt.bin":
                self.send_response(200)
                self.send_header("X-Repo-Commit", commit)
                self.send_header("X-Linked-Etag", f'"{bad_etag}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    up_port = up.server_address[1]
    threading.Thread(target=up.serve_forever, daemon=True).start()

    store = tmp_path / "hub"
    store.mkdir()
    monkeypatch.setattr(mirror, "STORE", str(store))
    monkeypatch.setattr(mirror, "WRITE_THROUGH", True)
    monkeypatch.setattr(mirror, "_IDX", {"ts": 0, "map": {}})
    monkeypatch.setattr(mirror, "_UPC", {})

    def fake_upstream(method, path, headers=None, body=None, timeout=30):
        c = http.client.HTTPConnection("127.0.0.1", up_port, timeout=timeout)
        c.request(method, path, body=body, headers=headers or {})
        return c, c.getresponse()

    monkeypatch.setattr(mirror, "upstream", fake_upstream)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mirror.H)
    m_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 1. First request writes through and populates store
        c = http.client.HTTPConnection("127.0.0.1", m_port, timeout=5)
        c.request("GET", "/org/demo/resolve/main/model.bin")
        r1 = c.getresponse()
        b1 = r1.read()
        assert r1.status == 200 and b1 == payload
        assert r1.getheader("X-CM-Mirror") == "write-through"
        c.close()
        import time as _t
        blob_path = store / "models--org--demo" / "blobs" / sha256
        for _ in range(50):
            if blob_path.is_file():
                break
            _t.sleep(0.02)
        assert blob_path.is_file()
        assert (store / "models--org--demo" / "snapshots" / commit / "model.bin").is_symlink()
        assert (store / "models--org--demo" / "refs" / "main").read_text() == commit

        # 2. Corrupted sha256 etag is NOT committed to the store
        c = http.client.HTTPConnection("127.0.0.1", m_port, timeout=5)
        c.request("GET", "/org/demo/resolve/main/corrupt.bin")
        rc = c.getresponse()
        rc.read()
        assert rc.status == 200
        assert not (store / "models--org--demo" / "blobs" / bad_etag).exists()
        assert not (store / "models--org--demo" / "snapshots" / commit / "corrupt.bin").exists()
        c.close()

        # 3. Shut down upstream; second request for model.bin is a local HIT!
        up.shutdown()
        up.server_close()
        mirror._UPC.clear()
        c = http.client.HTTPConnection("127.0.0.1", m_port, timeout=5)
        c.request("GET", "/org/demo/resolve/main/model.bin")
        r2 = c.getresponse()
        b2 = r2.read()
        assert r2.status == 200 and b2 == payload
        assert r2.getheader("X-CM-Mirror") == "hit"
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_lister_discovers_hub_cache_and_local_dir(tmp_path):
    # Standard HF hub layout
    hub = tmp_path / "cache" / "hub"
    repo = hub / "models--org--m1"
    (repo / "blobs").mkdir(parents=True)
    (repo / "refs").mkdir()
    commit = "1" * 40
    blob = "2" * 64
    (repo / "blobs" / blob).write_bytes(b"weights")
    (repo / "blobs" / "skip.incomplete").write_bytes(b"partial")
    (repo / "refs" / "main").write_text(commit)
    snap = repo / "snapshots" / commit
    snap.mkdir(parents=True)
    os.symlink(f"../../blobs/{blob}", snap / "weights.bin")

    # --local-dir layout with hints
    loc = tmp_path / "my-local-model"
    meta = loc / ".cache" / "huggingface" / "download"
    meta.mkdir(parents=True)
    (loc / "model.safetensors").write_bytes(b"abcd")
    mt = os.path.getmtime(loc / "model.safetensors")
    (meta / "model.safetensors.metadata").write_text(f"{commit}\n{blob}\n{mt + 1}\n")
    (loc / "README.md").write_text("See https://huggingface.co/Org/LocalModel for details.")
    (loc / "config.json").write_text(json.dumps({"_name_or_path": "Org/LocalModel"}))

    hubs, locals_ = lister.roots([str(tmp_path)])
    assert str(hub) in hubs and str(loc) in locals_

    d = lister.describe(str(hub), "models--org--m1")
    assert d["files"][commit] == {"weights.bin": blob} and d["blobs"] == {blob: 7}
    assert "skip.incomplete" not in d["blobs"]

    dl = lister.describe_local(str(loc))
    assert dl["local"] is True and dl["files"][commit] == {"model.safetensors": blob}
    assert "models/Org/LocalModel" in dl["hints"]
