"""The assistant's endpoints: its own first, then the model servers discovery found; the first that answers is
asked, a swapped model's stale name is asked again as served, and which one answered is kept for the console."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from noctyx import llm


class Stub(ThreadingHTTPServer):
    """An OpenAI-compatible server: serves `model`; `strict` refuses another model name with 404."""

    def __init__(self, model, strict=False, answer="fine"):
        self.model, self.strict, self.answer, self.asked = model, strict, answer, []
        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"data": [{"id": self.server.model}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.asked.append(body["model"])
        if self.server.strict and body["model"] != self.server.model:
            return self._send(404, {"error": "no such model"})
        self._send(200, {"model": self.server.model, "choices": [{"message": {"content": self.server.answer}}]})


@pytest.fixture()
def health(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "HEALTH", tmp_path / "assistant-health.json")
    monkeypatch.setattr(llm, "TOPO", tmp_path / "llm-topology.json")
    return tmp_path


def topo(*servers):
    return {"hosts": {f"gpu-0{i}": {"servers": [{"role": "serving", "engine": "tensorfold", "model": s.model,
                                                  "target": s.url[7:-3]}]} for i, s in enumerate(servers, 1)}}


def test_endpoints_are_the_assistants_own_then_what_discovery_found(health):
    t = {"hosts": {"gpu-01": {"servers": [{"role": "serving", "engine": "tensorfold", "model": "m1", "target": "192.0.2.21:8000"},
                                          {"role": "worker", "engine": "tensorfold", "model": "m1", "target": None}]},
                   "gpu-02": {"servers": [{"role": "serving", "engine": "other", "model": "x", "target": "192.0.2.22:1"},
                                          {"role": "serving", "engine": "mtplx", "model": "m2", "target": "192.0.2.22:9501",
                                           "upstream": "http://192.0.2.22:9502"}]}}}
    a = {"url": "http://192.0.2.21:8000/v1", "model": "m1", "key_host": "assistant"}
    assert llm.endpoints(a, t) == [("http://192.0.2.21:8000/v1", "m1", "assistant"),       # listed once
                                   ("http://192.0.2.22:9502/v1", "m2", "gpu-02")]
    stale = {**a, "model": "an-old-name"}                           # the model behind its own url was swapped
    assert [e[:2] for e in llm.endpoints(stale, t)] == [("http://192.0.2.21:8000/v1", "an-old-name"),
                                                        ("http://192.0.2.22:9502/v1", "m2")]   # still one per url
    assert llm.endpoints({**a, "fallback": False}, t) == [("http://192.0.2.21:8000/v1", "m1", "assistant")]
    assert llm.endpoints({"fallback": True}, t)[0] == ("http://192.0.2.21:8000/v1", "m1", "gpu-01")
    (health / "llm-topology.json").write_text(json.dumps(t))                               # read from the file too
    assert len(llm.endpoints(a)) == 2 and llm.endpoints(a, {}) == [("http://192.0.2.21:8000/v1", "m1", "assistant")]


def test_the_first_endpoint_that_answers_is_asked_and_remembered(health):
    own, other = Stub("model-a"), Stub("model-b", answer="from b")
    a = {"url": own.url, "model": "model-a", "timeout_seconds": 5}
    assert llm.ask_any(a, "sys", "q", topo=topo(own, other)) == ("fine", "model-a")
    h = llm.health()
    assert h["answered"] == own.url and h["model"] == "model-a" and h["fallback"] is False and other.asked == []
    own.shutdown(); own.server_close()                                 # the assistant's own server goes away
    assert llm.ask_any(a, "sys", "q", topo=topo(other)) == ("from b", "model-b")
    h = llm.health()
    assert h["answered"] == other.url and h["fallback"] is True
    other.shutdown(); other.server_close()
    text, why = llm.ask_any(a, "sys", "q", topo=topo(other))
    assert text is None and why and llm.health()["answered"] is None and llm.health()["error"] == why
    assert llm.ask_any({"fallback": False}, "sys", "q") == (None, "no endpoint")


def test_a_swapped_models_stale_name_is_asked_again_as_served(health):
    srv = Stub("new-model", strict=True)
    a = {"url": srv.url, "model": "old-model", "fallback": False, "timeout_seconds": 5}
    assert llm.ask_any(a, "sys", "q") == ("fine", "new-model")          # the answer names the model that gave it
    assert srv.asked == ["old-model", "new-model"]
    srv.shutdown(); srv.server_close()


def test_the_probe_counts_endpoints_that_list_a_model_and_is_cached(health):
    own, other = Stub("model-a"), Stub("model-b")
    a = {"url": own.url, "model": "model-a"}
    t = topo(own, other)
    h = llm.probe(a, topo=t)
    assert (h["up"], h["total"], h["preferred"]) == (2, 2, True)
    own.shutdown(); own.server_close()
    assert llm.probe(a, topo=t)["up"] == 2                               # within the cache's age
    h = llm.probe(a, max_age=0, topo=t)
    assert (h["up"], h["total"], h["preferred"]) == (1, 2, False) and h["serving"] == [other.url]
    other.shutdown(); other.server_close()
    assert llm.probe(a, max_age=0, topo=t)["up"] == 0
