"""What is new in the fleet's logs: each night, every Linux node's warnings and errors of the last day, grouped
into kinds, and the kinds no earlier night had.

    noctyx log-digest [--host NAME] [--json]

A node logs thousands of warnings a day, nearly all of them the same few lines it logged yesterday. What deserves
a look is the line that was never there before. So each node counts its own journal (priority warning and worse,
the last 24 hours) by kind: the unit and the message with what varies taken out (numbers, hex, addresses, ids,
interface names). The head keeps which kinds each host has shown and when (<state>/log-kinds.json, 14 days), and
a kind first seen tonight is new. The first night on a host only records: everything would be new.

The site's assistant, when one is set up, reads the new kinds (redacted) and says in a few lines which matter.
The digest is kept in the console's state (log-digest.json): `noctyx log-digest` prints it, Ask reads it (an
operator's tool, like every log), and the morning brief is told how many new kinds each host has, not their text.
"""
import json
import os
import pathlib
import re
import time

from noctyx.paths import P

KINDS = pathlib.Path(P["state"]) / "log-kinds.json"
DIGEST = pathlib.Path(P["console_state"]) / "log-digest.json"
KEEP_DAYS = 14
TOP = 300                 # kinds a node reports (its most frequent)
SHOW = 12                 # new kinds per host given to the assistant and printed
# the node's own count: what varies is taken out, so one kind of line is one line however often it came
SNIPPET = r"""journalctl -p 4 --since "-24h" --no-pager -o short-iso 2>/dev/null \
 | awk '{$1="";$2="";print}' \
 | sed -E 's/\[[0-9]+\]//; s/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}/ID/g; s/veth[0-9a-f]+/vethX/g; s/0x[0-9a-fA-F]+/H/g; s/\b[0-9a-f]{12,}\b/H/g; s/[0-9]+/N/g; s/^ +//' \
 | cut -c1-180 | sort | uniq -c | sort -rn | head -n %d""" % TOP
SYSTEM = """You read a small AI cluster's logs for its operator. Below are the kinds of warning and error lines \
that appeared on each machine in the last day and on no earlier day (numbers and ids are replaced by N, H, ID), \
each with how often it came. In at most 6 short lines say which of them deserve a look and why, most important \
first, naming the machine and the unit; group the ones that are noise in one line. If none matters, say so in one \
line. Use only what is below; do not advise restarting a model server or rebooting a node on a log line alone."""


def _cm():
    from noctyx import orchestrator as cm
    return cm


def parse(text):
    """`uniq -c` output -> {kind: count}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*\S)", line)
        if m and not m.group(2).startswith("-- No entries"):
            out[m.group(2)] = out.get(m.group(2), 0) + int(m.group(1))
    return out


def fold(seen, host, kinds, now=None):
    """Put tonight's kinds of one host into what is known -> the new ones [(kind, count)], most frequent first.
    seen: {host: {kind: {"first", "last"}}}; a host with nothing known yet records and reports none."""
    now = now or time.time()
    known = seen.setdefault(host, {})
    first_night = not known
    new = []
    for k, n in kinds.items():
        if k in known:
            known[k]["last"] = now
        else:
            known[k] = {"first": now, "last": now}
            if not first_night:
                new.append((k, n))
    for k in [k for k, v in known.items() if now - v["last"] > KEEP_DAYS * 86400]:
        del known[k]
    return sorted(new, key=lambda x: -x[1])


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _save(path, d, mode=0o644):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(c, only=None, read=None, ask=None, now=None):
    """Count every Linux node's log kinds, find the new ones, ask the assistant about them -> the digest."""
    from noctyx import agent, llm
    cm = _cm()
    now = now or time.time()
    seen = load(KINDS)
    hosts = {}
    for h in cm.hosts(c):
        if h.get("os", "linux") != "linux" or not (h.get("address") or h.get("local")) or (only and h["name"] != only):
            continue
        try:
            if read:
                text = read(c, h)
            else:
                r = cm._on(c, h, SNIPPET, timeout=120)
                if r.returncode == 255:
                    raise RuntimeError((r.stderr or "ssh failed").strip()[-120:])
                text = r.stdout or ""
            kinds = parse(text)
        except Exception as e:
            hosts[h["name"]] = {"error": str(e)[:120]}
            continue
        first = h["name"] not in seen or not seen[h["name"]]
        new = fold(seen, h["name"], kinds, now)
        hosts[h["name"]] = {"lines": sum(kinds.values()), "kinds": len(kinds), "first_night": first,
                            "new": [{"kind": agent.redact(k), "count": n} for k, n in new[:SHOW]], "new_total": len(new)}
    out = {"ts": now, "hosts": hosts, "summary": None, "model": None}
    fresh = {h: d["new"] for h, d in hosts.items() if d.get("new")}
    a = llm.configured()
    if fresh and a and not os.environ.get("NOCTYX_NO_ASSIST"):
        body = "\n".join(f"{h}:\n" + "\n".join(f"  {x['count']:>6}x  {x['kind']}" for x in rows) for h, rows in fresh.items())
        try:
            text, model = (ask or llm.ask_any)(a, SYSTEM, body[:12000], max_tokens=320)
        except Exception:
            text, model = None, None
        if text:
            out.update(summary=text.strip()[:1500], model=model)
    if not only:
        _save(KINDS, seen)
        _save(DIGEST, out, 0o600)            # log lines: the console (root) reads it, operators see it
    return out


def counts(d=None):
    """{host: new kinds} of the last digest, for the morning brief (numbers only: the text is an operator's)."""
    d = d if d is not None else load(DIGEST)
    return {h: x.get("new_total", 0) for h, x in (d.get("hosts") or {}).items() if x.get("new_total")}


def text(d):
    lines = []
    for h, x in (d.get("hosts") or {}).items():
        if x.get("error"):
            lines.append(f"{h}: could not read its journal ({x['error']})")
            continue
        lines.append(f"{h}: {x['lines']} warning/error line(s) in {x['kinds']} kind(s)"
                     + (" — first night: recorded, nothing to compare with yet" if x.get("first_night")
                        else f", {x['new_total']} new" if x.get("new_total") else ", none new"))
        for n in x.get("new") or []:
            lines.append(f"   {n['count']:>6}x  {n['kind']}")
        if x.get("new_total", 0) > len(x.get("new") or []):
            lines.append(f"          … and {x['new_total'] - len(x['new'])} more")
    if d.get("summary"):
        lines += ["", f"the assistant ({d.get('model')}):"] + ["  " + l for l in d["summary"].splitlines()]
    return "\n".join(lines)


def cmd_log_digest(a):
    cm = _cm()
    c = cm.cfg()
    if a.host:
        cm.host_by_name(c, a.host)
    if a.show:
        d = load(DIGEST)
        if not d:
            raise SystemExit("no digest yet: the nightly run makes one (or: sudo noctyx log-digest)")
    else:
        d = run(c, only=a.host)
        if not a.host:
            cm.audit("log-digest", hosts=len(d["hosts"]), new={h: n for h, n in counts(d).items()},
                     summarised=bool(d.get("summary")))
    print(json.dumps(d, indent=1) if a.json else text(d))

