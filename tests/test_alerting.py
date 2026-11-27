"""Tests for external alert routing (ntfy, pushover, slack, telegram, webhook, email) and macOS Alertmanager."""
import http.server
import json
import plistlib
import threading

from noctyx import alerting, hostos, orchestrator as cm, setup


def test_validate_alerting_channels():
    cfg = {
        "repeat_interval": "6h",
        "channels": {
            "phone": {"kind": "ntfy", "url": "https://ntfy.sh/my-cluster", "token_file": "/etc/noctyx/alert-keys/ntfy.tok", "min_severity": "critical"},
            "push": {"kind": "pushover", "user_key_file": "/etc/noctyx/alert-keys/po.user", "token_file": "/etc/noctyx/alert-keys/po.tok"},
            "ops-slack": {"kind": "slack", "webhook_url_file": "/etc/noctyx/alert-keys/slack.url", "channel": "#alerts"},
            "tg": {"kind": "telegram", "bot_token_file": "/etc/noctyx/alert-keys/tg.tok", "chat_id": -100123456},
            "mail": {"kind": "email", "smarthost": "smtp.example.com:587", "from": "noctyx@example.com", "to": "ops@example.com", "auth_username": "noctyx", "auth_password_file": "/etc/noctyx/alert-keys/smtp.pass"},
        },
    }
    clean, errs = alerting.validate_alerting(cfg)
    assert errs == [] and len(clean["channels"]) == 5 and clean["repeat_interval"] == "6h"

    bad, berrs = alerting.validate_alerting({
        "repeat_interval": "forever",
        "channels": {
            "x": {"kind": "ntfy", "url": "ftp://bad"},
            "y": {"kind": "telegram", "bot_token_file": "relative", "chat_id": "nope"},
            "z": {"kind": "email", "smarthost": "bad", "from": "no", "to": "no"},
        },
    })
    assert bad is None and len(berrs) >= 5


def test_render_alertmanager_yml_routes_console_and_channels():
    site = {
        "server": {"ui_port": 20996},
        "alerting": {
            "repeat_interval": "4h",
            "channels": {
                "phone": {"kind": "ntfy", "url": "https://ntfy.sh/test-topic", "token_file": "/etc/noctyx/alert-keys/ntfy.tok", "min_severity": "critical"},
                "chat": {"kind": "slack", "webhook_url_file": "/etc/noctyx/alert-keys/slack.url", "min_severity": "warning"},
                "off": {"kind": "webhook", "url": "https://example.com/hook", "enabled": False},
            },
        },
    }
    yml = alerting.render_alertmanager_yml(site, ca_file="/etc/noctyx/tls/ca.crt")
    assert "repeat_interval: 4h" in yml
    assert "https://127.0.0.1:20996/api/v1/alert-hook" in yml
    assert "receiver: console" in yml and "continue: true" in yml
    assert "- name: ch_phone" in yml and 'credentials_file: "/etc/noctyx/alert-keys/ntfy.tok"' in yml
    assert 'matchers: [severity="critical"]' in yml
    assert "- name: ch_chat" in yml and 'api_url_file: "/etc/noctyx/alert-keys/slack.url"' in yml
    assert "ch_off" not in yml


def test_prometheus_and_alertmanager_macos_launchd_wiring():
    c = cm.cfg()
    py = setup.prometheus_yml(c)
    assert "alertmanagers:" in py and "127.0.0.1:9093" in py
    assert "alertmanager.service" in setup.METRICS_UNITS
    unit = cm.render_unit(c, "alertmanager.service")
    assert "@" not in unit and "--config.file=" in unit and "127.0.0.1:9093" in unit
    plist = plistlib.loads(hostos.launchd_plist("dev.noctyx.alertmanager", unit).encode())
    assert plist["ProgramArguments"][0] == "/usr/local/bin/alertmanager"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True


def test_send_test_alert_posts_to_v2_api():
    got = []

    class AM(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append((self.path, json.loads(self.rfile.read(n))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AM)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = alerting.send_test_alert(am_url=f"http://127.0.0.1:{port}", host="head-01")
        assert res["ok"] is True and res["status"] == 200
        assert got[0][0] == "/api/v2/alerts"
        assert got[0][1][0]["labels"]["alertname"] == "NoctyxTestAlert"
        assert got[0][1][0]["labels"]["host"] == "head-01"
    finally:
        srv.shutdown()
        srv.server_close()
