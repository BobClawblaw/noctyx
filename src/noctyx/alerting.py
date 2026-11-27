"""External alert routing (phone/chat/email/webhook) and Alertmanager config rendering for Linux & macOS.

Config lives in `site.json["alerting"]` (no secrets — secrets live in 0600 files under `<etc>/alert-keys/`
or an explicit `*_file` path). Every alert ALWAYS delivers to the local Noctyx console webhook; external
channels receive alerts matching their `min_severity` ("all" | "warning" | "critical").
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import urllib.request

from noctyx.paths import P

CHANNELS = ("ntfy", "pushover", "slack", "telegram", "webhook", "email")
SEVERITIES = ("all", "warning", "critical")
_URL = re.compile(r"^https?://[A-Za-z0-9._:-]+(?:/[^\s\"'<>]*)?$")
_HOSTPORT = re.compile(r"^[A-Za-z0-9._-]+:\d{1,5}$")
_EMAIL = re.compile(r"^[^@\s\"'<>]+@[^@\s\"'<>]+\.[^@\s\"'<>]+$")


def _keys_dir():
    return pathlib.Path(P["etc"]) / "alert-keys"


def _one_line(s):
    return isinstance(s, str) and bool(s.strip()) and not any(c in s for c in "\n\r\0")


def _abs_path(s):
    return _one_line(s) and s.startswith("/") and ".." not in s.split("/")


def _read_secret(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return ""


def _yaml_quote(s: str) -> str:
    return json.dumps(str(s))


def validate_alerting(cfg):
    """Validate `site.json["alerting"]` -> (clean_dict | None, errors)."""
    if cfg is None:
        return {}, []
    if not isinstance(cfg, dict):
        return None, ["alerting: must be an object"]
    errs = []
    clean = {}
    if "repeat_interval" in cfg:
        ri = str(cfg["repeat_interval"]).strip()
        if not re.fullmatch(r"\d{1,3}[mhd]", ri):
            errs.append("alerting.repeat_interval: e.g. 4h, 12h, 1d")
        else:
            clean["repeat_interval"] = ri

    chans = cfg.get("channels") or {}
    if not isinstance(chans, dict):
        return None, errs + ["alerting.channels: must be an object"]
    out_ch = {}
    for name, c in chans.items():
        if not isinstance(c, dict):
            errs.append(f"alerting.channels.{name}: must be an object")
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(name)):
            errs.append(f"alerting.channels.{name}: lowercase letters, digits, - _")
            continue
        kind = str(c.get("kind") or "").strip().lower()
        if kind not in CHANNELS:
            errs.append(f"alerting.channels.{name}.kind: one of {', '.join(CHANNELS)}")
            continue
        sev = str(c.get("min_severity") or "warning").strip().lower()
        if sev not in SEVERITIES:
            errs.append(f"alerting.channels.{name}.min_severity: one of {', '.join(SEVERITIES)}")
            continue
        ent = {"kind": kind, "enabled": c.get("enabled", True) is not False, "min_severity": sev,
               "send_resolved": c.get("send_resolved", True) is not False}

        if kind in ("ntfy", "webhook"):
            url = str(c.get("url") or "").strip()
            if not _URL.match(url):
                errs.append(f"alerting.channels.{name}.url: https://... required")
            else:
                ent["url"] = url
            tf = c.get("token_file")
            if tf is not None:
                if not _abs_path(tf):
                    errs.append(f"alerting.channels.{name}.token_file: absolute path required")
                else:
                    ent["token_file"] = tf

        elif kind == "pushover":
            for fk in ("user_key_file", "token_file"):
                v = c.get(fk)
                if not _abs_path(v or ""):
                    errs.append(f"alerting.channels.{name}.{fk}: absolute path required")
                else:
                    ent[fk] = v
            if c.get("priority") is not None:
                ent["priority"] = str(c["priority"]).strip()

        elif kind == "slack":
            wf = c.get("webhook_url_file")
            if not _abs_path(wf or ""):
                errs.append(f"alerting.channels.{name}.webhook_url_file: absolute path required")
            else:
                ent["webhook_url_file"] = wf
            if c.get("channel"):
                if not _one_line(c["channel"]):
                    errs.append(f"alerting.channels.{name}.channel: single-line string")
                else:
                    ent["channel"] = c["channel"].strip()

        elif kind == "telegram":
            bf = c.get("bot_token_file")
            if not _abs_path(bf or ""):
                errs.append(f"alerting.channels.{name}.bot_token_file: absolute path required")
            else:
                ent["bot_token_file"] = bf
            try:
                ent["chat_id"] = int(c["chat_id"])
            except (KeyError, TypeError, ValueError):
                errs.append(f"alerting.channels.{name}.chat_id: integer required")

        elif kind == "email":
            sh = str(c.get("smarthost") or "").strip()
            fr = str(c.get("from") or "").strip()
            to = str(c.get("to") or "").strip()
            if not _HOSTPORT.match(sh):
                errs.append(f"alerting.channels.{name}.smarthost: host:port required")
            if not _EMAIL.match(fr):
                errs.append(f"alerting.channels.{name}.from: email address required")
            if not _EMAIL.match(to):
                errs.append(f"alerting.channels.{name}.to: email address required")
            ent.update(smarthost=sh, **{"from": fr}, to=to, require_tls=c.get("require_tls", True) is not False)
            if c.get("auth_username"):
                ent["auth_username"] = str(c["auth_username"]).strip()
            if c.get("auth_password_file"):
                if not _abs_path(c["auth_password_file"]):
                    errs.append(f"alerting.channels.{name}.auth_password_file: absolute path required")
                else:
                    ent["auth_password_file"] = c["auth_password_file"]

        out_ch[name] = ent

    if out_ch:
        clean["channels"] = out_ch
    return (None, errs) if errs else (clean, [])


def render_alertmanager_yml(site_cfg: dict, ca_file: str | None = None) -> str:
    """Render a complete Alertmanager configuration for this site (Linux or macOS).

    The `console` receiver is always active; any enabled channels in `site_cfg["alerting"]["channels"]`
    are attached as child routes with `continue: true` so both the console and external phones/chats receive alerts.
    """
    ui_port = int((site_cfg.get("server") or {}).get("ui_port", 20996))
    ca = ca_file or f"{P['tls']}/ca.crt"
    acfg, errs = validate_alerting(site_cfg.get("alerting"))
    if errs or not acfg:
        acfg = {}
    repeat = acfg.get("repeat_interval") or "12h"
    channels = {k: v for k, v in (acfg.get("channels") or {}).items() if v.get("enabled", True)}

    lines = [
        "# managed by noctyx alerting: change site.json alerting block or `noctyx alerting-set`",
        "global:",
        "  resolve_timeout: 5m",
        "route:",
        "  receiver: console",
        "  group_by: [alertname, host]",
        "  group_wait: 30s",
        "  group_interval: 5m",
        f"  repeat_interval: {repeat}",
    ]

    if channels:
        lines.append("  routes:")
        # Console ALWAYS receives every alert first (continue: true)
        lines.append("  - receiver: console")
        lines.append("    continue: true")
        for name, ch in sorted(channels.items()):
            lines.append(f"  - receiver: ch_{name}")
            lines.append("    continue: true")
            sev = ch.get("min_severity", "warning")
            if sev == "critical":
                lines.append('    matchers: [severity="critical"]')
            elif sev == "warning":
                lines.append('    matchers: [severity=~"warning|critical"]')

    lines += [
        "receivers:",
        "- name: console",
        "  webhook_configs:",
        f"  - url: https://127.0.0.1:{ui_port}/api/v1/alert-hook",
        "    send_resolved: true",
        "    http_config:",
        "      tls_config:",
        f"        ca_file: {_yaml_quote(ca)}",
    ]

    for name, ch in sorted(channels.items()):
        kind = ch["kind"]
        sr = "true" if ch.get("send_resolved", True) else "false"
        lines.append(f"- name: ch_{name}")
        if kind in ("ntfy", "webhook"):
            lines.append("  webhook_configs:")
            lines.append(f"  - url: {_yaml_quote(ch['url'])}")
            lines.append(f"    send_resolved: {sr}")
            if ch.get("token_file"):
                lines.append("    http_config:")
                lines.append("      authorization:")
                lines.append(f"        credentials_file: {_yaml_quote(ch['token_file'])}")
        elif kind == "pushover":
            lines.append("  pushover_configs:")
            lines.append(f"  - user_key_file: {_yaml_quote(ch['user_key_file'])}")
            lines.append(f"    token_file: {_yaml_quote(ch['token_file'])}")
            lines.append(f"    send_resolved: {sr}")
            if ch.get("priority"):
                lines.append(f"    priority: {_yaml_quote(ch['priority'])}")
        elif kind == "slack":
            lines.append("  slack_configs:")
            lines.append(f"  - api_url_file: {_yaml_quote(ch['webhook_url_file'])}")
            lines.append(f"    send_resolved: {sr}")
            if ch.get("channel"):
                lines.append(f"    channel: {_yaml_quote(ch['channel'])}")
        elif kind == "telegram":
            lines.append("  telegram_configs:")
            lines.append(f"  - bot_token_file: {_yaml_quote(ch['bot_token_file'])}")
            lines.append(f"    chat_id: {ch['chat_id']}")
            lines.append(f"    send_resolved: {sr}")
        elif kind == "email":
            lines.append("  email_configs:")
            lines.append(f"  - to: {_yaml_quote(ch['to'])}")
            lines.append(f"    from: {_yaml_quote(ch['from'])}")
            lines.append(f"    smarthost: {_yaml_quote(ch['smarthost'])}")
            lines.append(f"    require_tls: {'true' if ch.get('require_tls', True) else 'false'}")
            lines.append(f"    send_resolved: {sr}")
            if ch.get("auth_username"):
                lines.append(f"    auth_username: {_yaml_quote(ch['auth_username'])}")
            if ch.get("auth_password_file"):
                lines.append(f"    auth_password_file: {_yaml_quote(ch['auth_password_file'])}")

    return "\n".join(lines) + "\n"


def send_test_alert(am_url: str = "http://127.0.0.1:9093", host: str = "head", timeout: int = 8) -> dict:
    """Post a short-lived synthetic test alert to Alertmanager v2 API to verify external delivery."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ends = now + datetime.timedelta(minutes=2)
    payload = [{
        "labels": {
            "alertname": "NoctyxTestAlert",
            "severity": "warning",
            "host": host,
            "source": "noctyx-test",
        },
        "annotations": {
            "summary": "Noctyx test alert — external routing check",
            "description": "Triggered by `noctyx alerting-test`. Auto-resolves in 2 minutes.",
        },
        "startsAt": now.isoformat().replace("+00:00", "Z"),
        "endsAt": ends.isoformat().replace("+00:00", "Z"),
    }]
    req = urllib.request.Request(
        f"{am_url.rstrip('/')}/api/v2/alerts",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {"ok": 200 <= r.status < 300, "status": r.status}
