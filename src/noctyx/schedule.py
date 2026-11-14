"""The schedule's rules, as pure functions: when a host/profile is due, when it will be next, and how healthy
its newest snapshot is against the schedule. The orchestrator's tick and the console both decide with these.

"How often" is policy in the site config `schedule`, evaluated every tick (the tick timer): a host/profile is
DUE when its newest committed snapshot is older than `every`, inside `window`. Driving off the repository (not
the audit log, not a fixed time) is what makes this self-healing: a skipped (busy), failed or killed run is
simply still due on the next tick, and a snapshot that landed without an audit record still counts.
"""
import datetime
import re

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_DUR_MULT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r} (use e.g. 30m, 6h, 1d)")
    return float(m.group(1)) * _DUR_MULT[m.group(2)]


def fmt_duration(s):
    s = int(max(0, s))
    return f"{s // 86400}d{s % 86400 // 3600:02d}h" if s >= 86400 else \
           f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def in_window(local_dt, window):
    """window 'HH:MM-HH:MM' in head-node local time; may wrap midnight."""
    if not window:
        return True
    a, b = [datetime.time.fromisoformat(x.strip()) for x in window.split("-")]
    t = local_dt.time()
    return a <= t < b if a <= b else (t >= a or t < b)


def effective_schedule(c, host):
    """defaults merged with hosts.<host>; a profile set to null is disabled."""
    sch = c.get("schedule") or {}
    out = {p: dict(r) for p, r in (sch.get("defaults") or {}).items() if r}
    for p, r in ((sch.get("hosts") or {}).get(host) or {}).items():
        if r is None:
            out.pop(p, None)
        else:
            out[p] = {**out.get(p, {}), **r}
    return {p: {"every": parse_duration(r["every"]), "window": r.get("window")}
            for p, r in out.items() if not p.startswith("_")}


def due(now, local_dt, last, every, window, failed_at=None, retry=3600, slack=0):
    """Pure decision: (is_due, reason). `last` = newest snapshot epoch or None;
    `failed_at` = epoch of the last FAILED attempt newer than `last`."""
    if not in_window(local_dt, window):
        return False, f"outside window {window}"
    if failed_at and now - failed_at < retry:
        return False, f"failed {fmt_duration(now - failed_at)} ago, retry in {fmt_duration(retry - (now - failed_at))}"
    if last is None:
        return True, "never backed up"
    age = now - last
    if window and every >= 86400:
        # calendar semantics: "every 1d in 01:00-07:00" means once per night,
        # not "24h after whenever the last one happened to run" — a daytime
        # manual run must not push the nightly out by a whole extra night
        cutoff = _window_start(local_dt, window).timestamp() - (every - 86400)
        if last < cutoff:
            return True, f"no snapshot this window (last {fmt_duration(age)} ago)"
        return False, "already done this window"
    if age + slack >= every:
        return True, f"age {fmt_duration(age)} ≥ every {fmt_duration(every)}"
    return False, f"next in {fmt_duration(every - age - slack)}"


def _window_start(local_dt, window):
    """Most recent opening of `window` at or before local_dt."""
    a = datetime.time.fromisoformat(window.split("-")[0].strip())
    start = local_dt.replace(hour=a.hour, minute=a.minute, second=0, microsecond=0)
    return start if start <= local_dt else start - datetime.timedelta(days=1)


def next_due(now, last, every, window, failed_at=None, retry=3600, slack=0):
    """Earliest epoch at which due() turns true (ignoring the tick grid)."""
    earliest = max(now, (failed_at + retry) if failed_at else now)
    if window and every >= 86400:
        span = 86400 - window_gap(window)
        w = _window_start(datetime.datetime.fromtimestamp(earliest), window)
        for _ in range(400):
            wt = w.timestamp()
            if (last is None or last < wt - (every - 86400)) and earliest < wt + span:
                return max(wt, earliest)
            w += datetime.timedelta(days=1)
        return float("inf")
    t = earliest if last is None else max(earliest, last + every - slack)
    if window:
        dt = datetime.datetime.fromtimestamp(t)
        if not in_window(dt, window):
            a = datetime.time.fromisoformat(window.split("-")[0].strip())
            start = dt.replace(hour=a.hour, minute=a.minute, second=0, microsecond=0)
            if start <= dt:
                start += datetime.timedelta(days=1)
            t = start.timestamp()
    return t


def window_gap(window):
    """Seconds per day OUTSIDE the window — how late a due backup can be made
    to wait by the window alone, which is not a fault."""
    if not window:
        return 0
    a, b = [datetime.time.fromisoformat(x.strip()) for x in window.split("-")]
    mins = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    return 86400 - (mins % 1440) * 60


def health(now, last, every, window, slack=0):
    """ok | late | stale | never — RPO against the schedule, not a raw age."""
    if last is None:
        return "never"
    age = now - last
    grace = window_gap(window) + slack
    return "ok" if age <= every + grace else "late" if age <= 2 * every + grace else "stale"
