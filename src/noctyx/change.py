"""Every change Noctyx makes to a machine checks itself and can undo itself.

A change is one transaction: preflight (nothing changed yet: refuse with the reason), snapshot (what it
replaces, kept where a rollback finds it), apply, verify (the thing works, measured the way it is used), and
on any failure — a bad verify, an error, a timeout, a SIGTERM from the console's watchdog — rollback, then a
check that the rollback worked. Each outcome goes to the ledger (state/changes.jsonl) and the audit trail, so
`noctyx heal` can still undo a change that breaks later, and the console, the brief and the assistant can say
what happened.

A rollout over several machines changes one first (the canary), watches it for a while, then the rest one at
a time, and stops at the first failure: the machines after it are left as they were.
"""
import datetime
import fcntl
import json
import os
import pathlib
import secrets
import signal
import threading
import time

from noctyx.paths import P

LEDGER = pathlib.Path(P["state"]) / "changes.jsonl"
LOCK = pathlib.Path(P["locks"]) / "change.lock"


class Refused(Exception):
    """Preflight said no: nothing was changed."""


class Interrupted(Exception):
    """SIGTERM while a change ran: roll back before exiting."""


class Change:
    """One change on one machine. Subclasses set kind/host/component/what and implement the steps; self.snap
    holds what the snapshot kept (JSON), so a later rollback (`noctyx heal`) can be rebuilt from the ledger."""
    kind = "change"
    auto_revert = False          # may `noctyx heal` undo it on its own when it breaks later (cheap, no data lost)
    timeout = 900                # apply's own budget, seconds

    def __init__(self, c, host, snap=None):
        self.c, self.host, self.snap = c, host, snap or {}
        self.component = self.kind
        self.what = self.kind

    def preflight(self):
        """Raise Refused(why) when the change must not start."""

    def snapshot(self):
        """Keep what apply replaces (fill self.snap)."""

    def apply(self):
        raise NotImplementedError

    def verify(self):
        """-> [problem, …]; empty when the change works."""
        return []

    def rollback(self):
        raise NotImplementedError

    def verify_rollback(self):
        return self.verify_before()

    def verify_before(self):
        """What must hold again after a rollback: by default, what held before the change."""
        return []


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def record(rec):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    os.chmod(LEDGER, 0o644)


def ledger(limit=200):
    try:
        lines = LEDGER.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def last_change(host, component, within=86400, outcome="ok"):
    """The newest change to host/component in the last `within` seconds that ended `outcome` (None if none),
    unless a later one (a rollback by heal, say) superseded it."""
    for r in reversed(ledger()):
        if r.get("host") == host and r.get("component") == component:
            if r.get("outcome") == outcome and time.time() - r.get("ts_epoch", 0) < within:
                return r
            return None
    return None


class _held:
    """One change at a time on this head (a CLI run and a console job, or heal and an upgrade)."""

    def __enter__(self):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise Refused("another change is running on this head (noctyx upgrade or noctyx heal); try again when it ends")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def busy():
    """Is a change running right now? (heal skips its repairs while one does)"""
    try:
        with open(LOCK, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def _on_term(*_):
    raise Interrupted("stopped (SIGTERM)")


def _say(m):
    print(m, flush=True)


def run(ch, log=_say, by=None, locked=True):
    """Run one change as a transaction. -> the ledger record: outcome ok | refused | rolled_back |
    rollback_failed, with the problems found."""
    rec = {"id": secrets.token_hex(4), "ts": _utc(), "ts_epoch": time.time(), "kind": ch.kind, "host": ch.host["name"],
           "component": ch.component, "what": ch.what, **({"by": by} if by else {})}
    old = signal.signal(signal.SIGTERM, _on_term) if threading.current_thread() is threading.main_thread() else None
    lock = _held() if locked else None
    try:
        try:
            if lock:
                lock.__enter__()
            log(f"· checking first ({ch.what})")
            ch.preflight()
        except Refused as e:
            rec.update(outcome="refused", problems=[str(e)])
            log(f"✗ refused, nothing changed: {e}")
            return _done(rec, ch, log)
        problems = []
        changed = False
        try:
            log("· keeping what it replaces")
            ch.snapshot()
            changed = True
            log("· changing")
            ch.apply()
            log("· checking the result")
            problems = ch.verify()
        except (Exception, Interrupted) as e:
            problems = [str(e) if isinstance(e, (RuntimeError, Interrupted)) else f"{type(e).__name__}: {e}"]
        if not problems:
            rec.update(outcome="ok", problems=[])
            log(f"✓ {ch.what}: works")
            return _done(rec, ch, log)
        for p in problems:
            log(f"  ✗ {p}")
        rec["problems"] = problems
        if not changed:
            rec["outcome"] = "refused"
            log("✗ failed before anything was changed")
            return _done(rec, ch, log)
        log("· rolling back")
        if old is not None:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)     # a second SIGTERM must not cut the rollback short
        try:
            ch.rollback()
            again = ch.verify_rollback()
        except (Exception, Interrupted) as e:
            again = [f"rollback: {e}"]
        rec["outcome"] = "rollback_failed" if again else "rolled_back"
        rec["after_rollback"] = again
        log(f"✗ rolled back: {ch.host['name']} is as it was" if not again
            else "✗✗ the rollback did not restore it: " + "; ".join(again))
        return _done(rec, ch, log)
    finally:
        if lock and getattr(lock, "fh", None) and not lock.fh.closed:
            lock.__exit__()
        if old is not None:
            signal.signal(signal.SIGTERM, old)


def _done(rec, ch, log):
    rec["snap"] = ch.snap
    record(rec)
    try:
        from noctyx import orchestrator as cm
        cm.audit("change", **{k: v for k, v in rec.items() if k not in ("snap", "ts", "ts_epoch")})
    except Exception as e:                      # the ledger has it; the audit line is a copy
        log(f"  (audit not written: {e})")
    return rec


def rollout(changes, soak=300, log=_say, by=None, sleep=time.sleep):
    """Change one machine first, watch it `soak` seconds (verify again), then the rest one at a time; stop at
    the first failure. -> {"results": [rec…], "stopped": why or None, "untouched": [host…]}"""
    results, stopped = [], None
    for i, ch in enumerate(changes):
        log(f"\n== {ch.host['name']} ({i + 1}/{len(changes)}{', canary' if i == 0 and len(changes) > 1 else ''})")
        rec = run(ch, log=log, by=by)
        results.append(rec)
        if rec["outcome"] != "ok":
            stopped = f"{ch.host['name']}: {rec['outcome']}"
            break
        if i == 0 and len(changes) > 1 and soak:
            log(f"· watching the canary {soak}s before the others")
            sleep(soak)
            late = ch.verify()
            if late:
                for p in late:
                    log(f"  ✗ {p}")
                log("· the canary broke while watched: rolling it back")
                with _held():
                    ch.rollback()
                    again = ch.verify_rollback()
                late_rec = {**rec, "id": secrets.token_hex(4), "ts": _utc(), "ts_epoch": time.time(),
                            "outcome": "rollback_failed" if again else "rolled_back", "problems": late,
                            "after_rollback": again, "note": f"broke within {soak}s of the change"}
                _done(late_rec, ch, log)
                results.append(late_rec)
                stopped = f"{ch.host['name']}: broke while watched"
                break
            log("✓ canary still good")
    untouched = [ch.host["name"] for ch in changes[len({r['host'] for r in results}):]]
    if stopped:
        log(f"\nstopped at {stopped}; not changed: {', '.join(untouched) or 'none'}")
    return {"results": results, "stopped": stopped, "untouched": untouched}
