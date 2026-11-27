#!/usr/bin/env python3
"""Simulated AI cluster harness for CI and local integration testing.

Provides lightweight, pure-stdlib live servers for:
  - OpenAI-compatible GPU slot server (/health, /v1/models, /v1/chat/completions)
    with programmable model switching and answer drift for `slot-check` / deploy tests
  - Fake Prometheus (/api/v1/query, /api/v1/query_range, /api/v1/targets, /-/ready)
  - Fake Alertmanager (/api/v2/alerts, /api/v2/silences, /-/ready) capturing posted alerts
  - Optional Docker container node helper (`DockerNode`) for container-backed tests on Linux hosts
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
import time


class _SlotServer:
    def __init__(self, model: str = "sim-model-7b", answer_prefix: str = ""):
        self.model = model
        self.answer_prefix = answer_prefix
        self.up = True
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if not outer.up:
                    return self._send(503, {"status": "down"})
                if self.path == "/health":
                    return self._send(200, {"status": "ok", "model": outer.model})
                if self.path == "/v1/models":
                    return self._send(200, {"object": "list", "data": [{"id": outer.model, "object": "model"}]})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                if not outer.up:
                    return self._send(503, {"error": "down"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) if n > 0 else b"{}")
                outer.requests.append(body)
                msgs = body.get("messages") or []
                prompt = str((msgs[-1] or {}).get("content") or "") if msgs else ""
                reply = outer.answer_prefix + _canned_answer(prompt)
                return self._send(200, {
                    "id": "chatcmpl-sim",
                    "model": body.get("model") or outer.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                })

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._th.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _canned_answer(prompt: str) -> str:
    low = prompt.lower()
    if "17" in low and "23" in low:
        return "391"
    if "prime" in low:
        return "2, 3, 5, 7, 11, 13, 17, 19"
    if "quick brown fox" in low:
        return "The quick brown fox jumps over the lazy dog."
    if "sky is blue" in low:
        return "Sunlight undergoes Rayleigh scattering, so shorter blue wavelengths scatter more across the sky."
    return f"ok:{prompt[:40]}"


class _FakeAlertmanager:
    def __init__(self):
        self.alerts: list[dict] = []
        self.silences: dict[str, dict] = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/-/ready":
                    return self._send(200, {"status": "ready"})
                if self.path.startswith("/api/v2/alerts"):
                    return self._send(200, outer.alerts)
                if self.path.startswith("/api/v2/silences"):
                    return self._send(200, list(outer.silences.values()))
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) if n > 0 else b"{}")
                if self.path.startswith("/api/v2/alerts"):
                    if isinstance(body, list):
                        outer.alerts.extend(body)
                    return self._send(200, {"status": "ok"})
                if self.path.startswith("/api/v2/silences"):
                    sid = f"sil-{len(outer.silences) + 1}"
                    outer.silences[sid] = {"id": sid, **body}
                    return self._send(200, {"silenceID": sid})
                return self._send(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._th.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _FakePrometheus:
    def __init__(self, hosts: list[str]):
        self.hosts = hosts
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/-/ready":
                    return self._send(200, {"status": "ready"})
                if self.path.startswith("/api/v1/targets"):
                    return self._send(200, {"status": "success", "data": {"activeTargets": [
                        {"labels": {"job": "node", "host": h}, "health": "up", "lastError": ""}
                        for h in outer.hosts
                    ]}})
                if self.path.startswith("/api/v1/query"):
                    now = time.time()
                    return self._send(200, {"status": "success", "data": {"resultType": "vector", "result": [
                        {"metric": {"host": h}, "value": [now, "42.5"]} for h in outer.hosts
                    ]}})
                return self._send(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._th.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DockerNode:
    """Optional ephemeral container node wrapper (used when Docker is available and requested)."""

    def __init__(self, name: str, image: str = "ubuntu:24.04"):
        self.name = name
        self.image = image
        self.cid: str | None = None

    @staticmethod
    def available() -> bool:
        if not shutil.which("docker"):
            return False
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0

    def __enter__(self):
        r = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, self.image, "sleep", "300"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        self.cid = r.stdout.strip()
        return self

    def exec(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        assert self.cid
        return subprocess.run(["docker", "exec", self.cid, "bash", "-lc", cmd],
                              capture_output=True, text=True, timeout=timeout)

    def __exit__(self, *exc):
        if self.cid:
            subprocess.run(["docker", "rm", "-f", self.cid], capture_output=True, timeout=15)
            self.cid = None


class SimulatedCluster:
    """Context manager launching stub GPU slot(s), Prometheus, and Alertmanager on loopback."""

    def __init__(self, hosts: list[str] | None = None, model: str = "sim-model-7b"):
        self.hosts = hosts or ["gpu-01", "gpu-02"]
        self.slot = _SlotServer(model=model)
        self.alertmanager = _FakeAlertmanager()
        self.prometheus = _FakePrometheus(self.hosts)

    def __enter__(self):
        self.slot.start()
        self.alertmanager.start()
        self.prometheus.start()
        return self

    def __exit__(self, *exc):
        self.slot.stop()
        self.alertmanager.stop()
        self.prometheus.stop()
