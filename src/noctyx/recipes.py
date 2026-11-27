"""Recipe deployment: GPU slots, catalog validation, prepare/switch/rollback, adopt, GPU lock inspection,
and support for spark, systemd (setup.sh + serve.sh), script (start.sh + stop.sh), and recipe.toml [deploy] spec."""
from __future__ import annotations

import base64
import datetime
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

from noctyx.paths import P

RECIPE_KINDS = ("spark", "systemd", "script", "spec")   # spark | setup+serve | start+stop | recipe.toml [deploy]
_RNAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,47}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^(https://|ssh://)[A-Za-z0-9@._:/-]+$")
_ENVK = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,120}\.service$")
PREBUILD_MIN_GIB = 40      # a pair head builds the next image while serving only with this much free


def _cm():
    from noctyx import orchestrator as cm
    return cm


def recipes_cfg(c):
    r = c.get("recipes") or {}
    return {"root": r.get("root") or P["recipe_root"], "hf_mirror": r.get("hf_mirror", True),
            "slots": r.get("slots") or {}, "catalog": r.get("catalog") or {}}


def recipe_slot(c, name):
    s = recipes_cfg(c)["slots"].get(name)
    if not s:
        sys.exit(f"unknown slot: {name} (slots: {', '.join(recipes_cfg(c)['slots']) or 'none'})")
    return s


def slot_deploys(s):
    """False for a watch-only slot ("deploy": false): Noctyx shows what it serves and says when that changes,
    and never deploys to it, stops it or starts it (a server it has no unit for, a Mac's launchd job)."""
    return s.get("deploy", True) is not False


def _recipe_state():
    cm = _cm()
    try:
        return json.loads(cm.RECIPE_STATE.read_text())
    except Exception:
        return {"slots": {}}


def _save_recipe_state(st):
    cm = _cm()
    cm.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cm.RECIPE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, cm.RECIPE_STATE)


def _adopted(s):
    a = s.get("adopt") or {}
    if not a.get("unit") and not a.get("models"):
        return None
    return {"recipe": None, "name": a.get("name") or a["unit"], "adopted": True, "unit": a.get("unit"),
            "spark_dir": a.get("spark_dir"), **({"models": list(a["models"])} if a.get("models") else {})}


def slot_records(c, slot):
    """(current, previous, history) of a slot; before any deploy, current is the adopted unit."""
    st = _recipe_state()["slots"].get(slot) or {}
    return st.get("current") or _adopted(recipe_slot(c, slot)), st.get("previous"), st.get("history") or []


def _record(slot, current=None, previous=None, event=None, keep_previous=False):
    st = _recipe_state()
    s = st["slots"].setdefault(slot, {})
    if current is not None:
        if not keep_previous:
            s["previous"] = s.get("current") or previous
        s["current"] = current
    if event:
        s["history"] = ([{"ts": time.time(), **event}] + (s.get("history") or []))[:30]
    _save_recipe_state(st)


def rec_label(r):
    if not r:
        return "nothing"
    return r.get("name") if r.get("adopted") else f"{r['recipe']}@{r['sha'][:12]}"


def recipe_live(c, slot, timeout=3):
    """What the slot's port answers right now, asked from here: {ok, models, error}."""
    cm = _cm()
    s = recipe_slot(c, slot)
    h = cm.host_by_name(c, s["head"])
    base = f"http://{cm.addr(h)}:{s['port']}"
    out = {"ok": False, "models": [], "url": base}
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            hj = json.loads(r.read() or b"{}")
        out["status"] = hj.get("status")
        with urllib.request.urlopen(base + "/v1/models", timeout=timeout) as r:
            out["models"] = [m.get("id") for m in json.loads(r.read()).get("data", []) if m.get("id")]
        out["ok"] = hj.get("status", "ok") == "ok" and bool(out["models"])
        for k in ("max_batch_size", "warming"):
            if k in hj:
                out[k] = hj[k]
        if isinstance(hj.get("live"), dict):
            out["connections"] = hj["live"].get("connections")
    except Exception as e:
        out["error"] = str(getattr(e, "reason", e))[:120]
    return out


def _slot_user(c, h):
    return h.get("ssh_user") or c["server"]["ssh"]["user"]


def _root_stream(c, h, script):
    """A bash script as root on h, fed on stdin (nothing secret or quoted on a command line),
    its output streamed into ours. Returns (rc, output)."""
    cm = _cm()
    if cm.is_head(c, h):
        argv = ["bash", "-s"]
    else:
        s_ = c["server"]["ssh"]
        argv = ["ssh", *cm.SSH_OPTS, "-i", s_["key"], f"{_slot_user(c, h)}@{cm.addr(h)}", "sudo -n bash -s"]
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdin.write(script.encode())
    p.stdin.close()
    out = []
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").replace("\r", "\n")
        out.append(line)
        if len(out) > 4000:
            out = out[-2000:]
        sys.stdout.write("  " + line if not line.startswith("NX_") else "")
        sys.stdout.flush()
    return p.wait(), "".join(out)


def _home(c, h, user):
    cm = _cm()
    r = cm._run_on(c, h, f"getent passwd {shlex.quote(user)}", timeout=30)
    home = (r.stdout or "").strip().split(":")[5:6]
    if r.returncode != 0 or not home or not home[0].startswith("/"):
        raise RuntimeError(f"cannot find {user}'s home on {h['name']}: {(r.stderr or '').strip()[:120]}")
    return home[0]


def _tilde(p, home):
    return home + p[1:] if p == "~" or p.startswith("~/") else p


def _resolve_ref(repo, ref):
    """A branch, tag or full sha -> the commit sha (git ls-remote from the head node)."""
    if _SHA.match(ref):
        return ref
    r = subprocess.run(["git", "ls-remote", repo, ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
                       capture_output=True, text=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    refs = dict(reversed(l.split("\t", 1)) for l in (r.stdout or "").splitlines() if "\t" in l)
    for k in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}", ref):
        if k in refs:
            return refs[k]
    raise RuntimeError(f"{repo}: no branch or tag {ref!r}" + (f" ({r.stderr.strip()[:160]})" if r.stderr.strip() else ""))


def _sh_env(env):
    return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())


def _toml(sections):
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return json.dumps(str(v))
    out = ["# written by `noctyx recipe-deploy` (slot settings + catalog overrides); redeploy to change"]
    for sec, kv in sections.items():
        out.append(f"\n[{sec}]")
        out += [f"{k} = {val(v)}" for k, v in kv.items()]
    return "\n".join(out) + "\n"


def health_timeout(s):
    """How long a slot's new deployment may take to answer before the switch rolls back (health_timeout_s).
    Its unit's start timeout is this plus 5 minutes, so systemd never gives up first."""
    return int(s.get("health_timeout_s", 1800))


def _unit_text(rec, slot, s, env):
    d, user = rec["dir"], rec["user"]
    envl = "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
    if rec["kind"] == "spec":
        sp = rec.get("spec") or {}
        t_start = max(health_timeout(s), int(sp.get("start_timeout_s") or 0)) + 300
        start_cmd = f"/bin/bash -lc {shlex.quote(sp['start'])}"
        stop_cmd = f"/bin/bash -lc {shlex.quote(sp['stop'])}"
        return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
                f"# `noctyx recipe-deploy` (recipe.toml [deploy] spec v0); rewritten on every switch.\n"
                f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot}, spec)\n"
                f"After=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nUser={user}\nWorkingDirectory={d}\n"
                f"Environment=HOME={rec['home']}\nEnvironment=PATH={rec['home']}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"{envl}ExecStart={start_cmd}\nExecStop={stop_cmd}\n"
                f"TimeoutStartSec={t_start}\nTimeoutStopSec=300\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
    if rec["kind"] == "script":
        return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
                f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
                f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot}, start.sh)\n"
                f"After=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nUser={user}\nWorkingDirectory={d}\n"
                f"Environment=HOME={rec['home']}\nEnvironment=PATH={rec['home']}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"{envl}ExecStart={d}/start.sh\nExecStop={d}/{rec.get('stop') or 'stop.sh'}\n"
                f"TimeoutStartSec={health_timeout(s) + 300}\nTimeoutStopSec=300\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
    if rec["kind"] == "spark":
        return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
                f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
                f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot}, TP pair)\n"
                f"After=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nUser={user}\nWorkingDirectory={d}\n"
                f"Environment=HOME={rec['home']}\nEnvironment=PATH={rec['home']}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"{envl}ExecStart=/usr/bin/python3 -m spark up\nExecStop=/usr/bin/python3 -m spark down\n"
                f"TimeoutStartSec={health_timeout(s) + 300}\nTimeoutStopSec=300\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
    return (f"# Noctyx recipe deployment: {rec['recipe']}@{rec['sha'][:12]} on slot {slot}. Written by\n"
            f"# `noctyx recipe-deploy`; rewritten on every switch, so do not edit by hand.\n"
            f"[Unit]\nDescription=Noctyx recipe {rec['recipe']}@{rec['sha'][:12]} (slot {slot})\n"
            f"After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\nUser={user}\nWorkingDirectory={d}\nEnvironment=HOME={rec['home']}\n"
            f"{envl}ExecStart={d}/serve.sh\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=60\n"
            f"StandardOutput=append:{d}/serve.log\nStandardError=append:{d}/serve.log\n\n"
            f"[Install]\nWantedBy=multi-user.target\n")


def _spark_info(c, h, user, home, d):
    """The settings a spark recipe's checkout resolves to (cluster.toml + recipe.toml), or {}."""
    cm = _cm()
    py = ("import json; from spark import config; s = config.load(); print('NX_INFO ' + json.dumps({"
          "'tf_cache': str(s.tf_cache), 'container': s.container, 'served': getattr(s, 'served_name', None),"
          "'port': s.port}))")
    r = cm._run_on(c, h, f"sudo -u {shlex.quote(user)} -H bash -lc "
                         + shlex.quote(f"cd {shlex.quote(d)} && python3 -c {shlex.quote(py)}"), timeout=60)
    m = re.search(r"^NX_INFO (.+)$", r.stdout or "", re.M)
    return json.loads(m.group(1)) if m else {}


_SPARK_PREP = r'''
import sys
from spark import config, weights
from spark.nodes import Node
s = config.load(); head = Node("head"); worker = Node("worker", s.worker); sync = sys.argv[1] == "1"
if hasattr(weights, "ensure_converted"):
    if s.converted_repo or weights.converted_ready(s, head):
        weights.ensure_converted(s, head)
        if sync and hasattr(weights, "sync_converted"):
            weights.sync_converted(s, head, worker)
    else:
        weights.ensure_snapshot(s)
        print("the conversion needs the GPU: it runs during the switch (`spark up`)")
else:
    weights.ensure_snapshot(s)
    dfl = getattr(s, "drafts", False) and getattr(s, "drafter", "") == "dflash2" and hasattr(weights, "ensure_dflash2")
    if dfl:
        weights.ensure_dflash2(s)
    if sync and hasattr(weights, "sync_snapshot"):
        weights.sync_snapshot(s, head, worker)
        if dfl and hasattr(weights, "sync_dflash2"):
            weights.sync_dflash2(s, head, worker)
print("weights ready")
'''


def recipe_prepare(c, slot, name, ref=None):
    """Check out, configure, build and download — the old deployment keeps serving. Returns the record."""
    cm = _cm()
    rc_ = recipes_cfg(c)
    s = recipe_slot(c, slot)
    cat = rc_["catalog"].get(name)
    if not cat:
        raise RuntimeError(f"unknown recipe: {name} (catalog: {', '.join(rc_['catalog'])})")
    if slot not in (cat.get("slots") or []):
        raise RuntimeError(f"{name} does not run on slot {slot} (its slots: {', '.join(cat.get('slots') or [])})")
    ref = ref or cat.get("ref") or "main"
    sha = cm._resolve_ref(cat["repo"], ref)
    h = cm.host_by_name(c, s["head"])
    user = _slot_user(c, h)
    home = cm._home(c, h, user)
    root = _tilde(s.get("root") or rc_["root"], home)
    d = f"{root}/{name}/{sha[:12]}"
    rec = {"recipe": name, "kind": cat["kind"], "repo": cat["repo"], "ref": ref, "sha": sha, "dir": d,
           "user": user, "home": home, "unit": f"{P['recipe_unit_prefix']}-{slot}.service", "host": h["name"]}
    env = {**(s.get("env") or {}), **(cat.get("env") or {})}
    hfe = {}
    if rc_["hf_mirror"] and (c.get("hf_mirror") or {}).get("port"):
        hfe = {"HF_ENDPOINT": f"http://{c['server']['address']}:{cm.hf_cfg(c)['port']}"}
    files = {}
    if cat["kind"] == "spark":
        sec = {k: dict(v) for k, v in (cat.get("overrides") or {}).items()}
        sec.setdefault("cluster", {}).update({**(s.get("cluster") or {}), "port": s["port"]})
        files["cluster.toml"] = _toml(sec)
        env = {**env, **hfe}
    elif cat["kind"] == "script":
        senv = {**(s.get("script_env") or {}), **(cat.get("env") or {}), "PORT": str(s["port"]), **hfe}
        files[".env"] = "# written by `noctyx recipe-deploy` (slot script_env + catalog env)\n" + \
            "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in senv.items())
        env = {**env, **senv}
    elif cat["kind"] == "spec":
        env = {**(s.get("script_env") or {}), **env, **hfe}
    else:
        env = {"PORT": str(s["port"]), "MODELS": f"{root}/{name}/models", "TF_SRC": f"{d}/engine", **env}
    if cat["kind"] != "spec":
        rec["unit_text"] = _unit_text(rec, slot, s, env)
    print(f"prepare {name}@{sha[:12]} ({ref}) on {h['name']}:{d}", flush=True)

    q = shlex.quote
    put = "".join(f"echo {base64.b64encode(t.encode()).decode()} | base64 -d > {q(d + '/' + f)}\n"
                  for f, t in files.items())
    script = f"""set -uo pipefail
cd /
asu() {{ sudo -u {q(user)} -H env HOME={q(home)} {_sh_env(hfe)} bash -lc "$1"; }}
D={q(d)}
if [ ! -d "$D/.git" ]; then
  echo "clone {cat['repo']} -> $D"
  asu {q(f'rm -rf {q(d)}.partial && mkdir -p {q(os.path.dirname(d))} && git clone -q --filter=blob:none {q(cat["repo"])} {q(d)}.partial && git -C {q(d)}.partial -c advice.detachedHead=false checkout -q {sha} && mv {q(d)}.partial {q(d)}')} || exit 10
fi
[ "$(sudo -u {q(user)} git -C "$D" rev-parse HEAD)" = {sha} ] || {{ echo "checkout $D is not at {sha}"; exit 11; }}
sudo -u {q(user)} bash -c {q(put)} || exit 12
"""
    if cat["kind"] == "spark":
        r1, _ = cm._root_stream(c, h, script + f"asu {q(f'cd {q(d)} && python3 -m spark validate')} || exit 13\n")
        if r1:
            raise RuntimeError(f"checkout/validate failed (rc={r1})")
        info = cm._spark_info(c, h, user, home, d)
        rec["expect"] = [info["served"]] if info.get("served") else []
        cur, _, _ = slot_records(c, slot)
        cd = (cur or {}).get("spark_dir") or ((cur or {}).get("dir") if (cur or {}).get("kind") == "spark" else None)
        cinfo = cm._spark_info(c, h, user, home, _tilde(cd, home)) if cd else {}
        shared = bool(cinfo) and cinfo.get("tf_cache") == info.get("tf_cache")
        mem = cm._run_on(c, h, "awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo", timeout=20)
        free_gib = int((mem.stdout or "0").strip() or 0)
        steps = []
        if free_gib >= PREBUILD_MIN_GIB:
            steps.append(f"asu {q(f'cd {q(d)} && python3 -m spark image')} || exit 14")
        else:
            print(f"  {h['name']} has {free_gib} GiB free (< {PREBUILD_MIN_GIB}): the image builds during the switch")
        if shared:
            print(f"  {name} shares {info.get('tf_cache')} with what serves now: weights update during the switch")
        else:
            steps.append(f"asu {q(f'cd {q(d)} && python3 - 1 <<' + chr(39) + 'PYEOF' + chr(39) + chr(10) + _SPARK_PREP + 'PYEOF')} || exit 15")
        r2, _ = cm._root_stream(c, h, script + "\n".join(steps) + "\n") if steps else (0, "")
        if r2:
            raise RuntimeError(f"prepare failed (rc={r2})")
    elif cat["kind"] == "spec":
        from noctyx import recipespec
        r1, _ = cm._root_stream(c, h, script + f"test -r {q(d + '/recipe.toml')} || exit 18\n")
        if r1:
            raise RuntimeError(f"checkout failed or recipe.toml missing (rc={r1})")
        rt = cm._run_on(c, h, f"cat {q(d + '/recipe.toml')}", timeout=30)
        if rt.returncode != 0:
            raise RuntimeError(f"cannot read {d}/recipe.toml: {(rt.stderr or '').strip()[:120]}")
        spec, errs = recipespec.parse_deploy_toml(rt.stdout or "")
        if errs or not spec:
            raise RuntimeError("invalid recipe.toml [deploy]: " + ("; ".join(errs) if errs else "missing [deploy] section"))
        senv, missing = recipespec.resolve_settings(spec, {**env, spec["port_env"]: str(s["port"])})
        if missing:
            raise RuntimeError(f"recipe.toml [deploy.settings] missing required setting(s): {', '.join(missing)}")
        dotenv = recipespec.render_dotenv(senv)
        b64_env = base64.b64encode(dotenv.encode()).decode()
        env_file = q(f"{d}/.env")
        put_env_cmd = f"echo {b64_env} | base64 -d > {env_file}"
        put_env = f"sudo -u {q(user)} bash -c {q(put_env_cmd)} || exit 12\n"
        prep_str = spec.get("prepare") or ""
        prep_inner = f"cd {q(d)} && set -a && . ./.env && set +a && {prep_str}"
        prep_cmd = f"asu {q(prep_inner)} || exit 19\n" if prep_str else ""
        r2, _ = cm._root_stream(c, h, f"set -uo pipefail\ncd /\nasu() {{ sudo -u {q(user)} -H env HOME={q(home)} {_sh_env(hfe)} bash -lc \"$1\"; }}\n" + put_env + prep_cmd)
        if r2:
            raise RuntimeError(f"spec prepare failed (rc={r2})")
        rec["spec"] = spec
        rec["expect"] = list(spec.get("served") or [])
        rec["unit_text"] = _unit_text(rec, slot, s, senv)
    elif cat["kind"] == "script":
        chk = (f"cd {q(d)} && test -x start.sh && test -x stop.sh"
               f" && if grep -q DRY_RUN start.sh; then set -a; . ./.env; set +a; DRY_RUN=1 ./start.sh; fi")
        r1, _ = cm._root_stream(c, h, script + f"asu {q(chk)} || exit 17\n")
        if r1:
            raise RuntimeError(f"start.sh check failed (rc={r1}): needs start.sh + stop.sh, and its DRY_RUN must pass")
        rec["expect"] = []
    else:
        bld = f"cd {q(d)} && test -x setup.sh -a -x serve.sh && {_sh_env({k: v for k, v in env.items() if k != 'PORT'})} ./setup.sh"
        r1, _ = cm._root_stream(c, h, script + f"asu {q(bld)} || exit 16\n")
        if r1:
            raise RuntimeError(f"setup.sh failed (rc={r1})")
        rec["expect"] = []
    rec["prepared_ts"] = time.time()
    st = _recipe_state()
    st["slots"].setdefault(slot, {}).setdefault("prepared", {})[name] = rec
    _save_recipe_state(st)
    cm.audit("recipe-prepare", slot=slot, recipe=name, sha=sha, host=h["name"])
    return rec


def _switch(c, slot, frm, to, wait_lock=0):
    """Under the slot's GPU lock on its head: stop frm (and every conflicting unit), check the port
    and GPU are free, start to, wait until it serves. rc 0 + models, or rc>0 (75: lock busy, nothing done)."""
    cm = _cm()
    s = recipe_slot(c, slot)
    h = cm.host_by_name(c, s["head"])
    user = _slot_user(c, h)
    home = (to or frm or {}).get("home") or cm._home(c, h, user)
    q = shlex.quote
    lock = _tilde(s.get("lock") or P["gpu_lock"], home)
    port = int(s["port"])
    stop = list(dict.fromkeys([*(s.get("conflicts") or []), f"{P['recipe_unit_prefix']}-{slot}.service"] + ([frm["unit"]] if frm else [])))
    downs = [x for x in {(frm or {}).get("spark_dir"), (frm or {}).get("dir") if (frm or {}).get("kind") == "spark" else None} if x]
    sdowns = [(frm or {})["dir"]] if (frm or {}).get("kind") == "script" else []
    spec_downs = [((frm or {})["dir"], ((frm or {}).get("spec") or {}).get("stop"))] if (frm or {}).get("kind") == "spec" and ((frm or {}).get("spec") or {}).get("stop") else []
    to_spec = (to or {}).get("spec") or {}
    health_path = to_spec.get("health") or "/health"
    models_path = to_spec.get("models") or "/v1/models"
    ht = max(health_timeout(s), int(to_spec.get("start_timeout_s") or 0))
    lines = [f"""set -uo pipefail
cd /
x() {{ echo "+ $*"; "$@"; }}
asu() {{ sudo -u {q(user)} -H env HOME={q(home)} bash -lc "$1"; }}
LOCK={q(lock)}
[ -e "$LOCK" ] || sudo -u {q(user)} sh -c 'mkdir -p "$(dirname "$1")" && touch "$1"' _ "$LOCK"
exec 9>>"$LOCK"
if ! flock -n 9; then
  echo "GPU lock $LOCK is held:"; ps -o pid=,etime=,args= -p "$(fuser "$LOCK" 2>/dev/null | tr -s ' ' ',' | sed 's/^,//')" 2>/dev/null | cut -c1-160
  {f'echo "waiting up to {wait_lock}s"; flock -w {wait_lock} 9 ||' if wait_lock else ''} exit 75
fi
alive() {{ curl -s -m 3 http://127.0.0.1:{port}{health_path} 2>/dev/null | grep -q '"status"'; }}
echo "== stop ({rec_label(frm)})"
for u in {' '.join(q(u) for u in stop)}; do
  if systemctl is-enabled -q "$u" 2>/dev/null || systemctl is-active -q "$u" 2>/dev/null; then x systemctl disable --now "$u"; fi
done"""]
    for dd in downs:
        dd = _tilde(dd, home)
        lines.append(f"[ -d {q(dd)} ] && asu {q(f'cd {q(dd)} && python3 -m spark down')}")
    for dd in sdowns:
        lines.append(f"[ -x {q(dd)}/stop.sh ] && asu {q(f'cd {q(dd)} && set -a && . ./.env && set +a && ./stop.sh')}")
    for dd, scmd in spec_downs:
        lines.append(f"[ -d {q(dd)} ] && asu {q(f'cd {q(dd)} && ([ -f ./.env ] && set -a && . ./.env && set +a || true) && {scmd}')}")
    lines.append(f"""for i in $(seq 120); do alive || break; sleep 1; done
if alive; then echo "port {port} still answers after the stop: a server Noctyx does not manage holds it"; exit 76; fi""")
    if s.get("gpu_idle_check"):
        lines.append("""for i in $(seq 120); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && break; sleep 1; done
busy=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)
if [ -n "$busy" ]; then echo "the GPU is still in use:"; echo "$busy"; exit 77; fi""")
    if not to:
        lines.append('echo "== stopped"; exit 0')
        rc, _ = cm._root_stream(c, h, "\n".join(lines) + "\n")
        return rc, []
    lines.append(f"sleep {int(s.get('settle_s', 5))}")
    lines.append(f'echo "== start ({rec_label(to)})"')
    unit = to["unit"]
    if not to.get("adopted"):
        b64 = base64.b64encode(to["unit_text"].encode()).decode()
        lines.append(f"[ -d {q(to['dir'])} ] || {{ echo 'checkout {to['dir']} is gone: prepare again'; exit 78; }}")
        lines.append(f"echo {b64} | base64 -d > /etc/systemd/system/{unit} && systemctl daemon-reload || exit 78")
    lines.append(f"""x systemctl enable {q(unit)} >/dev/null 2>&1; x systemctl start --no-block {q(unit)} || exit 78
t0=$(date +%s); n=0
while :; do
  if alive && curl -s -m 3 http://127.0.0.1:{port}{health_path} | grep -q '"status": *"ok"' && curl -s -m 3 http://127.0.0.1:{port}{models_path} | grep -q '"id"'; then break; fi
  st=$(systemctl show -p ActiveState --value {q(unit)})
  if [ "$st" = failed ] || {{ [ "$st" = inactive ] && [ $(( $(date +%s) - t0 )) -gt 20 ]; }}; then
    echo "{unit} is $st:"; journalctl -u {q(unit)} -n 40 --no-pager -o cat; exit 79; fi
  if [ $(( $(date +%s) - t0 )) -gt {ht} ]; then echo "not serving after {ht}s"; journalctl -u {q(unit)} -n 20 --no-pager -o cat; exit 79; fi
  n=$((n+1)); [ $((n % 12)) -eq 0 ] && echo "  loading… $(( $(date +%s) - t0 ))s ($st)"
  sleep 5
done
echo "== serving after $(( $(date +%s) - t0 ))s"
echo "NX_MODELS $(curl -s -m 5 http://127.0.0.1:{port}{models_path} | python3 -c 'import json,sys; print(" ".join(m["id"] for m in json.load(sys.stdin)["data"]))')"
""")
    rc, out = cm._root_stream(c, h, "\n".join(lines) + "\n")
    m = re.findall(r"^NX_MODELS (.*)$", out, re.M)
    return rc, (m[-1].split() if m else [])


def _llm_refresh(wait=False):
    cm = _cm()
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "start", *([] if wait else ["--no-block"]), "llm-discover.service"],
                       capture_output=True, timeout=300)
    else:
        subprocess.run([sys.executable, "-m", "noctyx.cli", "llm-discover"], capture_output=True, timeout=300,
                       env={**os.environ, "PYTHONPATH": str(cm.NX_HOME.parent)})


_GPU_SNIPPET = r"""f=%(lock)s
case "$f" in "~/"*) f="$(getent passwd %(user)s | cut -d: -f6)/${f#\~/}";; esac
if [ -e "$f" ]; then
  key=$(printf '%%02x:%%02x:%%s' $(stat -c '%%Hd %%Ld %%i' "$f"))
  awk -v k="$key" '$0 ~ k {print ($2=="->" ? "W " $6 : "H " $5)}' /proc/locks
else echo "N"; fi
echo "=="
nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null
echo "=="
ps -eo pid=,etimes=,user=,args= 2>/dev/null
"""


def parse_gpu_use(text):
    """The snippet's output -> {"lock": "free"|"held"|"none", "holder": {...}|None, "waiting": [...], "gpu": [...]}."""
    from noctyx import agent
    locks, gpu, ps = (text.split("==\n") + ["", ""])[:3]
    procs = {}
    for line in ps.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)", line)
        if m:
            procs[int(m.group(1))] = {"pid": int(m.group(1)), "seconds": int(m.group(2)), "user": m.group(3),
                                      "cmd": agent.redact(m.group(4))[:160]}
    who = lambda pid: procs.get(pid) or {"pid": pid, "seconds": None, "user": None, "cmd": "(a process in another namespace)"}
    held = [int(l.split()[1]) for l in locks.splitlines() if l.startswith("H ") and l.split()[1].isdigit()]
    wait = [int(l.split()[1]) for l in locks.splitlines() if l.startswith("W ") and l.split()[1].isdigit()]
    out = {"lock": "none" if locks.strip() == "N" else "held" if held else "free",
           "holder": who(held[0]) if held else None, "waiting": [who(p) for p in wait], "gpu": []}
    for line in gpu.splitlines():
        m = re.match(r"\s*(\d+)\s*,\s*(\d+)", line)
        if m:
            out["gpu"].append({**who(int(m.group(1))), "mib": int(m.group(2))})
    return out


def slot_gpu_use(c, slot, timeout=12):
    """Who holds the slot's GPU lock, who waits for it, and which processes use its GPU, read on the slot's head."""
    cm = _cm()
    s = recipe_slot(c, slot)
    h = cm.host_by_name(c, s["head"])
    if h.get("os", "linux") != "linux":
        return {"lock": "none", "holder": None, "waiting": [], "gpu": [], "note": "not a Linux host"}
    snippet = _GPU_SNIPPET % {"lock": shlex.quote(s.get("lock") or P["gpu_lock"]), "user": shlex.quote(_slot_user(c, h))}
    try:
        r = cm._on(c, h, snippet, timeout=timeout)
    except Exception as e:
        return {"error": str(e)[:120]}
    if r.returncode not in (0,) and not (r.stdout or "").strip():
        return {"error": (r.stderr or f"rc {r.returncode}").strip()[-120:]}
    return {**parse_gpu_use(r.stdout or ""), "path": s.get("lock") or P["gpu_lock"]}


def gpu_use_line(u):
    """One line for a slot's GPU use (recipe-status)."""
    cm = _cm()
    if u.get("error"):
        return f"GPU: could not read ({u['error']})"
    d = lambda p: f"{p['cmd'][:70]} (pid {p['pid']}" + (f", {cm.fmt_duration(p['seconds'])}" if p.get("seconds") is not None else "") + ")"
    gpu = "; ".join(f"{p['cmd'].split()[0].rsplit('/', 1)[-1]} {p['mib'] / 1024:.1f} GiB" for p in u.get("gpu") or []) or "idle"
    lock = ("lock held by " + d(u["holder"]) if u.get("holder") else "lock free" if u.get("lock") == "free" else "no lock file yet")
    return f"GPU: {gpu} · {lock}" + (f" · {len(u['waiting'])} waiting: " + "; ".join(d(p) for p in u["waiting"]) if u.get("waiting") else "")


def _answers_after(c, slot):
    """After a switch that came up: ask the fixed prompts (noctyx.answers)."""
    cm = _cm()
    try:
        from noctyx import answers
        if answers.every(c) is None:
            return
        rec = answers.check_slot(c, slot)
        print("  answers  " + answers.describe(slot, rec), flush=True)
        last = rec.get("last") or {}
        cm.audit("slot-check", slot=slot, state=last.get("state"), model=last.get("model"), prompts=last.get("prompts"),
                 after="switch")
    except Exception as e:
        print(f"  (the answer check did not run: {str(e)[:120]})")


def recipe_plan(c, slot, name=None, ref=None):
    cm = _cm()
    s = recipe_slot(c, slot)
    cur, prev, _ = slot_records(c, slot)
    live = cm.recipe_live(c, slot)
    hosts_ = [s["head"]] + ([s["worker"]] if s.get("worker") else [])
    plan = {"slot": slot, "hosts": hosts_, "port": s["port"], "current": cur, "previous": prev, "live": live,
            "impact": [], "blockers": []}
    if not slot_deploys(s):
        plan["blockers"].append(f"{slot} is watch-only (deploy: false in the site config): Noctyx does not switch it")
    if live["ok"]:
        plan["impact"].append(f"{', '.join(live['models'])} on {'+'.join(hosts_)}:{s['port']} stops serving during the switch")
    if name:
        cat = recipes_cfg(c)["catalog"].get(name)
        if not cat:
            plan["blockers"].append(f"unknown recipe {name}")
        elif slot not in (cat.get("slots") or []):
            plan["blockers"].append(f"{name} does not run on {slot}")
        else:
            try:
                plan["sha"] = cm._resolve_ref(cat["repo"], ref or cat.get("ref") or "main")
            except Exception as e:
                plan["blockers"].append(str(e)[:200])
    try:
        holder = cm.spindle_holder(f"recipe-{slot}")
        if holder:
            plan["blockers"].append(f"another recipe job holds slot {slot}: {holder.get('what')} since {holder.get('since')}")
    except Exception:
        pass
    return plan


def _print_plan(p, what):
    print(f"{what} on slot {p['slot']} ({'+'.join(p['hosts'])}, port {p['port']})")
    print(f"  now:  {rec_label(p['current'])}  — " + (f"serving {', '.join(p['live']['models'])}" if p["live"]["ok"]
                                                   else f"not serving ({p['live'].get('error') or p['live'].get('status')})"))
    for i in p["impact"]:
        print(f"  ! {i}")
    for b in p["blockers"]:
        print(f"  ✗ {b}")


def cmd_recipe_deploy(a):
    cm = _cm()
    c = cm.cfg()
    p = recipe_plan(c, a.slot, a.recipe, a.ref)
    _print_plan(p, f"deploy {a.recipe}" + (f"@{p['sha'][:12]}" if p.get("sha") else ""))
    if p["blockers"]:
        sys.exit(1)
    if not (a.yes or a.prepare_only):
        print("\nnothing changed: add --yes to deploy (or --prepare-only to check out and build without switching)")
        sys.exit(2)
    try:
        with cm.spindle_lock(f"recipe-{a.slot}", what=f"recipe-deploy {a.recipe} on {a.slot}"):
            t0 = time.time()
            try:
                new = cm.recipe_prepare(c, a.slot, a.recipe, a.ref)
            except Exception as e:
                print(f"prepare FAILED: {e} — nothing was stopped")
                cm.audit("recipe-deploy", slot=a.slot, recipe=a.recipe, rc=1, stage="prepare", error=str(e)[:200])
                sys.exit(1)
            print(f"prepared in {cm.fmt_duration(time.time() - t0)}", flush=True)
            if a.prepare_only:
                return
            cur, _, _ = slot_records(c, a.slot)
            t1 = time.time()
            rc, models = cm._switch(c, a.slot, cur, new, wait_lock=a.wait_lock * 60)
            if rc == 0:
                new.update(models=models, deployed_ts=time.time(), state="serving")
                _record(a.slot, current=new, previous=cur,
                        event={"event": "deploy", "to": rec_label(new), "from": rec_label(cur), "rc": 0,
                               "down_s": round(time.time() - t1)})
                cm.audit("recipe-deploy", slot=a.slot, recipe=a.recipe, sha=new["sha"], rc=0, models=models,
                         replaced=rec_label(cur), down_s=round(time.time() - t1))
                print(f"✓ {a.slot}: {rec_label(new)} serving {', '.join(models)} "
                      f"(switch took {cm.fmt_duration(time.time() - t1)})")
                cm._answers_after(c, a.slot)
                cm._llm_refresh()
                return
            if rc == 75:
                print("✗ the GPU lock is held (a harness run or another job): nothing was stopped — try again later"
                      " or pass --wait-lock MINUTES")
                cm.audit("recipe-deploy", slot=a.slot, recipe=a.recipe, rc=75, stage="lock")
                sys.exit(75)
            print(f"✗ {rec_label(new)} did not come up (rc={rc}): switching back to {rec_label(cur)}", flush=True)
            back = 1
            if cur:
                back, models = cm._switch(c, a.slot, new, cur, wait_lock=600)
            _record(a.slot, event={"event": "deploy", "to": rec_label(new), "from": rec_label(cur), "rc": rc,
                                   "rolled_back": back == 0})
            cm.audit("recipe-deploy", slot=a.slot, recipe=a.recipe, sha=new["sha"], rc=rc, rolled_back=back == 0)
            print(f"{'✓ rolled back: ' + rec_label(cur) + ' serving ' + ', '.join(models) if back == 0 else '✗ ROLLBACK FAILED: slot ' + a.slot + ' is serving nothing — check ' + new['host']}")
            cm._llm_refresh()
            sys.exit(rc)
    except cm.SpindleBusy:
        print(f"✗ slot {a.slot} is busy: {cm.spindle_holder(f'recipe-{a.slot}').get('what')}")
        sys.exit(1)


def _slot_op(a, what):
    cm = _cm()
    c = cm.cfg()
    p = recipe_plan(c, a.slot)
    cur, prev = p["current"], p["previous"]
    _print_plan(p, what)
    if p["blockers"]:
        sys.exit(1)
    if what == "rollback" and not prev:
        sys.exit("nothing to roll back to")
    if what == "start" and p["live"]["ok"]:
        print("already serving — nothing to do")
        return
    if what != "start" and not a.yes:
        print(f"\nnothing changed: add --yes to {what}" + (f" (back to {rec_label(prev)})" if what == "rollback" else ""))
        sys.exit(2)
    try:
        with cm.spindle_lock(f"recipe-{a.slot}", what=f"recipe-{what} {a.slot}"):
            frm, to = {"stop": (cur, None), "start": (None, cur), "rollback": (cur, prev)}[what]
            rc, models = cm._switch(c, a.slot, frm, to, wait_lock=a.wait_lock * 60)
            if rc == 0 and what == "rollback":
                _record(a.slot, current={**prev, "models": models, "state": "serving"}, previous=cur,
                        event={"event": "rollback", "to": rec_label(prev), "from": rec_label(cur), "rc": 0})
            elif rc == 0 and cur:
                _record(a.slot, current={**cur, "state": "stopped" if what == "stop" else "serving",
                                         **({"models": models} if models else {})}, keep_previous=True,
                        event={"event": what, "to": rec_label(cur), "rc": 0})
            else:
                _record(a.slot, event={"event": what, "to": rec_label(to or cur), "rc": rc})
            cm.audit(f"recipe-{what}", slot=a.slot, rc=rc, target=rec_label(to or cur), models=models)
            print(("✓ " if rc == 0 else "✗ ") + f"{what} {a.slot}: rc={rc}" + (f", serving {', '.join(models)}" if models else ""))
            if rc == 0 and what != "stop":
                cm._answers_after(cm.cfg(), a.slot)
            cm._llm_refresh()
            sys.exit(rc)
    except cm.SpindleBusy:
        print(f"✗ slot {a.slot} is busy: {cm.spindle_holder(f'recipe-{a.slot}').get('what')}")
        sys.exit(1)


def cmd_recipe_stop(a):
    _slot_op(a, "stop")


def cmd_recipe_start(a):
    _slot_op(a, "start")


def cmd_recipe_rollback(a):
    _slot_op(a, "rollback")


def recipe_summary(c, live=True):
    """Slots (config, current/previous/history, live answer) and the catalog — CLI status and the UI."""
    cm = _cm()
    rc_ = recipes_cfg(c)
    st = _recipe_state()["slots"]
    from noctyx import answers
    ans = answers.summary()
    slots = {}
    for n, s in rc_["slots"].items():
        cur, prev, hist = slot_records(c, n)
        lv = cm.recipe_live(c, n) if live else {}
        exp = (cur or {}).get("models") or (cur or {}).get("expect") or []
        drift = None
        if lv.get("ok") and exp and not set(exp) & set(lv["models"]):
            drift = f"serving {', '.join(lv['models'])}, but Noctyx last started {rec_label(cur)} ({', '.join(exp)})"
        elif not lv.get("ok") and (cur or {}).get("state") == "serving":
            drift = f"{rec_label(cur)} should be serving but the port does not answer"
        unrecorded = bool(lv.get("ok") and not exp and (cur is None or cur.get("adopted")))
        slots[n] = {"head": s["head"], "worker": s.get("worker"), "port": s["port"], "lock": s.get("lock"),
                    "current": cur, "previous": prev, "history": hist[:10], "live": lv, "drift": drift,
                    "unrecorded": unrecorded, "deploy": slot_deploys(s), "answers": ans.get(n),
                    "prepared": {k: {x: v.get(x) for x in ("sha", "ref", "prepared_ts")} for k, v in
                                 ((st.get(n) or {}).get("prepared") or {}).items()},
                    "recipes": [k for k, v in rc_["catalog"].items() if n in (v.get("slots") or [])],
                    "busy": cm.spindle_holder(f"recipe-{n}").get("what")}
    for r in slots.values():
        for k in ("current", "previous"):
            if r[k]:
                r[k] = {x: v for x, v in r[k].items() if x != "unit_text"}
    try:
        disc = json.loads(cm.DISCOVER_FILE.read_text())
    except Exception:
        disc = {}
    return {"slots": slots, "catalog": rc_["catalog"], "kinds": list(RECIPE_KINDS), "discover": disc}


def cmd_recipe_status(a):
    cm = _cm()
    c = cm.cfg()
    d = recipe_summary(c)
    if a.json:
        print(json.dumps(d, indent=1, default=str))
        return
    for n, s in d["slots"].items():
        lv = s["live"]
        print(f"{n:<9} {s['head']}{'+' + s['worker'] if s['worker'] else ''}:{s['port']}  {rec_label(s['current'])}")
        print(f"          {'serving ' + ', '.join(lv['models']) if lv.get('ok') else 'NOT serving (' + str(lv.get('error') or lv.get('status')) + ')'}"
              + (f"  · previous: {rec_label(s['previous'])}" if s["previous"] else ""))
        if s["drift"]:
            print(f"          ! {s['drift']}")
            if lv.get("ok"):
                print(f"            a swap made by hand? `noctyx recipe-adopt --slot {n} --unit UNIT` records it")
        elif s["unrecorded"]:
            print(f"          · Noctyx has no record of which models this serves, so it cannot tell a swap: "
                  f"`noctyx recipe-adopt --slot {n}` records what answers now")
        if not s["deploy"]:
            print("          watch-only (deploy: false): Noctyx reports it and never switches it")
        print("          " + gpu_use_line(slot_gpu_use(c, n)))
        an = s.get("answers") or {}
        if an.get("state") in ("changed", "wrong"):
            print(f"          ! answers {an['state']}: {an.get('detail')}  (`noctyx slot-check --slot {n}`)")
        elif an.get("state"):
            print(f"          answers {an['state']}, checked {cm.fmt_duration(time.time() - (an.get('ts') or 0))} ago")
        if s["busy"]:
            print(f"          busy: {s['busy']}")
    print("catalog:")
    for k, v in d["catalog"].items():
        print(f"  {k:<24} {v['kind']:<8} {v.get('ref', 'main'):<10} {', '.join(v.get('slots') or [])}  {v['repo']}")


def cmd_recipe_adopt(a):
    """Record what a slot serves now as its deployment: a model somebody started or swapped by hand."""
    cm = _cm()
    c = cm.cfg()
    s = recipe_slot(c, a.slot)
    lv = cm.recipe_live(c, a.slot, timeout=10)
    if not lv.get("ok"):
        sys.exit(f"{a.slot} does not answer on port {s['port']} ({lv.get('error') or lv.get('status')}): "
                 "nothing to adopt")
    cur, _, _ = slot_records(c, a.slot)
    old = dict(s.get("adopt") or {})
    if not a.unit and cur and not cur.get("adopted"):
        sys.exit(f"{a.slot} runs Noctyx's own deployment {rec_label(cur)}: name the unit that serves now "
                 "(--unit), or deploy again to put Noctyx's back")
    unit = a.unit or old.get("unit")
    if slot_deploys(s) and not unit:
        sys.exit("name the unit that serves it (--unit NAME.service): Noctyx stops and starts a deployment "
                 "through its unit")
    if unit and not _UNIT.match(unit):
        sys.exit(f"not a service unit name: {unit}")
    if a.spark_dir and not re.match(r"^[A-Za-z0-9~/._-]{1,200}$", a.spark_dir):
        sys.exit(f"not a directory path: {a.spark_dir}")
    name = a.name or ", ".join(lv["models"])
    new = {k: v for k, v in old.items() if k not in ("unit", "name", "models", "spark_dir", "adopted_ts")}
    new.update(name=name, models=lv["models"],
               adopted_ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    if unit:
        new["unit"] = unit
        if unit == old.get("unit") and old.get("spark_dir") and not a.spark_dir:
            new["spark_dir"] = old["spark_dir"]
    if a.spark_dir:
        new["spark_dir"] = a.spark_dir
    print(f"{a.slot}: serving {', '.join(lv['models'])}")
    print(f"  recorded now: {rec_label(cur)}" + (f" ({', '.join(cur['models'])})" if (cur or {}).get("models") else ""))
    print(f"  to record   : {name}" + (f", unit {unit}" if unit else ", no unit (watch-only)"))
    if unit and slot_deploys(s):
        h = cm.host_by_name(c, s["head"])
        try:
            r = cm.ssh(c, h, f"systemctl show -p LoadState --value {shlex.quote(unit)}", timeout=20)
            if (r.stdout or "").strip() != "loaded":
                sys.exit(f"{unit} is not a unit on {s['head']}: Noctyx could not stop or start this deployment "
                         "through it")
        except subprocess.TimeoutExpired:
            sys.exit(f"{s['head']} did not answer over ssh: the unit could not be checked")
    if not a.yes:
        print("\nnothing changed: add --yes to record it")
        sys.exit(2)
    r = dict(c.get("recipes") or {})
    slots = dict(r.get("slots") or {})
    slots[a.slot] = {**s, "adopt": new}
    if unit and slot_deploys(s) and unit not in (s.get("conflicts") or []):
        slots[a.slot]["conflicts"] = list(s.get("conflicts") or []) + [unit]
    r["slots"] = slots
    cm._write_block("recipes", r)
    if cur and not cur.get("adopted"):
        st = _recipe_state()
        sl = st["slots"].setdefault(a.slot, {})
        sl["previous"] = sl.pop("current")
        _save_recipe_state(st)
    _record(a.slot, event={"event": "adopt", "to": name, "from": rec_label(cur), "rc": 0})
    cm.audit("recipe-adopt", slot=a.slot, name=name, unit=unit, models=lv["models"], replaced=rec_label(cur))
    print(f"✓ {a.slot}: {name} is the recorded deployment")


def validate_catalog_entry(name, e, c):
    errs = []
    if not _RNAME.match(name or ""):
        errs.append("name: lowercase letters, digits, '.' and '-' (max 48)")
    if not isinstance(e, dict):
        return ["entry must be an object"]
    if not _REPO.match(str(e.get("repo", ""))):
        errs.append("repo: an https:// or ssh:// git URL")
    if e.get("kind") not in RECIPE_KINDS:
        errs.append(f"kind: one of {', '.join(RECIPE_KINDS)}")
    if not _REF.match(str(e.get("ref", "main"))):
        errs.append("ref: a branch, tag or commit sha")
    sl = e.get("slots")
    known = recipes_cfg(c)["slots"]
    if not isinstance(sl, list) or not sl or any(x not in known for x in sl):
        errs.append(f"slots: one or more of {', '.join(known)}")
    for k in ("env", "overrides"):
        if k in e and not isinstance(e[k], dict):
            errs.append(f"{k}: an object")
    if any(not _ENVK.match(k) for k in (e.get("env") or {})):
        errs.append("env: UPPER_CASE names")
    return errs


def cmd_recipe_catalog(a):
    """Add, replace or remove one catalog entry."""
    cm = _cm()
    c = cm.cfg()
    r = dict(c.get("recipes") or {})
    cat = dict(r.get("catalog") or {})
    if a.remove:
        if a.name not in cat:
            sys.exit(f"no recipe {a.name}")
        using = [n for n, s in recipe_summary(c, live=False)["slots"].items() if (s["current"] or {}).get("recipe") == a.name]
        if using:
            sys.exit(f"{a.name} is deployed on {', '.join(using)}: deploy something else there first")
        cat.pop(a.name)
    else:
        e = json.loads(a.json)
        errs = validate_catalog_entry(a.name, e, c)
        if errs:
            sys.exit("invalid: " + "; ".join(errs))
        cat[a.name] = {k: e[k] for k in ("repo", "ref", "kind", "slots", "env", "overrides") if k in e}
    r["catalog"] = cat
    cm._write_block("recipes", r)
    cm.audit("recipe-catalog", name=a.name, removed=bool(a.remove))
    print(f"catalog: {'removed' if a.remove else 'saved'} {a.name}")
