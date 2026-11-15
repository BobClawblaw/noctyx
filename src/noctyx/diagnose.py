"""A likely cause and a next step for each problem self-repair hands to a person, from the site's LLM.

When `noctyx heal` finds something it will not or cannot repair, the person who gets the alert starts from
"what" and "why it is your turn". This adds what they would look up first: the unit's last log lines, what
Noctyx changed on that machine lately, what it already tried, the recent audit records for it. The assistant
reads those and writes a short diagnosis that is kept with the problem (heal.json), shown on the Self-repair
page, the machine's page and in `noctyx heal`'s output.

It only reads, through commands fixed here (a journal tail, df, a log file's tail), and only suggests: nothing it
writes is run. Evidence is redacted (keys, tokens, passwords) before the model sees it. A problem is diagnosed
once, and again when what it says changes or a day has passed; at most MAX_PER_RUN per heal run, so a broken
assistant or a storm of findings never holds the repair loop up for long.
"""
import json
import os
import re
import shlex
import time

from noctyx import change, llm
from noctyx.paths import P

MAX_PER_RUN = 2
AGAIN_AFTER = 86400
UNIT = re.compile(r"\b([A-Za-z0-9@_.:-]{1,100}\.(?:service|timer|mount|socket))\b")
SYSTEM = """You are the diagnostician of Noctyx, a self-hosted operator for a small AI cluster (GPU nodes \
serving LLMs, restic backups to a NAS, Prometheus). Noctyx's repair loop found a problem it leaves to a person. \
From the evidence below, say in at most 6 short lines: the most likely cause (name the log line or fact that \
shows it), then the next one or two things the person should do, as exact commands in backticks where there is \
one. Use only the evidence: if it does not show the cause, say so and name the one command whose output would. \
Never suggest turning off a protection (a firewall, TLS verification, a lock, a backup), deleting data, or \
restarting a model server or a container without saying what it interrupts. No preamble, no headings."""


def _cm():
    from noctyx import orchestrator as cm
    return cm


def _run(c, host, cmd, timeout=25):
    """A read-only snippet on a host -> its output, or a note that it could not be read."""
    try:
        r = _cm()._on(c, host, cmd, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()[-3000:] or "(no output)"
    except Exception as e:
        return f"(could not read: {str(e)[:100]})"


def evidence(c, p, repairs=(), run=_run):
    """What a person would look up first for this problem, as text. p: a heal problem record."""
    cm = _cm()
    host = next((h for h in cm.hosts(c) if h["name"] == p.get("host")), None)
    out = [f"problem: {p.get('what')}", f"machine: {p.get('host')}", f"check: {p.get('check')}",
           f"detail: {p.get('detail') or '-'}", f"why it is a person's turn: {p.get('why') or '-'}",
           f"open for: {cm.fmt_duration(time.time() - (p.get('since') or time.time()))}"]
    mine = [r for r in repairs if r.get("key") == p.get("key")][-4:]
    if mine:
        out.append("what Noctyx already tried:")
        out += [f"  {r.get('ts')} {r.get('how')} -> {'ok' if r.get('ok') else 'failed'}: {r.get('detail')}" for r in mine]
    linux = bool(host) and host.get("os", "linux") == "linux" and (host.get("address") or host.get("local"))
    if linux:
        units = list(dict.fromkeys(UNIT.findall(f"{p.get('what')} {p.get('detail')}")))[:2]
        for u in units:
            q = shlex.quote(u)
            out.append(f"--- {u} on {p['host']}: state, then its last log lines ---")
            out.append(run(c, host, f"systemctl show -p ActiveState,SubState,Result,ExecMainStatus,NRestarts {q} 2>&1; "
                                    f"journalctl --no-pager -o short-iso -u {q} -n 40 2>&1"))
        if p.get("check") == "backups":
            out.append(f"--- the last backup's log on {p['host']} ---")
            out.append(run(c, host, f"tail -n 40 {shlex.quote(P['node_lib'])}/last-backup.log 2>&1"))
        if p.get("check") == "disk":
            out.append(f"--- filesystems on {p['host']} ---")
            out.append(run(c, host, "df -h -x tmpfs -x devtmpfs -x overlay 2>&1 | head -20"))
        if not units and p.get("check") in ("service", "ready", "exporter", "textfile", "nas"):
            out.append(f"--- failed units on {p['host']} ---")
            out.append(run(c, host, "systemctl --failed --no-legend --plain 2>&1 | head -15"))
    since = time.time() - 2 * 86400
    ch = [r for r in change.ledger(400) if r.get("host") == p.get("host") and (r.get("ts_epoch") or 0) > since][-6:]
    if ch:
        out.append("changes Noctyx made on that machine in the last 2 days:")
        out += [f"  {r.get('ts')} {r.get('what') or r.get('component')}: {r.get('outcome') or r.get('state')}" for r in ch]
    try:
        lines = cm.AUDIT.read_text().splitlines()[-400:]
    except OSError:
        lines = []
    recent = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("host") == p.get("host") and r.get("event") not in ("hygiene", "job-detached"):
            recent.append(cm.audit_line(r))
    if recent:
        out.append("the audit trail's last records for that machine:")
        out += ["  " + x for x in recent[-10:]]
    from noctyx import agent
    return agent.redact("\n".join(out))[:14000]


def diagnose(c, p, repairs=(), run=_run, ask=None):
    """-> {"text", "model", "ts", "of"} or None when the assistant is off or did not answer."""
    a = llm.configured()
    if not a:
        return None
    text, model = (ask or llm.ask_any)(a, SYSTEM, evidence(c, p, repairs, run), max_tokens=320)
    if not text:
        return None
    return {"text": text.strip()[:1500], "model": model, "ts": time.time(), "of": _of(p)}


def _of(p):
    return f"{p.get('what')}|{p.get('detail') or ''}"[:400]


def attach(c, problems, old, repairs=(), log=print, note=None, run=_run, ask=None, budget=MAX_PER_RUN):
    """Give every needs-a-person problem its diagnosis: the one it had while it says the same thing and is under a
    day old, else a new one (at most `budget` per call). Returns how many were asked for."""
    if os.environ.get("NOCTYX_NO_ASSIST"):
        return 0
    asked = 0
    for p in problems:
        if not p.get("needs_person") or p.get("check") == "heal":
            continue
        had = (old.get(p["key"]) or {}).get("diagnosis")
        if had and had.get("of") == _of(p) and time.time() - had.get("ts", 0) < AGAIN_AFTER:
            p["diagnosis"] = had
            continue
        if asked >= budget:
            if had:
                p["diagnosis"] = had
            continue
        asked += 1
        try:
            d = diagnose(c, p, repairs, run, ask)
        except Exception as e:
            d = None
            log(f"  (no diagnosis: {str(e)[:100]})")
        if d:
            p["diagnosis"] = d
            log(f"  diagnosis ({d['model']}) of {p['host']}: {p['what']}")
            for line in d["text"].splitlines():
                log(f"    {line}")
            if note:
                note("diagnosis", d["text"], p.get("host"), model=d["model"], check=p.get("check"))
        elif had:
            p["diagnosis"] = had
    return asked
