"""Recipe discovery: public recipe repos that fit this cluster's hardware, ranked by measured speed or recency.

    noctyx recipe-discover [--sort fastest|newest|stars]

Searches GitHub (unauthenticated, paced), reads each repo's recipe.toml to tell what hardware and how many
machines it is built for and which layout it has (spark, setup.sh + serve.sh, start.sh + stop.sh), and joins
measured speeds from the recipe harness ledger and the public leaderboard. The answer is kept in the
orchestrator's DISCOVER_FILE for the console's Recipes page.
"""
import json
import os
import pathlib
import re
import time
import urllib.parse
import urllib.request

from noctyx.paths import P


def _cm():
    from noctyx import orchestrator as cm
    return cm


DISCOVER_BASE = ["tensorfold recipe"]
DISCOVER_HW = {"gb10": ["dgx-sparks tensorfold", "2x-dgx-sparks", "dual dgx spark tensorfold"],
               "rtx5090": ["rtx-5090 tensorfold", "rtx 5090 recipe"],
               "mac": ["m5 ultra tensorfold", "mlx m5 ultra recipe"]}


def discover_queries(c, hws):
    """recipes.discover_queries replaces the list; otherwise the base ones plus some per GPU on this site."""
    q = (c.get("recipes") or {}).get("discover_queries")
    if q:
        return list(q)
    out = list(DISCOVER_BASE)
    for k in sorted(hws):
        lbl = GPU_LABEL.get(k, k).lower().replace("(", "").replace(")", "")
        out += DISCOVER_HW.get(k, [f"{k} tensorfold", f"{lbl} recipe"])
    return list(dict.fromkeys(out))


LEADERBOARD = "https://tensorfold.dev/api/benchmarks/v1/results"
_UA = {"User-Agent": "curl/8.5.0 noctyx-recipe-discover", "Accept": "application/json"}


def _http(url, timeout=20, raw=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        b = r.read()
    return b.decode("utf-8", "replace") if raw else json.loads(b)


def _gh_search(q, per_page=40):
    """Unauthenticated repository search (10/min: callers pace it). No token is involved anywhere."""
    u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "sort": "updated", "per_page": per_page})
    return _http(u).get("items") or []


def _raw(full, branch, path):
    try:
        return _http(f"https://raw.githubusercontent.com/{full}/{branch}/{path}", timeout=15, raw=True)
    except Exception:
        return None


# hardware a recipe can be built for: (key, label, pattern). First match wins, so specific before general.
GPU_KINDS = [
    ("gb10", "DGX Spark (GB10)", r"\bgb10\b|dgx[- ]?sparks?|\bsparks?\b"),
    ("rtx-pro-6000", "RTX PRO 6000", r"rtx[- ]?pro[- ]?6000|pro[- ]?6000[- ]?blackwell"),
    ("rtx5090", "RTX 5090", r"5090"),
    ("rtx5080", "RTX 5080", r"5080"),
    ("rtx4090", "RTX 4090", r"4090"),
    ("rtx3090", "RTX 3090", r"3090"),
    ("h200", "H200", r"\bh200\b"), ("h100", "H100", r"\bh100\b"), ("b200", "B200", r"\bb200\b"),
    ("a100", "A100", r"\ba100\b"), ("l40s", "L40S", r"\bl40s\b"),
    ("mac", "Apple silicon", r"\bm[345][- ]?(ultra|max|pro)\b|apple[- ]?(silicon|m[1-9])|mac[- ]?studio|\bmlx\b|\bmetal\b"),
]
GPU_LABEL = {k: lbl for k, lbl, _ in GPU_KINDS}


def hw_key(text):
    """The GPU a text names (a repo name, a README, nvidia-smi's model): a GPU_KINDS key, or None."""
    t = (text or "").lower().replace("_", "-")
    not_gb10 = re.search(r"jetson|\bthor\b|apache[- ]spark|pyspark|spark[- ](sql|streaming|cluster|job)", t)
    for k, _, pat in GPU_KINDS:
        if k == "gb10" and not_gb10:
            continue
        if re.search(pat, t):
            return k
    if re.search(r"sm[- =\"]*120\b", t):                # Blackwell consumer silicon, no model named
        return "rtx5090"
    return None


def _hw_class(text):
    """What a recipe is built for, from its name, description and recipe.toml: (key or "other", units)."""
    t = text.lower().replace("_", "-")
    n = None
    m = re.search(r"\b([1-8])x[- ]?(?:nvidia[- ])?(?:dgx[- ])?(?:spark|gb10|rtx|h100|h200|b200|a100|l40s)|\btp[= -]?([2-8])\b", t)
    if m:
        n = int(m.group(1) or m.group(2))
    else:
        for w, k in (("dual|two", 2), ("three", 3), ("four", 4)):
            if re.search(rf"\b({w})[- ](?:nvidia[- ])?(?:dgx[- ])?(?:sparks?|gpus?|nodes?)\b", t):
                n = k
                break
    return hw_key(t) or "other", n or 1


def slot_hw(c, name, s):
    """(GPU key, devices) of a slot: its "hw" / "gpus" in the config, else the GPU its head node reports
    (the console's facts) and 2 for a head + worker pair."""
    key = s.get("hw")
    if not key:
        try:
            facts = json.loads((pathlib.Path(P["console_state"]) / "facts.json").read_text())
            key = hw_key(" ".join(map(str, (facts.get(s.get("head")) or {}).get("gpu") or [])))
        except (OSError, ValueError):
            key = None
    return key or hw_key(name), int(s.get("gpus") or (2 if s.get("worker") else 1))


def _speed_sources(c):
    """{repo full name or HF model id (lower): [{tps, peak, source, hw, ts}]} from the harness ledger
    (measured here) and the tensorfold.dev leaderboard (community runs)."""
    out = {}
    hd = pathlib.Path((c.get("recipes") or {}).get("harness_dir") or "/srv/recipe-harness")
    for f in sorted((hd / "stats").glob("*.json")) if (hd / "stats").is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        tf = hd / "targets" / f"{d.get('target')}.toml"
        keys = set(re.findall(r"github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?[)/\s\"]", (tf.read_text() if tf.exists() else "") + " "))
        runs = [r for r in d.get("runs") or [] if r.get("rulers")]
        for r in runs:
            keys |= set(re.findall(r"github\.com/([\w.-]+/[\w.-]+?)(?:/|$)", (r.get("source") or "") + "/"))
        if not runs or not keys:
            continue
        r = runs[-1]
        rl = [x for v in r["rulers"].values() for x in v if isinstance(x, dict) and x.get("agg")]
        c1 = [x["agg"] for x in rl if x.get("c") == 1]
        cm = max((x.get("c") or 1 for x in rl), default=1)
        rec = {"tps": round(sum(c1) / len(c1), 1) if c1 else None, "peak": max((x["agg"] for x in rl if x.get("c") == cm), default=None),
               "streams": cm, "source": f"harness {d.get('target')}", "measured": True, "hw": r.get("hardware"),
               "ts": r.get("started")}
        for k in keys:
            out.setdefault(k.lower(), []).append(rec)
    try:
        cur, res = None, []
        for _ in range(40):
            j = _http(LEADERBOARD + "?limit=50" + (f"&cursor={urllib.parse.quote(cur)}" if cur else ""))
            res += j.get("results") or []
            cur = j.get("next_cursor")
            if not cur:
                break
        for r in res:
            rc = r.get("receipt") or {}
            mid = ((rc.get("model") or {}).get("repo_id") or "").lower()
            tps = [s["delivery_tps"]["median"] for s in r.get("summary") or [] if (s.get("delivery_tps") or {}).get("median")]
            if not mid or not tps:
                continue
            g = ((rc.get("hardware") or {}).get("gpus") or [{}])[0].get("name") or "?"
            out.setdefault(mid, []).append({"tps": round(sorted(tps)[len(tps) // 2], 1), "source": f"tensorfold.dev ({r.get('display_name')})",
                                            "measured": True, "hw": f"{(rc.get('settings') or {}).get('rank_count', 1)}x {g}",
                                            "ts": r.get("received_at")})
    except Exception as e:
        print(f"  leaderboard: {str(e)[:100]}")
    return out


def _hw_match(cls, units, hw):
    return hw_key(hw) == cls


def recipe_discover(c, limit=80):
    rc_ = _cm().recipes_cfg(c)
    slots = rc_["slots"]
    shw = {n: slot_hw(c, n, s) for n, s in slots.items()}           # slot -> (gpu key, devices)
    macs = {h["name"] for h in c.get("hosts", []) if h.get("os") == "macos"}
    queries = discover_queries(c, {k for k, _ in shw.values() if k} | ({"mac"} if macs else set()))
    owners = sorted({m.group(1) for v in rc_["catalog"].values() for m in [re.search(r"github\.com/([\w.-]+)/", v.get("repo", ""))] if m})
    seen = {}
    for q in queries + [f"user:{o}" for o in owners]:
        try:
            for it in _gh_search(q):
                seen.setdefault(it["full_name"], it)
        except Exception as e:
            print(f"  search {q!r}: {str(e)[:100]}")
        time.sleep(6.5)                      # unauthenticated search: 10 requests a minute
    have = {re.sub(r"(\.git)?/*$", "", v.get("repo", "")).lower(): k for k, v in rc_["catalog"].items()}
    speeds = _speed_sources(c)
    items, n_here, n_away = [], 0, 0
    site_hw = {k for k, _ in shw.values() if k} | ({"mac"} if macs else set())
    for full, it in sorted(seen.items(), key=lambda kv: kv[1].get("pushed_at") or "", reverse=True):
        if it.get("archived") or n_here >= limit:
            continue
        br = it.get("default_branch") or "main"
        toml_ = _raw(full, br, "recipe.toml") or ""
        from noctyx import recipespec
        spec, _spec_errs = recipespec.parse_deploy_toml(toml_) if toml_ else (None, [])
        if spec and spec.get("hardware"):
            cls, units = spec["hardware"], spec["nodes"]
        else:
            cls, units = _hw_class(full.split("/", 1)[1])          # the repo's name says the most
            c2, u2 = _hw_class(" ".join([full, it.get("description") or ""]))
            if cls == "other":
                cls, units = c2, u2
            elif c2 == cls and units == 1:
                units = u2                                       # "…-DGX-Spark" named, "four Sparks" described
            if cls == "other" and toml_:
                cls, units = _hw_class(toml_[:4000])
        if cls == "other":
            continue
        here = cls in site_hw
        if not here and n_away >= limit // 4:      # other hardware: a few for context, never crowding this site's out
            continue
        n_here, n_away = n_here + here, n_away + (not here)
        if spec:
            kind = "spec"
        else:
            has_spark = bool(toml_) and _raw(full, br, "spark/__main__.py") is not None
            has_sys = bool(toml_) and not has_spark and _raw(full, br, "setup.sh") is not None and _raw(full, br, "serve.sh") is not None
            has_script = not (has_spark or has_sys) and _raw(full, br, "start.sh") is not None and _raw(full, br, "stop.sh") is not None
            kind = "spark" if has_spark else "systemd" if has_sys else "script" if has_script else None
        models = [w["repo"] for w in (spec.get("weights") or [])] if spec else []
        if not models:
            models = re.findall(r'^\s*(?:repo|converted_repo|published_repo)\s*=\s*"([\w.-]+/[\w.-]+)"', toml_, re.M)
        fits, why = [], None
        same = {n: g for n, (k, g) in shw.items() if k == cls}       # this site's slots with that GPU
        lbl = GPU_LABEL.get(cls, cls)
        if kind is None:
            why = "no [deploy] spec, spark/, setup.sh + serve.sh or start.sh + stop.sh: needs an adapter"
        elif cls == "mac" and not same:
            why = "Apple silicon: recipes deploy as systemd units, so a macOS host can be monitored, not a slot"
        elif not same:
            why = f"no {lbl} slot on this site"
        else:
            # setup.sh + serve.sh is one machine; spec, spark and start.sh recipes span as many as they say
            want = 1 if kind == "systemd" else units
            fits = sorted(n for n, g in same.items() if g == want)
            if not fits:
                slots_ = ", ".join(f"{n} ({g}×)" for n, g in sorted(same.items()))
                why = f"needs {want}× {lbl}; this site's {lbl} slots: {slots_}"
        sp = [s for k in [full.lower(), *[m.lower() for m in models]] for s in speeds.get(k, [])
              if _hw_match(cls, units, s.get("hw")) or s["source"].startswith("harness")]
        best = max(sp, key=lambda s: s.get("tps") or 0) if sp else None
        claim = None
        if not best:
            rd = _raw(full, br, "README.md") or ""
            m = re.search(r"(\d{2,4}(?:\.\d+)?)\s*(?:tok/s|tokens/s|t/s|tps)\b", rd)
            if m:
                claim = float(m.group(1))
        items.append({"repo": full, "url": it.get("html_url"), "clone": it.get("clone_url"), "branch": br,
                      "description": (it.get("description") or "")[:200], "stars": it.get("stargazers_count", 0),
                      "pushed_at": it.get("pushed_at"), "fork": it.get("fork"), "hw": cls, "units": units, "kind": kind,
                      "deployable": bool(fits), "spec": bool(spec),
                      "models": models[:3], "slots": fits, "why": why, "speed": best, "claimed_tps": claim,
                      "in_catalog": have.get(f"https://github.com/{full}".lower())})
    return {"ts": time.time(), "items": items, "queries": queries, "owners": owners,
            "slots": {n: {"hw": k, "label": GPU_LABEL.get(k, k), "gpus": g} for n, (k, g) in shw.items()}}


def discover_rank(x, sort):
    """fastest: measured tok/s (harness, leaderboard) first, README claims after them, unknown last;
    newest: last push. Ties: newest."""
    sp = (x.get("speed") or {}).get("tps")
    if sort == "newest":
        return (0, x.get("pushed_at") or "")
    if sort == "stars":
        return (x.get("stars") or 0, x.get("pushed_at") or "")
    return ((2, sp) if sp else (1, x.get("claimed_tps")) if x.get("claimed_tps") else (0, 0), x.get("pushed_at") or "")


def cmd_recipe_discover(a):
    cm = _cm()
    c = cm.cfg()
    d = recipe_discover(c)
    DISCOVER_FILE = cm.DISCOVER_FILE
    DISCOVER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISCOVER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, DISCOVER_FILE)
    rows = sorted(d["items"], key=lambda x: discover_rank(x, a.sort), reverse=True)
    print(f"{len(rows)} recipes for this cluster's hardware (of {len(d['items'])} found), by {a.sort}:")
    for x in rows:
        sp = x["speed"]
        s = f"{sp['tps']:>6} tok/s ({sp['source']})" if sp and sp.get("tps") else \
            (f"{x['claimed_tps']:>6} tok/s (README claim)" if x["claimed_tps"] else "     ? tok/s")
        fit = ("→ " + ", ".join(x["slots"])) if x["slots"] else f"✗ {x['why']}"
        print(f"  {x['repo']:<62} {x['hw']}{'x' + str(x['units']) if x['units'] > 1 else ''}  {(x['pushed_at'] or '')[:10]}  {s}  {fit}"
              + (f"  [catalog: {x['in_catalog']}]" if x["in_catalog"] else ""))
    cm.audit("recipe-discover", found=len(d["items"]), deployable=sum(1 for x in d["items"] if x["slots"]))
