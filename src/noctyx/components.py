"""The critical components on every machine, in one registry: what Noctyx pins, where each runs, how to read
the installed version, and where new releases appear. `setup tools` and `setup metrics` install from it;
`noctyx versions` and the console report it per node, and the nightly run turns "a newer release exists" or
"this node is behind the pin" into a metric the alert rules notify on.

    noctyx versions [--host NAME] [--refresh] [--json]

A component that is behind is upgraded by the command its row names: the head's binaries with
`noctyx setup tools --force` / `noctyx setup metrics --force` after the pin moves, a node's agent and restic
with `noctyx install-agent --host NAME`.
"""
import datetime
import json
import pathlib
import re
import time
import urllib.request

# pinned releases and their SHA-256 sums (from each release's own sums file). A download that does not match
# is refused. Moving a pin = new version + new sums together, in one commit.
PINS = {
    "restic": {
        "version": "0.19.1", "repo": "restic/restic",
        "asset": "restic_{v}_{os}_{arch}.bz2",
        "sums": {
            "darwin_amd64": "c38d579622cf602f665234c5a8c315030b6cf70656028fe6dc29a786b60e5f35",
            "darwin_arm64": "7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143",
            "linux_amd64": "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
            "linux_arm64": "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465"}},
    "rest-server": {
        "version": "0.14.0", "repo": "restic/rest-server",
        "asset": "rest-server_{v}_{os}_{arch}.tar.gz",
        "sums": {
            "darwin_amd64": "ced47a785aec6a9fa3041cfa799dcc6c55527a12944d7a8147fc1595b5b0e945",
            "darwin_arm64": "057004ce24ca9edfbe0e391eae87981ab03f2abb6b65cf8fdfcddf847ded8656",
            "linux_amd64": "4c9c95bc079a0334e81fad379b19dc5c3353c71c2c88d652cafce2081c2b1c66",
            "linux_arm64": "cef139cbe8b27b16bda731d17f093b0aa466b8c60b136c12d78b6f2bff3daf22"}},
    "prometheus": {
        "version": "3.15.0", "repo": "prometheus/prometheus",
        "asset": "prometheus-{v}.{os}-{arch}.tar.gz",
        "sums": {
            "darwin_amd64": "2d79e744c2d7e505db936fbc898e05abc74fcb6e437c25befd26e9c9f00aa58b",
            "darwin_arm64": "920df4d17e78b3b0175af144eb318b0c74d1cf7b1d1251b326966f0e81977260",
            "linux_amd64": "2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0",
            "linux_arm64": "f1f90ec08e849d494ca66c611470afc50192f0355f1a61c33f2cbde02d067823"}},
    "node_exporter": {
        "version": "1.12.1", "repo": "prometheus/node_exporter",
        "asset": "node_exporter-{v}.{os}-{arch}.tar.gz",
        "sums": {
            "darwin_amd64": "3506a6d788b768a3c2cd1f58dc97b9ff3fecc5ac9196623d582982db2aa9d0f6",
            "darwin_arm64": "35b246a34af55a8f0c3f11067f17aecf9836cfd4689bde364776b3a4ad3a84d6",
            "linux_amd64": "b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63",
            "linux_arm64": "ad35b605f9954b9f1ffddf5ba054bdc5a98d790b9eae5291e1eeb83f1ecbd0e7"}},
}

# what each machine runs. role: head = the head only; node = every Linux host Noctyx backs up; both.
# probe: a shell snippet printing the installed version (empty = not installed); run on that machine as root.
COMPONENTS = [
    {"name": "noctyx", "role": "head", "repo": "BobClawblaw/noctyx", "upgrade": "noctyx upgrade guide (pip install the new release)"},
    {"name": "restic", "role": "both", "probe": "restic version 2>/dev/null | awk '{print $2}'",
     "upgrade_head": "sudo noctyx setup tools --force", "upgrade_node": "sudo noctyx install-agent --host {host}"},
    {"name": "rest-server", "role": "head", "probe": "/usr/local/bin/rest-server --version 2>/dev/null | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1",
     "upgrade_head": "sudo noctyx setup tools --force"},
    {"name": "prometheus", "role": "head", "probe": "/usr/local/bin/prometheus --version 2>&1 | awk 'NR==1{print $3}'",
     "upgrade_head": "sudo noctyx setup metrics --force"},
    {"name": "node_exporter", "role": "both",
     "probe": "(/usr/local/bin/node_exporter --version 2>&1 || prometheus-node-exporter --version 2>&1) | awk 'NR==1{print $3}'",
     "upgrade_head": "sudo noctyx setup metrics --force", "upgrade_node": "sudo noctyx tool install-node-metrics --host {host}"},
    {"name": "agent", "role": "node", "probe": "md5sum @AGENT_BIN@ 2>/dev/null | cut -c1-12",
     "upgrade_node": "sudo noctyx install-agent --host {host}"},
]


def plat(os_name, arch):
    return f"{'darwin' if os_name == 'macos' else 'linux'}_{ {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(arch, arch)}"


def asset(name, os_name, arch):
    """-> (file name, url, sha256) of a pinned component for a platform."""
    p = PINS[name]
    o, a = plat(os_name, arch).split("_")
    f = p["asset"].format(v=p["version"], os=o, arch=a)
    return f, f"https://github.com/{p['repo']}/releases/download/v{p['version']}/{f}", p["sums"][f"{o}_{a}"]


def norm(v):
    return (v or "").strip().lstrip("v")


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", norm(v))[:3]) or (0,)


def newer(a, b):
    """Is version a newer than b?"""
    return _vtuple(a) > _vtuple(b)


# ── upstream releases, cached a day ─────────────────────────────────────────
def latest(repo, cache, max_age=86400):
    """The newest non-prerelease tag of a GitHub repo (cached); None when GitHub cannot be reached."""
    hit = cache.get(repo)
    if hit and time.time() - hit.get("ts", 0) < max_age:
        return hit.get("tag")
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "noctyx"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        tag = norm(d.get("tag_name"))
    except Exception:
        return (hit or {}).get("tag")
    cache[repo] = {"tag": tag, "ts": time.time()}
    return tag


# ── the inventory ───────────────────────────────────────────────────────────
def applies(comp, host, head_name):
    is_head = host["name"] == head_name
    linux_node = host.get("os", "linux") == "linux" and host.get("repo") and host.get("backup", True) is not False
    return (comp["role"] in ("head", "both") and is_head) or (comp["role"] in ("node", "both") and linux_node)


def status(comp, installed, pinned, upstream, want_agent=None):
    """-> (state, detail): ok | missing | behind (older than the pin, or a stale agent) | upgrade (a newer
    release exists than the pin) | ahead (newer than the pin: someone upgraded by hand)."""
    if comp["name"] == "agent":
        if not installed:
            return "missing", "not installed"
        return ("ok", "current") if installed == want_agent else ("behind", "older than this Noctyx's agent")
    if not installed:
        return "missing", "not installed"
    if pinned and newer(pinned, installed):
        return "behind", f"older than the pin {pinned}"
    if upstream and newer(upstream, pinned or installed):
        return "upgrade", f"{upstream} released"
    if pinned and newer(installed, pinned):
        return "ahead", f"newer than the pin {pinned}"
    return "ok", "current"


def upgrade_cmd(comp, host, is_head):
    key = "upgrade_head" if is_head else "upgrade_node"
    return (comp.get(key) or comp.get("upgrade") or "").format(host=host["name"])


def inventory(c, probe, cache, want_agent=None, version=None, only=None):
    """Every critical component on every machine. probe(host, snippet) -> stdout of the snippet run on that
    machine as root (locally on the head). -> {"ts", "hosts": {name: [row, …]}}"""
    s = c["server"]
    head = next((h for h in c.get("hosts", []) if h.get("local") or h.get("address") == s.get("address")),
                {"name": s.get("hostname") or "head", "address": s.get("address"), "local": True})
    hosts = [head] + [h for h in c.get("hosts", []) if h is not head and h.get("enabled", True) is not False]
    out = {}
    for h in hosts:
        if only and h["name"] != only:
            continue
        rows = []
        comps = [x for x in COMPONENTS if applies(x, h, head["name"])]
        snippet = "".join(f"echo '=={x['name']}=='; {x['probe']}\n" for x in comps if x.get("probe"))
        got, err = {}, None
        try:
            txt = probe(h, snippet) if snippet else ""
            sec = None
            for line in txt.splitlines():
                m = re.fullmatch(r"==(.+)==", line.strip())
                if m:
                    sec = m.group(1)
                elif sec and line.strip() and sec not in got:
                    got[sec] = line.strip()
        except Exception as e:
            err = str(e)[:160]
        for x in comps:
            pin = PINS.get(x["name"], {}).get("version")
            if x["name"] == "restic":
                pin = str(c.get("restic_version") or pin)
            repo = PINS.get(x["name"], {}).get("repo") or x.get("repo")
            up = latest(repo, cache) if repo else None
            inst = version if x["name"] == "noctyx" else norm(got.get(x["name"]))
            if err and x["name"] != "noctyx":
                st, why = "unknown", f"unreachable: {err}"
            else:
                st, why = status(x, inst, None if x["name"] in ("noctyx", "agent") else pin, up, want_agent)
            rows.append({"component": x["name"], "installed": inst or None, "pinned": pin, "latest": up,
                         "state": st, "detail": why, "upgrade": upgrade_cmd(x, h, h is head)})
        out[h["name"]] = rows
    return {"ts": time.time(), "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "hosts": out}


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}
