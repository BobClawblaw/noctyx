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
        "version": "3.15.1", "repo": "prometheus/prometheus",
        "asset": "prometheus-{v}.{os}-{arch}.tar.gz",
        "sums": {
            "darwin_amd64": "c89fcb30045d8c394c9d93e96cbbee3aaf2448a48326b417e811eb4e528edf88",
            "darwin_arm64": "0d4d3d9837d27e4fcd0bd16d115b1b8d6732a4bf3e69107f1190d9ef653ae471",
            "linux_amd64": "6a726885328ad9efebbf36e876ce2fb6225468a71fd1e7ce3ba666d5f4d97a84",
            "linux_arm64": "ceb6d21493c37ca0b4513b147642797c68cf39a0f50a444d50e1e80c74f21b26"}},
    "alertmanager": {
        "version": "0.34.1", "repo": "prometheus/alertmanager",
        "asset": "alertmanager-{v}.{os}-{arch}.tar.gz",
        "sums": {
            "darwin_amd64": "9f3a3806c81a2ef3546fea9bae3dd39abcf2206edd112c9433133a9355875fce",
            "darwin_arm64": "a3941879f340ef12a4cc1a479c82bd8520611265ea8d1ee25c88bd5f944d8293",
            "linux_amd64": "265b9d1e55ef0d5306a436018af6d2b686c2ce051f03d968f7464ecb1372a7e8",
            "linux_arm64": "d98d6cbaf52151c7e76e24355fec88b11cebcb9875d4cdd8b76ddce7a7e5535c"}},
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
# bins: where the component may live and who put it there, first match wins: "noctyx" (installed and pinned by
# Noctyx, upgraded by Noctyx), "distro" (apt: compared with upstream, upgraded with apt), "brew" (likewise).
# adopted: the distro unit's drop-in that runs Noctyx's pinned binary instead (noctyx upgrade took it over):
# that row is "adopted", pinned like Noctyx's own. optional: no row where it is not installed.
COMPONENTS = [
    {"name": "noctyx", "role": "head", "repo": "BobClawblaw/noctyx", "upgrade": "pip install the new release (see the upgrade guide)"},
    {"name": "restic", "role": "both", "arg": "version", "pkg": "restic",
     "bins": [("/usr/local/bin/restic", "noctyx"), ("/usr/bin/restic", "distro"), ("/opt/homebrew/bin/restic", "brew")],
     "upgrade_head": "sudo noctyx setup tools --force", "upgrade_node": "sudo noctyx install-agent --host {host}"},
    {"name": "rest-server", "role": "head", "arg": "--version",
     "bins": [("/usr/local/bin/rest-server", "noctyx")], "upgrade_head": "sudo noctyx setup tools --force"},
    {"name": "prometheus", "role": "head", "arg": "--version", "pkg": "prometheus",
     "adopted": ("/etc/systemd/system/prometheus.service.d/noctyx.conf", "/usr/local/bin/prometheus"),
     "bins": [("/usr/local/bin/prometheus", "noctyx"), ("/usr/bin/prometheus", "distro"), ("/opt/homebrew/bin/prometheus", "brew")],
     "upgrade_head": "sudo noctyx setup metrics --force"},
    {"name": "alertmanager", "role": "head", "arg": "--version", "pkg": "prometheus-alertmanager", "optional": True,
     "adopted": ("/etc/systemd/system/prometheus-alertmanager.service.d/noctyx.conf", "/usr/local/bin/alertmanager"),
     "bins": [("/usr/local/bin/alertmanager", "noctyx"), ("/usr/bin/prometheus-alertmanager", "distro"),
              ("/opt/homebrew/bin/alertmanager", "brew")]},
    {"name": "node_exporter", "role": "both", "arg": "--version", "pkg": "prometheus-node-exporter",
     "adopted": ("/etc/systemd/system/prometheus-node-exporter.service.d/noctyx.conf", "/usr/local/bin/node_exporter"),
     "bins": [("/usr/local/bin/node_exporter", "noctyx"), ("/usr/bin/prometheus-node-exporter", "distro"),
              ("/opt/homebrew/bin/node_exporter", "brew")],
     "upgrade_head": "sudo noctyx setup metrics --force", "upgrade_node": "sudo noctyx tool install-node-metrics --host {host}"},
    {"name": "agent", "role": "node", "probe": "f=@AGENT_BIN@; [ -r $f ] && echo \"$(md5sum $f | cut -c1-12) noctyx\""},
]


def probe_snippet(comp):
    """Shell printing `VERSION SOURCE` for a component (nothing when it is not installed)."""
    if comp.get("probe"):
        return comp["probe"]
    ver = lambda p: f"$({p} {comp['arg']} 2>&1 | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)"  # noqa: E731
    tries = [f"[ -x {p} ] && {{ echo \"{ver(p)} {src}\"; exit 0; }}" for p, src in comp["bins"]]
    if comp.get("adopted"):
        d, b = comp["adopted"]
        tries.insert(0, f"[ -f {d} ] && [ -x {b} ] && {{ echo \"{ver(b)} adopted\"; exit 0; }}")
    return "( " + "; ".join(tries) + "; true )"


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
    release exists than the pin, or than what the distro installed) | ahead (newer than the pin: someone
    upgraded by hand). pinned is None for what Noctyx did not install itself."""
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


def upgrade_cmd(comp, host, is_head, source="noctyx"):
    if source == "distro":
        # the distro's archive may lag upstream: this takes what it carries
        return f"sudo apt-get install --only-upgrade {comp.get('pkg', comp['name'])}" + ("" if is_head else f"   (on {host['name']})")
    if source == "adopted":
        return f"sudo noctyx upgrade --host {host['name']} --component {comp['name']}"
    if source == "brew":
        return f"brew upgrade {comp.get('pkg', comp['name'])}" + ("" if is_head else f"   (on {host['name']})")
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
        snippet = "".join(f"echo '=={x['name']}=='; {probe_snippet(x)}\n" for x in comps if x.get("probe") or x.get("bins"))
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
            if x["name"] == "noctyx":
                inst, source = version, "noctyx"
            else:
                inst, _, source = (got.get(x["name"]) or "").partition(" ")
                inst, source = norm(inst), source.strip() or "noctyx"
            if x.get("optional") and not inst and not err:
                continue
            pin_here = pin if source in ("noctyx", "adopted") and x["name"] not in ("noctyx", "agent") else None
            if err and x["name"] != "noctyx":
                st, why = "unknown", f"unreachable: {err}"
            else:
                st, why = status(x, inst, pin_here, up, want_agent)
                if source in ("distro", "brew") and st != "missing":
                    why += f" ({'apt' if source == 'distro' else 'Homebrew'} manages it)"
            rows.append({"component": x["name"], "installed": inst or None, "pinned": pin_here, "latest": up,
                         "source": source, "state": st, "detail": why, "upgrade": upgrade_cmd(x, h, h is head, source)})
        out[h["name"]] = rows
    return {"ts": time.time(), "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "hosts": out}


# ── acting on a row: Noctyx's own upgrade, or skipping a release ─────────────
def fix(row, host, is_head):
    """How Noctyx itself brings this row up to date -> {"argv": [noctyx args], "what": ...}, or {"why": ...} when
    it cannot (only Noctyx's own pin or agent is ever installed: what is newer upstream than the pin waits for a
    Noctyx release that moves it). None for a row that needs nothing."""
    if row.get("state") not in ("upgrade", "behind", "missing"):
        return None
    name, inst, src = row["component"], row.get("installed"), row.get("source")
    linux = host.get("os", "linux") == "linux"
    if name == "agent":
        return {"argv": ["install-agent", "--host", host["name"]], "what": f"reinstall the backup agent on {host['name']}"}
    if name == "noctyx":
        return {"why": "Noctyx itself is upgraded with pip (see the upgrade guide), not from the console"}
    pin = PINS.get(name, {}).get("version")
    if not pin or (inst and not newer(pin, inst)):
        return {"why": f"Noctyx pins {name} {pin}: {row.get('latest')} comes with a Noctyx release that moves the pin"
                       if pin else f"Noctyx does not install {name}"}
    if name == "node_exporter" and linux and src in ("distro", "noctyx", "adopted"):
        if is_head and src == "noctyx":     # `setup metrics`'s own exporter beside its own Prometheus
            return {"argv": ["setup", "metrics", "--force"], "what": f"install Noctyx's node_exporter {pin} on the head"}
        return {"argv": ["upgrade", "--host", host["name"], "--component", name], "change": name,
                "what": f"install Noctyx's node_exporter {pin} on {host['name']}"
                        + (" in place of the distro's (same unit, same collectors)" if src == "distro" else "")
                        + ", checked and rolled back if it does not work"}
    if name in ("prometheus", "alertmanager") and is_head and linux and src in ("distro", "adopted"):
        trial = name == "prometheus" and inst and _vtuple(inst)[0] != _vtuple(pin)[0]
        return {"argv": ["upgrade", "--host", host["name"], "--component", name], "change": name,
                "what": f"run Noctyx's {name} {pin} in the head's {'distro ' if src == 'distro' else ''}unit"
                        + (f": a {inst} → {pin} trial beside it first, then the switch" if trial else "")
                        + ", checked and rolled back if it does not work"}
    if name == "restic" and src == "noctyx":
        return {"argv": ["setup", "tools", "--force"] if is_head else ["install-agent", "--host", host["name"]],
                "what": f"install restic {pin} on {host['name']}"}
    if name in ("rest-server", "prometheus") and src == "noctyx" and is_head:
        return {"argv": ["setup", "tools" if name == "rest-server" else "metrics", "--force"],
                "what": f"install {name} {pin} on the head"}
    return {"why": f"{name} here comes from {'apt' if src == 'distro' else 'Homebrew' if src == 'brew' else src}, which "
                   "Noctyx does not replace"}


def skip_key(host, component):
    return f"{host}/{component}"


def apply_skips(inv, skips):
    """Rows whose newer release the operator chose to skip read `skipped` (no alert) until a later one appears.
    skips: {"host/component" or "*/component": {"release", "by", "ts"}}."""
    for host, rows in (inv.get("hosts") or {}).items():
        for r in rows:
            s = skips.get(skip_key(host, r["component"])) or skips.get(skip_key("*", r["component"]))
            if s and r.get("state") == "upgrade" and norm(r.get("latest")) == norm(s.get("release")):
                r["state"], r["detail"] = "skipped", f"{s['release']} skipped" + (f" by {s['by']}" if s.get("by") else "")
    return inv


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}
