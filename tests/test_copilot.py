"""Ask Noctyx: the console's assistant looks things up with read-only tools, within the asker's role, and never
sees a secret a tool's output carried."""
import http.server
import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from noctyx import agent, copilot


class _LLM(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible server that answers from a script and keeps every request it got."""
    replies, seen, no_tools = [], [], False

    def log_message(self, *a):
        pass

    def _send(self, d, code=200):
        b = json.dumps(d).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": "tiny-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        if body.get("tools") and type(self).no_tools:
            return self._send({"error": "tools are not supported"}, 400)
        msg = type(self).replies.pop(0) if type(self).replies else {"content": "done"}
        self._send({"model": "tiny-model", "choices": [{"message": {"role": "assistant", **msg}}]})


def _call(name, cid, **args):
    return {"tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture()
def console(monkeypatch):
    """A console module stand-in: the few functions the copilot reads, and the fake LLM as its assistant."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _LLM.replies, _LLM.seen, _LLM.no_tools = [], [], False
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    C = types.SimpleNamespace(
        CFG={"hosts": [{"name": "gpu-01", "address": "192.0.2.21", "arch": "x86_64"},
                       {"name": "mac-01", "address": "192.0.2.30", "os": "macos", "backup": False}]},
        assistant_cfg=lambda: {"enabled": True, "url": url, "model": "tiny-model"},
        _assistant_endpoints=lambda a: [(url, "tiny-model", None)],
        _api_key=lambda h: None,
        alerts_view=lambda: {"active": [{"name": "DiskFilling", "severity": "warning", "host": "gpu-01",
                                         "summary": "root at 91% (token=abcdef0123456789abcdef)", "fingerprint": "f1"}]},
        CM=types.SimpleNamespace(cfg=lambda: {}, ssh=None))
    copilot.bind(C)
    copilot.SESSIONS.clear()
    yield C
    for s in copilot.SESSIONS.values():
        s.close()
    srv.shutdown()


def _settle(sess, t=10):
    end = time.time() + t
    while time.time() < end:
        with sess.lock:
            if not sess.busy and not sess.queue:
                return
        time.sleep(0.02)
    raise AssertionError("the assistant never settled")


def test_it_looks_before_it_answers_and_never_sees_a_secret(console):
    _LLM.replies = [_call("alerts", "c1"), {"content": "gpu-01's disk is filling."}]
    s = copilot.start("ana", "view")
    s.say("anything wrong?")
    _settle(s)
    tool = [m for m in _LLM.seen[-1]["messages"] if m["role"] == "tool"]
    assert tool[0]["tool_call_id"] == "c1" and "DiskFilling" in tool[0]["content"]
    assert "abcdef0123456789" not in json.dumps(_LLM.seen) and "[redacted]" in tool[0]["content"]
    assert "fingerprint" not in tool[0]["content"]
    assert s.items[-1] == {**s.items[-1], "kind": "assistant", "text": "gpu-01's disk is filling.", "model": "tiny-model"}


def test_redaction():
    out = agent.redact("password=hunter2 Authorization: Bearer abcdefghijklmnop api_key: \"k-123\" "
                       "hf_abcdefghijklmnopqrstuv https://u:pw@host/x\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"
                       "-----END OPENSSH PRIVATE KEY-----")
    for secret in ("hunter2", "abcdefghijklmnop", "k-123", "abcdefghijklmnopqrstuv", ":pw@", "AAAA"):
        assert secret not in out, secret
    assert agent.redact("root at 91% on /dev/nvme0n1p2, 3 tokens/s") == "root at 91% on /dev/nvme0n1p2, 3 tokens/s"


def test_tools_are_reads_and_a_role_gets_only_its_own(console, monkeypatch):
    assert all(t["readonly"] for t in copilot.TOOLS.values())
    assert {n for n, t in copilot.TOOLS.items() if t["need"] != "view"} == {"run_log", "containers", "service_log", "container_log"}
    assert "service_log" not in copilot.start("vic", "view").tools()
    monkeypatch.setitem(copilot.Copilot.TOOLS, "restart_it", agent.fn("restart_it", "x", {}, readonly=False,
                                                                     run=lambda s, a: "restarted")[1])
    _LLM.replies = [_call("restart_it", "c1", why="because"), {"content": "I cannot."}]
    s = copilot.start("vic", "view")
    s.say("restart it")
    _settle(s)
    offered = {t["function"]["name"] for t in _LLM.seen[0]["tools"]}
    assert "restart_it" not in offered and "alerts" in offered
    res = {m["tool_call_id"]: m["content"] for m in _LLM.seen[-1]["messages"] if m["role"] == "tool"}
    assert res["c1"].startswith("error: no tool") and not s.props
    assert "restart_it" in copilot.start("oli", "operate").tools()


def test_a_conversation_is_its_openers(console):
    s = copilot.start("ana", "admin")
    assert copilot.get(s.sid, "ana") is s and s.role == "admin"                  # reading changes nothing
    assert copilot.get(s.sid, "ana", "view") is s and s.role == "view"          # a message: the caller's role now
    with pytest.raises(agent.Bad):
        copilot.get(s.sid, "bob", "admin")
    with pytest.raises(agent.Bad):
        copilot.get("no-such-id", "ana", "admin")


def test_arguments_are_checked(console):
    s = copilot.start(None, "admin")
    for tool, args in [("service_log", {"host": "gpu-01", "unit": "x; reboot"}),
                       ("service_log", {"host": "gpu-01", "unit": "../../etc/passwd.service"}),
                       ("service_log", {"host": "mac-01", "unit": "sshd.service"}),
                       ("container_log", {"host": "gpu-01", "container": "-v /:/x"}),
                       ("service_log", {"host": "nobody", "unit": "sshd.service"}),
                       ("run_log", {"unit": "../../../etc/shadow"}),
                       ("prometheus", {"query": "up\nup"}),
                       ("find_in_backups", {"host": "gpu-01", "pattern": "--help"})]:
        out = s._run(tool, args, None)
        assert out.startswith("refused:"), (tool, args, out)


def test_a_model_without_tools_still_answers(console):
    _LLM.no_tools = True
    _LLM.replies = [{"content": "From what I was given: all is well."}]
    s = copilot.start(None, "admin")
    s.say("how is it?")
    _settle(s)
    assert s.advice_only and s.items[-1]["text"] == "From what I was given: all is well."
    assert "tools" not in _LLM.seen[-1] and s.view()["advice_only"]


def test_the_last_round_must_answer(console, monkeypatch):
    monkeypatch.setattr(copilot.Copilot, "ROUNDS", 2)
    _LLM.replies = [_call("alerts", "c1"), {"content": "<tool_call>\n<function=alerts>\n</function>\n</tool_call>"}]
    s = copilot.start(None, "admin")
    s.say("look forever")
    _settle(s)
    assert "tools" not in _LLM.seen[-1] and "No more lookups" in _LLM.seen[-1]["messages"][-1]["content"]
    assert s.items[-1]["kind"] == "error" and "<tool_call>" not in json.dumps(s.msgs)


def test_the_console_serves_it(console, monkeypatch):
    """POST /api/v1/ask and GET /api/v1/ask/<sid> through the console's own handler (open access)."""
    from noctyx import console as real
    monkeypatch.setenv("NOCTYX_CONSOLE_AUTH", "0")
    copilot.bind(console)                       # importing the console bound itself: keep the stand-in
    _LLM.replies = [{"content": "Hello."}]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), real.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"

    def req(path, body=None):
        r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert req("/ask", {"text": ""})[0] == 400
        code, d = req("/ask", {"text": "hi", "tunit": "C"})
        assert code == 200 and d["sid"]
        _settle(copilot.SESSIONS[d["sid"]])
        code, v = req(f"/ask/{d['sid']}?since=0")
        assert code == 200 and [i["kind"] for i in v["items"]] == ["user", "assistant"] and v["items"][1]["text"] == "Hello."
        assert "°C" in copilot.SESSIONS[d["sid"]].system()
        assert req("/ask/unknown")[0] == 404
    finally:
        srv.shutdown()


def test_it_sees_what_noctyx_repaired_and_which_machine_is_the_head(console, monkeypatch, tmp_path):
    from noctyx import change, heal
    monkeypatch.setattr(heal, "HEAL_FILE", tmp_path / "heal.json")
    monkeypatch.setattr(change, "LEDGER", tmp_path / "changes.jsonl")
    now = time.time()
    heal.HEAL_FILE.write_text(json.dumps({"ts": now, "problems": [], "repairs": [
        {"ts": "2026-10-24T05:49:59+00:00", "ts_epoch": now - 60, "host": "gpu-01", "check": "dropin",
         "what": "the node_exporter drop-in differs from Noctyx's", "how": "write it again", "ok": True, "detail": "ok"}]}))
    change.record({"ts": "2026-10-24T05:00:00+00:00", "ts_epoch": now - 3600, "host": "gpu-01", "component": "node_exporter",
                   "what": "node_exporter 1.12.1 on gpu-01", "outcome": "rolled_back", "problems": ["a scrape took 10s"],
                   "snap": {"big": "x" * 100}})
    out = json.dumps(copilot.start(None, "view")._run("repairs", {}, None))
    assert "write it again" in out and "rolled_back" in out and "a scrape took 10s" in out and "xxxx" not in out
    console.CFG["server"] = {"address": "192.0.2.21"}
    assert "gpu-01 (linux x86_64, backed up, the head)" in copilot.start(None, "view").system()


def test_the_prompt_stays_put_and_each_question_carries_its_time(console, monkeypatch):
    """The system prompt is the same on every round of a conversation (the server's prompt cache keeps it and the
    tools after it); the time goes with each question, in the asker's zone when the browser gave a real one."""
    console.CM = types.SimpleNamespace(cfg=lambda: {}, rec_label=lambda r: r["name"], recipe_summary=lambda c: {"slots": {
        "pair": {"head": "gpu-01", "worker": "gpu-02", "port": 8000, "current": {"name": "big-model"},
                 "live": {"ok": True, "models": ["swapped-in-by-hand"]}},
        "solo": {"head": "mac-01", "port": 9000, "current": {"name": "small-model"}, "live": {"ok": False}}}})
    console.CFG["hosts"].append({"name": "gpu-02", "address": "192.0.2.22", "arch": "aarch64"})
    clock = [1792817000.0]
    monkeypatch.setattr(copilot.time, "time", lambda: clock[0])
    _LLM.replies = [_call("alerts", "c1"), {"content": "All quiet."}, {"content": "Still quiet."}]
    s = copilot.start("ana", "view", "F", "Europe/Paris")
    s.say("anything wrong?")
    _settle(s)
    clock[0] += 3600
    s.say("and now?")
    _settle(s)
    systems = {m["messages"][0]["content"] for m in _LLM.seen}
    assert len(systems) == 1
    system = systems.pop()
    assert "slot pair: serves swapped-in-by-hand on :8000, a tensor-parallel pair with gpu-02" in system
    assert "slot solo: Noctyx last started small-model on :9000, which did not answer" in system
    assert "gpu-02 (linux aarch64, backed up; slot pair: the tensor-parallel worker of gpu-01)" in system
    assert "Not among your tools: run_log, containers, service_log, container_log" in system and "Europe/Paris" in system
    asked = [m["content"] for m in _LLM.seen[-1]["messages"] if m["role"] == "user"]
    assert asked[0].startswith("(asked 2026-10-24 04:43 UTC = 06:43 Europe/Paris)\n") and asked[0].endswith("anything wrong?")
    assert asked[1].startswith("(asked 2026-10-24 05:43 UTC")
    assert [i["text"] for i in s.items if i["kind"] == "user"] == ["anything wrong?", "and now?"]   # the page shows no stamp
    assert "You have every tool" in copilot.start("oli", "admin").system()
    for bad in ("Mars/Olympus", "../../etc/passwd", "x" * 80, ""):
        assert copilot._zone(bad) is None
    assert "give times in UTC" in copilot.start("ana", "view", "F", "Mars/Olympus").system()
