"""noctyx init --web: the assistant proposes, the operator approves, and only then does anything change."""
import http.server
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest

from noctyx import firstrun, hostos, setupagent, setupweb


class _Script(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible server that answers from a script and keeps every request it got."""
    replies, seen = [], []

    def log_message(self, *a):
        pass

    def _send(self, d):
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": "tiny-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        if not body.get("tools"):                       # the endpoint test
            return self._send({"model": "tiny-model", "choices": [{"message": {"content": "Noctyx assistant OK"}}]})
        msg = type(self).replies.pop(0) if type(self).replies else {"content": "done"}
        self._send({"model": "tiny-model", "choices": [{"message": {"role": "assistant", **msg}}]})


def _call(name, cid, **args):
    return {"tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture()
def site(tmp_path, monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Script)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Script.replies, _Script.seen = [], []
    cfg = tmp_path / "site.json"
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    cfg.write_text(json.dumps({"assistant": {"enabled": True, "url": url, "model": "tiny-model"}}))
    for m in (setupagent, setupweb, firstrun):
        monkeypatch.setattr(m, "CONFIG", str(cfg))
    monkeypatch.setattr(hostos, "IS_MAC", False)
    monkeypatch.setattr(firstrun, "_ssh_key", lambda c: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(firstrun, "_node_arch", lambda c, addr: "x86_64")
    monkeypatch.setattr(setupagent, "admin_exists", lambda: False)
    monkeypatch.setattr(setupagent, "steps", lambda c: [])
    yield cfg
    srv.shutdown()


def _settle(sess, t=10):
    end = time.time() + t
    while time.time() < end:
        with sess.lock:
            if not sess.busy and not sess.queue:
                return
        time.sleep(0.02)
    raise AssertionError("the assistant never settled")


def test_arguments_are_checked_before_anything_runs():
    for bad in ("a b", "x;rm -rf /", "$(id)", "", "-oProxyCommand=x"):
        with pytest.raises(setupagent.Bad):
            setupagent._address(bad)
    for bad in ("Gpu 1", "../x", "-a", "a" * 64):
        with pytest.raises(setupagent.Bad):
            setupagent._name(bad)
    for bad in ("relative", "/a/../b", "/a b", "/a;b", "/a$b"):
        with pytest.raises(setupagent.Bad):
            setupagent._path(bad)
    with pytest.raises(setupagent.Bad):
        setupagent._port(21000)
    assert setupagent._address("192.0.2.21") == "192.0.2.21" and setupagent._name("GPU-01") == "gpu-01"


def test_a_change_waits_for_the_operator(site):
    _Script.replies = [_call("site_state", "c1"),
                       {"content": "This machine can be the head.", **_call("configure_head", "c2", hostname="head-01",
                                                                            address="192.0.2.10", back_up_head=True)},
                       {"content": "The head is set up."}]
    s = setupagent.Session()
    s.say("start")
    _settle(s)
    assert "server" not in json.loads(site.read_text())          # proposed, not done
    prop = next(i for i in s.items if i["kind"] == "proposal")
    assert prop["tool"] == "configure_head" and prop["args"]["hostname"] == "head-01"
    tool_msgs = [m for m in _Script.seen[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1"]       # the read-only call ran by itself
    s.decide(prop["pid"], True, {"hostname": "head-02", "address": "192.0.2.10", "back_up_head": True})
    _settle(s)
    c = json.loads(site.read_text())
    assert c["server"]["hostname"] == "head-02" and c["server"]["address"] == "192.0.2.10"   # the operator's edit won
    last = _Script.seen[-1]["messages"]
    assert last[-1]["role"] == "tool" and last[-1]["tool_call_id"] == "c2"
    assert s.items[-1]["kind"] == "assistant" and s.items[-1]["text"] == "The head is set up."


def test_a_rejected_change_does_nothing_and_the_assistant_hears_why(site):
    _Script.replies = [_call("set_sign_in", "c1", required=False), {"content": "Understood."}]
    c = json.loads(site.read_text())
    firstrun.apply(c, ["head"], hostname="head-01", address="192.0.2.10")
    site.write_text(json.dumps(c))
    s = setupagent.Session()
    s.say("open the console to everyone")
    _settle(s)
    pid = next(i["pid"] for i in s.items if i["kind"] == "proposal")
    s.decide(pid, False)
    _settle(s)
    assert "console_auth" not in json.loads(site.read_text())["server"]
    assert _Script.seen[-1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "rejected by the operator"}


def test_unknown_tools_and_bad_arguments_are_refused_to_the_llm(site):
    _Script.replies = [{"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
                                       {"id": "c2", "type": "function", "function": {"name": "probe_host",
                                                                                     "arguments": '{"address": "x; id"}'}}]},
                       {"content": "ok"}]
    s = setupagent.Session()
    s.say("run id")
    _settle(s)
    res = {m["tool_call_id"]: m["content"] for m in _Script.seen[-1]["messages"] if m["role"] == "tool"}
    assert res["c1"].startswith("error: no tool") and res["c2"].startswith("refused: not an address")


def test_the_web_setup_needs_its_link_and_keeps_passwords_from_the_llm(site, tmp_path, monkeypatch):
    import subprocess
    crt, key = tmp_path / "c.crt", tmp_path / "c.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "1", "-subj", "/CN=t", "-keyout", key, "-out", crt], check=True, capture_output=True)
    made = {}
    from noctyx import accounts
    monkeypatch.setattr(accounts, "check_new", lambda n, r, p: [])
    monkeypatch.setattr(accounts, "add", lambda n, r, p: made.update(name=n, pw=p))
    monkeypatch.setitem(setupweb.P, "etc", str(tmp_path / "etc"))
    setupweb.TOKEN, setupweb.SESSION = "tok-123", setupagent.Session()
    srv = setupweb.SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), setupweb.H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"https://127.0.0.1:{srv.server_address[1]}"
    cctx = ssl.create_default_context(); cctx.check_hostname = False; cctx.verify_mode = ssl.CERT_NONE

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=cctx), NoRedirect)

    def req(path, body=None, cookie=None, hdr=True):
        r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None)
        if cookie:
            r.add_header("Cookie", cookie)
        if hdr and body is not None:
            r.add_header("X-Noctyx-Setup", "1")
        try:
            with op.open(r, timeout=10) as f:
                return f.status, f.headers, f.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
    try:
        assert req("/")[0] == 403 and req("/api/view")[0] == 403
        assert req("/?t=wrong")[0] == 403
        code, h, _ = req("/?t=tok-123")
        assert code == 303 and "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
        ck = h["Set-Cookie"].split(";")[0]
        code, h, page = req("/", cookie=ck)
        assert code == 200 and b"NOCTYX SETUP" in page and "nonce-" in h["Content-Security-Policy"]
        assert req("/api/admin", {"name": "ops", "password": "x" * 12}, cookie=ck, hdr=False)[0] == 403   # no CSRF header
        assert req("/api/admin", {"name": "ops", "password": "correct horse battery"}, cookie=ck)[0] == 200
        assert made == {"name": "ops", "pw": "correct horse battery"}
        assert not any("correct horse" in json.dumps(b) for b in _Script.seen)
        v = json.loads(req("/api/view", cookie=ck)[2])
        assert v["state"]["assistant"]["model"] == "tiny-model"
    finally:
        srv.shutdown()


def test_every_proposal_carries_a_reason_and_reads_are_not_proposals():
    for name, t in setupagent.TOOLS.items():
        req = t["schema"]["function"]["parameters"]["required"]
        assert ("why" in req) == (not t["readonly"]), name
    assert set(setupagent.JOB_TOOLS) <= {n for n, t in setupagent.TOOLS.items() if not t["readonly"]}


def test_guide_sections_by_heading():
    text = setupagent.t_read_guide(None, {"page": "install", "section": "macos"})
    assert text.startswith("## macOS specifics")
    assert "headings:" in setupagent.t_read_guide(None, {"page": "install", "section": "no such heading"})


def test_apply_is_init_without_questions(site):
    c = {}
    firstrun.apply(c, ["head", "storage", "hosts", "console"], hostname="head-01", address="192.0.2.10",
                   storage="local", path="/srv/nx", node=["gpu-01=192.0.2.21"], no_auth=True)
    assert c["server"]["hostname"] == "head-01" and c["server"]["repo_root"] == "/srv/nx/repos"
    assert [h["name"] for h in c["hosts"]] == ["head-01", "gpu-01"] and c["server"]["console_auth"] is False
    with pytest.raises(ValueError):
        firstrun.apply(c, ["head"], shell="id")


def test_a_mac_is_added_monitored_and_gets_mac_metrics(site, monkeypatch):
    c = json.loads(site.read_text())
    firstrun.apply(c, ["head", "storage", "hosts"], hostname="head-01", address="192.0.2.10", storage="local", path="/srv/nx")
    site.write_text(json.dumps(c))
    monkeypatch.setattr(setupagent, "probe", lambda c, addr: {"login": True, "mac": True, "arch": "aarch64",
                                                              "ssh_user": "ops", "passwordless_sudo": True})
    s = setupagent.Session()
    setupagent.t_add_node(s, {"name": "mac-01", "address": "192.0.2.30"})
    mac = json.loads(site.read_text())["hosts"][-1]
    assert mac == {"name": "mac-01", "address": "192.0.2.30", "arch": "aarch64", "os": "macos", "backup": False, "ssh_user": "ops"}
    started = []
    monkeypatch.setattr(s, "start_job", lambda what, argv: started.append(argv) or {"job": "j1"})
    setupagent.t_install_node_metrics(s, {"host": "mac-01"})
    assert started == [["tool", "install-mac-metrics", "mac-01"]]
    with pytest.raises(setupagent.Bad):
        setupagent.t_install_agent(s, {"host": "mac-01"})               # a Mac gets no backup agent
