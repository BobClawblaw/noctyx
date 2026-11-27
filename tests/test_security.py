"""Security and correctness regression tests for accounts and console HTTP handling."""
import http.client
import json
import ssl
import threading

import pytest

from noctyx import accounts as ac
from noctyx import console


@pytest.fixture()
def isolated_accounts(tmp_path, monkeypatch):
    monkeypatch.setitem(ac.P, "etc", str(tmp_path / "etc"))
    monkeypatch.setitem(ac.P, "console_state", str(tmp_path / "state"))
    ac.add("alice", "operator", "super-secret-pass-1")
    return tmp_path


def test_password_change_invalidates_existing_sessions(isolated_accounts):
    s1 = ac.make_session("alice", ttl=3600)
    assert ac.check_session(s1) == ("alice", "operator")
    # Role change preserves active session and reads new role live
    ac.set_role("alice", "admin")
    assert ac.check_session(s1) == ("alice", "admin")
    # Password change bumps session_epoch and immediately invalidates old cookie
    ac.set_password("alice", "brand-new-secret-2")
    assert ac.check_session(s1) is None
    # New session works
    s2 = ac.make_session("alice", ttl=3600)
    assert ac.check_session(s2) == ("alice", "admin")
    # Explicit revoke_sessions invalidates s2
    ac.revoke_sessions("alice")
    assert ac.check_session(s2) is None


def test_concurrent_session_key_creation_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setitem(ac.P, "etc", str(tmp_path / "etc"))
    keys = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        keys.append(ac._key())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1 and len(keys[0]) == 32


def test_settings_rejects_path_traversal(isolated_accounts):
    assert ac.get_settings("../etc/passwd") == {}
    with pytest.raises(ValueError):
        ac.put_settings("../escape", {"theme": "mono"})


def test_ok_path_and_safe_filename():
    assert console._ok_path("/etc/hosts")
    assert not console._ok_path("/etc/hosts\r\nX-Evil: 1")
    assert not console._ok_path("/etc/hosts\n")
    assert not console._ok_path("/etc/hosts\x00")
    assert not console._ok_path("relative/path")
    assert console._safe_filename('report "2026"\r\nX-Inject: 1.txt') == "report 2026X-Inject: 1.txt"
    assert console._safe_filename('""\\\r\n') == "download"


def test_console_security_headers_body_limit_and_split_assets(monkeypatch):
    monkeypatch.setattr(console, "DEMO", False)
    monkeypatch.setattr(console, "console_mode", lambda: "open")
    srv = console.ThreadingHTTPServer(("127.0.0.1", 0), console.H)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # 1. Security headers on GET /login and split assets
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/login")
        r = c.getresponse()
        body = r.read()
        assert r.status == 200
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        assert r.getheader("X-Frame-Options") == "DENY"
        assert r.getheader("Referrer-Policy") == "no-referrer"
        assert r.getheader("Strict-Transport-Security") == "max-age=31536000"
        assert b"<script>" in body and b"__THEME_JS__" not in body

        for asset, ctype in (("/assets/console.css", "text/css"),
                             ("/assets/console.js", "application/javascript"),
                             ("/assets/theme.js", "application/javascript")):
            c.request("GET", asset)
            ra = c.getresponse()
            ab = ra.read()
            assert ra.status == 200 and ctype in (ra.getheader("Content-Type") or "") and len(ab) > 500

        c.request("GET", "/")
        rp = c.getresponse()
        pb = rp.read()
        assert rp.status == 200
        assert b"/assets/console.css?v=" in pb and b"/assets/console.js?v=" in pb and b"window.NX_CFG=" in pb

        # 2. Oversized POST returns 413 Payload Too Large
        c.request("POST", "/api/v1/login", body=b"{}",
                  headers={"Content-Type": "application/json", "Content-Length": str(console.MAX_BODY + 1)})
        r413 = c.getresponse()
        r413.read()
        assert r413.status == 413
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()
