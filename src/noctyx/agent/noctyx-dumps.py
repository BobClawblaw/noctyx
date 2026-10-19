#!/usr/bin/env python3
"""Noctyx database dumps — consistent copies of live databases, taken just
before a backup so the snapshot holds a dump instead of (only) a torn data dir.

  noctyx-dumps hook PROFILE   pre-backup hook (pre.d/10-db-dumps.sh); dumps when
                                  PROFILE is in the config's "profiles" (default: system)
  noctyx-dumps now            dump everything enabled, regardless of profile
  noctyx-dumps list           JSON: what was discovered + the last run's status

Discovery, no per-database setup:
  docker  postgres (pg_dumpall as POSTGRES_USER), pg0-embedded postgres (credentials
          from the container's own ~/.pg0 instance.json), mariadb/mysql (root via the
          container's *_ROOT_PASSWORD, passed as MYSQL_PWD, never on a command line),
          redis/valkey with a volume (SAVE, so the .rdb in the volume is current)
  host    mysql/mariadb and postgresql services (socket auth as root / postgres)
  sqlite  gitea, grafana, plus any paths in the config (online backup API)

Dumps land in <node_lib>/dumps (0700 root), which is inside the `system`
profile. Written to a temp file and renamed, so a failed dump never replaces the last
good one. Config: <node_etc>/db-dumps.json, written by `noctyx policy-apply`.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

CONF = os.environ.get("NOCTYX_DUMPS_CONF", "@NODE_ETC@/db-dumps.json")
DIR = os.environ.get("NOCTYX_DUMPS_DIR", "@NODE_LIB@/dumps")
STATUS = os.path.join(DIR, "status.json")
TIMEOUT = 1800
KNOWN_SQLITE = ["/var/lib/gitea/data/gitea.db", "/var/lib/grafana/grafana.db"]


def conf():
    try:
        c = json.load(open(CONF))
    except Exception:
        c = {}
    return {"enabled": c.get("enabled", True), "skip": set(c.get("skip") or []),
            "sqlite": list(c.get("sqlite") or []), "profiles": c.get("profiles") or ["system"]}


def sh(argv, timeout=30):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _active(unit):
    return sh(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def discover(c):
    found = []
    if sh(["docker", "version", "--format", "x"]).returncode == 0:
        ps = sh(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"]).stdout.splitlines()
        for line in ps:
            name, _, image = line.partition("\t")
            top = sh(["docker", "top", name, "-eo", "pid,args"]).stdout
            mounts = sh(["docker", "inspect", "-f", "{{len .Mounts}}", name]).stdout.strip()
            kind = None
            # `docker top` lines are "PID ARGS"; postgres children are "postgres: walwriter" etc.
            if re.search(r"[\s/](postgres|postmaster)(\s|$)", top):
                pg0 = sh(["docker", "exec", name, "sh", "-c", 'ls "$HOME"/.pg0/instances/*/instance.json 2>/dev/null'])
                kind = "pg0" if pg0.stdout.strip() else "postgres"
            elif re.search(r"[\s/](mariadbd|mysqld)(\s|$)", top):
                kind = "mysql"
            elif re.search(r"[\s/](redis-server|valkey-server)(\s|$)", top) and mounts not in ("", "0"):
                kind = "redis"
            if kind:
                found.append({"id": f"docker:{name}", "kind": kind, "label": f"{name} ({image})"})
    for unit, kind in (("mysql", "host-mysql"), ("mariadb", "host-mysql"), ("postgresql", "host-postgres")):
        if _active(unit) and not any(f["kind"] == kind for f in found):
            found.append({"id": f"host:{unit}", "kind": kind, "label": f"{unit}.service"})
    for p in dict.fromkeys(KNOWN_SQLITE + c["sqlite"]):
        if os.path.isfile(p):
            found.append({"id": f"sqlite:{p}", "kind": "sqlite", "label": p})
    for f in found:
        f["enabled"] = f["id"] not in c["skip"]
    return found


PG0 = r'''f=$(ls "$HOME"/.pg0/instances/*/instance.json | head -1); j=$(tr -d '\n' < "$f")
v() { printf %s "$j" | sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\).*/\1/p"; }
PGPASSWORD=$(v password) exec "$(v installation_dir)/$(v version)/bin/pg_dumpall" -h 127.0.0.1 -p "$(v port)" -U "$(v username)"'''
MYSQL = ('MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" exec $(command -v mariadb-dump || command -v mysqldump) '
         '-uroot --all-databases --single-transaction --routines --events')


def command(d):
    n = d["id"].split(":", 1)[1]
    return {
        "postgres": ["docker", "exec", n, "sh", "-c", 'exec pg_dumpall -U "${POSTGRES_USER:-postgres}"'],
        "pg0": ["docker", "exec", n, "sh", "-c", PG0],
        "mysql": ["docker", "exec", n, "sh", "-c", MYSQL],
        "redis": ["docker", "exec", n, "sh", "-c", "$(command -v valkey-cli || command -v redis-cli) SAVE"],
        "host-mysql": ["mysqldump", "--all-databases", "--single-transaction", "--routines", "--events"],
        "host-postgres": ["runuser", "-u", "postgres", "--", "pg_dumpall"],
    }.get(d["kind"])


def fname(d):
    kind, _, name = d["id"].partition(":")
    base = kind + "-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return base if d["kind"] == "sqlite" and base.endswith(".db") else base + (".db" if d["kind"] == "sqlite" else ".sql")


def dump_one(d):
    t0, out = time.time(), os.path.join(DIR, fname(d))
    tmp = out + ".partial"
    r = {"id": d["id"], "kind": d["kind"], "ok": False, "bytes": 0, "seconds": 0, "error": None}
    try:
        if d["kind"] == "sqlite":
            src = sqlite3.connect(f"file:{d['label']}?mode=ro", uri=True, timeout=60)
            dst = sqlite3.connect(tmp)
            with dst:
                src.backup(dst)
            src.close(); dst.close()
        elif d["kind"] == "redis":
            p = subprocess.run(command(d), capture_output=True, text=True, timeout=TIMEOUT)
            if p.returncode != 0 or "OK" not in p.stdout:
                raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f"rc={p.returncode}")
            r.update(ok=True, file=None, note="SAVE: the .rdb in the volume is current")
            return r
        else:
            with open(tmp, "wb") as f:
                p = subprocess.run(command(d), stdout=f, stderr=subprocess.PIPE, timeout=TIMEOUT)
            if p.returncode != 0:
                raise RuntimeError(p.stderr.decode(errors="replace").strip()[-300:] or f"rc={p.returncode}")
            if os.path.getsize(tmp) == 0:
                raise RuntimeError("empty dump")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
        r.update(ok=True, bytes=os.path.getsize(out), file=out)
    except Exception as e:
        r["error"] = str(e)[:300]
        try:
            os.unlink(tmp)
        except OSError:
            pass
    finally:
        r["seconds"] = round(time.time() - t0, 1)
    return r


def run(c):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    found = discover(c)
    results = []
    for d in found:
        if not d["enabled"]:
            results.append({"id": d["id"], "kind": d["kind"], "ok": None, "skipped": True})
            continue
        r = dump_one(d)
        results.append(r)
        print(f"  dump {d['id']}: " + (f"ok {r['bytes'] / 1e6:.1f} MB in {r['seconds']}s" if r["ok"]
                                         else f"FAILED {r['error']}"), file=sys.stderr)
    # a dump whose source is gone stays for 30 days (it is in the snapshots anyway), then goes
    keep = {fname(d) for d in found} | {"status.json"}
    for n in os.listdir(DIR):
        p = os.path.join(DIR, n)
        if n not in keep and os.path.isfile(p) and time.time() - os.path.getmtime(p) > 30 * 86400:
            os.unlink(p)
    st = {"ts": time.time(), "results": results}
    with open(STATUS + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATUS + ".tmp", STATUS)
    return st


def main():
    c = conf()
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    if mode == "list":
        try:
            last = json.load(open(STATUS))
        except Exception:
            last = None
        print(json.dumps({"enabled": c["enabled"], "discovered": discover(c), "last": last}))
        return 0
    if mode == "hook":
        prof = sys.argv[2] if len(sys.argv) > 2 else "system"
        if not c["enabled"] or prof not in c["profiles"]:
            return 0
    elif mode != "now":
        print(__doc__, file=sys.stderr)
        return 2
    st = run(c)
    bad = [r for r in st["results"] if r.get("ok") is False]
    return 1 if bad and mode == "now" else 0   # as a hook: never block the backup


if __name__ == "__main__":
    sys.exit(main())
