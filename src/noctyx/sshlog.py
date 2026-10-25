"""SSH logins on one machine, read from its own log when asked: the systemd journal on Linux, the unified log
on a Mac. Nothing is collected ahead (the node collector only counts, for the alerts); this reads the lines,
so each row is a real event: when, accepted or not, which user, from where, how, and with which key.

  accepted  "Accepted <method> for <user> from <addr> port <p> ssh2[: <keytype> <fingerprint>]"
  failed    "Failed <method> for [invalid user ]<user> from <addr> port <p>"
  invalid   "Invalid user <user> from <addr> port <p>"         (a user that does not exist there)
  gave up   "Connection closed by [authenticating|invalid] user <user> <addr> port <p> [preauth]"
            (offered keys were all refused, or the client stopped before authenticating)
  too many  "maximum authentication attempts exceeded for [invalid user ]<user> from <addr> port <p>"

The head's own key (Noctyx's checks, backups and sessions started from the head) logs in constantly; its rows
are marked `head_key` so a view can set them aside.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime

from noctyx.paths import P

DAYS = (1, 7, 30)
MAX_LINES = 60000                     # newest kept when a machine logged more (a brute force at full speed)
_GREP = "Accepted |Failed |Invalid user |Connection closed by (authenticating |invalid )?user |maximum authentication attempts"

_PATTERNS = (
    ("accepted", re.compile(r"Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<addr>\S+) port (?P<port>\d+)"
                            r"(?: ssh2(?:: (?P<keytype>\S+) (?P<fp>\S+))?)?")),
    ("failed", re.compile(r"Failed (?P<method>\S+) for (?P<inv>invalid user )?(?P<user>\S*?) ?from (?P<addr>\S+) port (?P<port>\d+)")),
    ("too many", re.compile(r"maximum authentication attempts exceeded for (?P<inv>invalid user )?(?P<user>\S*?) ?from (?P<addr>\S+) port (?P<port>\d+)")),
    ("invalid", re.compile(r"Invalid user (?P<user>\S*?) ?from (?P<addr>\S+) port (?P<port>\d+)")),
    ("gave up", re.compile(r"Connection closed by (?P<inv>invalid |authenticating )?user (?P<user>\S*?) ?(?P<addr>[0-9a-fA-F.:]+) port (?P<port>\d+) \[preauth\]")),
)


def remote_cmd(days, macos=False):
    """The shell snippet that prints the raw lines, oldest first, at most MAX_LINES."""
    days = int(days)
    if macos:
        pred = 'process == "sshd" OR process == "sshd-session" OR process == "sshd-auth"'
        src = f"/usr/bin/log show --style syslog --info --last {days}d --predicate '{pred}' 2>/dev/null"
    else:
        src = f"journalctl -o short-unix --no-pager -q --since -{days}d _COMM=sshd + _COMM=sshd-session + _COMM=sshd-auth"
    homes = "/Users/*/.ssh" if macos else "/root/.ssh /home/*/.ssh"
    keys = (f"for d in {homes}; do for f in \"$d\"/authorized_keys \"$d\"/authorized_keys2; do [ -r \"$f\" ] && "
            f"ssh-keygen -lf \"$f\" 2>/dev/null | sed \"s|^|@@KEY |\"; done; done")
    return f"{keys}; {src} | grep -E '{_GREP}' | tail -n {MAX_LINES}"


def key_names(text):
    """The machine's authorized keys: SHA256 fingerprint -> the comment it was added with (who it is)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^@@KEY \d+ (SHA256:\S+) (.*?)(?: \(\w+\))?$", line)
        if m:
            out.setdefault(m[1], m[2].strip() if m[2].strip() not in ("", "no comment") else "")
    return out


_MAC_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(\.\d+)?([+-]\d{4})\s")


def _line_ts(line):
    """-> (epoch seconds, the rest of the line) for a journal short-unix or a macOS syslog-style line."""
    m = re.match(r"^(\d{9,11}\.\d+)\s", line)
    if m:
        return float(m[1]), line[m.end():]
    m = _MAC_TS.match(line)
    if m:
        t = datetime.strptime(m[1] + m[3], "%Y-%m-%d %H:%M:%S%z").timestamp() + float(m[2] or 0)
        return t, line[m.end():]
    return None, line


def parse(text, head_fp=None):
    """Raw log lines -> events, oldest first. A line no pattern knows is skipped."""
    out, names = [], key_names(text)
    for line in text.splitlines():
        ts, rest = _line_ts(line)
        if ts is None:
            continue
        for result, rx in _PATTERNS:
            m = rx.search(rest)
            if not m:
                continue
            g = m.groupdict()
            e = {"ts": round(ts, 3), "result": result, "user": g.get("user") or "", "addr": g["addr"],
                 "port": int(g["port"]), "method": g.get("method") or ""}
            if g.get("inv") and g["inv"].strip().startswith("invalid"):
                e["invalid_user"] = True
            if g.get("fp"):
                e["key"] = f"{g['keytype']} {g['fp']}"
                if names.get(g["fp"]):
                    e["key_name"] = names[g["fp"]]
                if head_fp and g["fp"] == head_fp:
                    e["head_key"] = True
            out.append(e)
            break
    # a refused attempt logs "Invalid user" and then "Failed ... invalid user" for one connection: keep the
    # Failed line (it says how), drop its Invalid twin
    fails = {(e["addr"], e["port"]) for e in out if e["result"] in ("failed", "gave up", "too many")}
    return [e for e in out if not (e["result"] == "invalid" and (e["addr"], e["port"]) in fails)]


def summary(events):
    """Counts, and one row per source address: what it tried, when it was last seen, which users."""
    tot = {"accepted": 0, "refused": 0, "head_key": 0}
    src = {}
    for e in events:
        ok = e["result"] == "accepted"
        tot["accepted" if ok else "refused"] += 1
        tot["head_key"] += bool(e.get("head_key"))
        s = src.setdefault(e["addr"], {"addr": e["addr"], "accepted": 0, "refused": 0, "first": e["ts"], "last": e["ts"],
                                       "users": {}, "head_key": 0})
        s["accepted" if ok else "refused"] += 1
        s["head_key"] += bool(e.get("head_key"))
        s["last"] = max(s["last"], e["ts"])
        s["first"] = min(s["first"], e["ts"])
        s["users"][e["user"]] = s["users"].get(e["user"], 0) + 1
    rows = sorted(src.values(), key=lambda s: (-s["refused"], -s["last"]))
    for s in rows:
        s["users"] = sorted(s["users"], key=lambda u: -s["users"][u])[:8]
    return {**tot, "sources": rows}


_FP = {}


def head_fingerprint(c):
    """The SHA256 fingerprint of the key Noctyx logs in with (the site's server.ssh.key), cached."""
    key = c["server"]["ssh"]["key"]
    if key not in _FP:
        try:
            r = subprocess.run(["ssh-keygen", "-lf", key], capture_output=True, text=True, timeout=10)
            m = re.search(r"(SHA256:\S+)", r.stdout or "")
            _FP[key] = m[1] if m else None
        except Exception:
            _FP[key] = None
    return _FP[key]


def read(c, h, days=7, timeout=150):
    """-> {host, days, ts, events, summary} or {error} for host h, straight from its log."""
    from noctyx import orchestrator as cm
    if int(days) not in DAYS:
        raise ValueError(f"days: one of {', '.join(map(str, DAYS))}")
    t0 = time.time()
    try:
        r = cm._on(c, h, remote_cmd(days, h.get("os") == "macos"), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"host": h["name"], "days": int(days), "error": f"reading the log took over {timeout}s"}
    # grep exits 1 when nothing matched: an empty log, not an error
    if r.returncode not in (0, 1) and not (r.stdout or "").strip():
        return {"host": h["name"], "days": int(days), "error": (r.stderr or f"exit {r.returncode}").strip()[-300:]}
    ev = parse(r.stdout or "", head_fingerprint(c))
    return {"host": h["name"], "days": int(days), "ts": time.time(), "seconds": round(time.time() - t0, 1),
            "truncated": len((r.stdout or "").splitlines()) >= MAX_LINES, "head_fp": head_fingerprint(c),
            "events": ev, "summary": summary(ev)}


def cached(c, h, days=7, max_age=120):
    """read(), kept for max_age seconds in the console's state (a Mac's log takes seconds to search)."""
    import pathlib
    d = pathlib.Path(P["console_state"]) / "ssh-log"
    f = d / f"{h['name']}-{int(days)}d.json"
    try:
        v = json.loads(f.read_text())
        if time.time() - v.get("ts", 0) < max_age:
            return v
    except Exception:
        pass
    v = read(c, h, days)
    if "error" not in v:
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:   # who logged in: root's
                fh.write(json.dumps(v))
            tmp.replace(f)
        except OSError:
            pass
    return v


def cmd(a):
    """noctyx ssh-log --host H [--days 1|7|30] [--refused] [--all] [--json]: SSH logins on a machine, newest first."""
    from noctyx import orchestrator as cm
    c = cm.cfg()
    h = next((x for x in c["hosts"] if x["name"] == a.host), None)
    if not h:
        raise SystemExit(f"no host {a.host}")
    v = read(c, h, a.days)
    if "error" in v:
        raise SystemExit(f"{a.host}: {v['error']}")
    ev = [e for e in v["events"] if (a.all or not e.get("head_key")) and (not a.refused or e["result"] != "accepted")]
    if a.json:
        print(json.dumps({**v, "events": ev}, indent=1))
        return
    s = v["summary"]
    print(f"{a.host}, last {a.days}d: {s['accepted']} accepted, {s['refused']} refused"
          + (f" ({s['head_key']} by the head's key{', shown' if a.all else ', hidden: --all shows them'})" if s["head_key"] else "")
          + (f"; only the newest {MAX_LINES} lines were read" if v["truncated"] else ""))
    if not ev:
        print("  no logins to show")
        return
    print(f"  {'when (UTC)':19}  {'result':9} {'user':24} {'from':40} how")
    for e in reversed(ev[-(len(ev) if a.limit == 0 else a.limit):]):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts"]))
        key = e.get("key_name") or (e["key"].split(" ")[-1][:20] + "…" if e.get("key") else "")
        how = e["method"] + (f" {key}" if key else "") + (" (head's key)" if e.get("head_key") else "")
        user = e["user"] + (" (no such user)" if e.get("invalid_user") or e["result"] == "invalid" else "")
        print(f"  {when}  {e['result']:9} {user[:24]:24} {e['addr'] + ':' + str(e['port']):40} {how}")
    if a.limit and len(ev) > a.limit:
        print(f"  … {len(ev) - a.limit} older (--limit 0 shows all)")
