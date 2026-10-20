#!/usr/bin/env python3
"""Find LLM inference servers on every node and hand them to Prometheus.

Every 5 min (the llm-discover timer): ssh each host in the site config (the head node
locally), read the command lines of vllm / sglang / llama-server processes and
the ports they (or their containers) listen on, probe /metrics from here, and
write
  /etc/prometheus/llm-targets.json      file_sd targets for job "llm"
  <console_state>/llm-topology.json   what runs where, incl. HEADLESS
        tensor-parallel workers (no HTTP of their own — the UI shows them as
        "worker for <head>")
Models get restarted on other ports; nothing here is hard-coded per host.
"""
import json, os, pathlib, re, socket, subprocess, sys, time, urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from noctyx import orchestrator as cm    # noqa: E402
from noctyx.paths import P               # noqa: E402

TARGETS = P["prom_llm_targets"]
TOPO = f"{P['console_state']}/llm-topology.json"
PREFIX = {"vllm": "vllm", "sglang": "sglang", "llamacpp": "llamacpp", "tgi": "tgi", "tensorfold": "tensorfold"}
KEYS = P["llm_keys"]                     # per-host API key for servers that require one (MTPLX)
MTPLX_EXPORTER = "127.0.0.1:9411"        # translates MTPLX's JSON metrics (metrics/mtplx_exporter.py)
NODE_TARGETS = P["prom_node_targets"]   # the node_exporter job (:9100)

# Rule: ALWAYS Ethernet over Wi-Fi. A host named in the site config rather than addressed
# (the Mac: DHCP lease, Wi-Fi + Ethernet on the same subnet, `mac1.local` answers with either
# address — the Wi-Fi one went dark from the LAN the moment Ethernet was plugged in) is asked
# which IP its wired port has; that IP is pinned in cm.ADDR_CACHE for every ssh and scrape.
WIRED_MAC = r"""
networksetup -listallhardwareports | awk -F': ' '/^Hardware Port: /{p=$2} /^Device: /{if (p ~ /Ethernet/ && p !~ /Bridge/) print $2}' | while read -r d; do
  a=$(ipconfig getifaddr "$d" 2>/dev/null); [ -n "$a" ] && { echo "$a $d"; exit 0; }
done
d=$(scutil <<< 'show State:/Network/Global/IPv4' | awk '/PrimaryInterface/{print $3}')
echo "$(ipconfig getifaddr "$d" 2>/dev/null) $d"
"""
WIRED_LINUX = r"""
for d in $(ls /sys/class/net | grep -vE '^(lo|docker|veth|br-|virbr|wl)'); do
  [ -d /sys/class/net/$d/wireless ] && continue
  a=$(ip -o -4 addr show dev "$d" scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
  [ -n "$a" ] && { echo "$a $d"; exit 0; }
done
"""


def resolve_wired(c, h, cache):
    """Return {"ip","iface"} for a *named* host, preferring its wired interface, or None."""
    prev = (cache.get(h["name"]) or {}).get("ip")
    cands = [prev] if prev else []
    try:
        cands += [ai[4][0] for ai in socket.getaddrinfo(h["address"], None, socket.AF_INET)]
    except Exception:
        pass
    user = h.get("ssh_user") or c["server"]["ssh"]["user"]
    snippet = WIRED_MAC if h.get("os") == "macos" else WIRED_LINUX
    for ip in dict.fromkeys(x for x in cands if x):
        try:
            r = subprocess.run(["ssh", *cm.SSH_OPTS, "-o", "ConnectTimeout=4", "-i", c["server"]["ssh"]["key"],
                                f"{user}@{ip}", "bash -s"], input=snippet, capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        out = (r.stdout or "").strip().split()
        if r.returncode == 0 and out and cm._IPV4.match(out[0]):
            return {"ip": out[0], "iface": out[1] if len(out) > 1 else None, "asked": ip, "ts": time.time()}
    return None


def pin_addresses(c, hosts):
    """Resolve every named host, persist the pins, and keep the node_exporter target file in
    step (Prometheus re-reads file_sd on change). Returns hosts with `address` = the pinned IP."""
    try:
        cache = json.load(open(cm.ADDR_CACHE))
    except Exception:
        cache = {}
    out = []
    for h in hosts:
        if cm._IPV4.match(h["address"]):
            out.append(h)
            continue
        pin = resolve_wired(c, h, cache)
        if pin:
            if pin["ip"] != (cache.get(h["name"]) or {}).get("ip"):
                print(f"{h['name']}: {h['address']} -> {pin['ip']} ({pin['iface']})"
                      + ("" if not cache.get(h["name"]) else
                         f" — ADDRESS CHANGED: if node_exporter's cert lacks this IP, rerun install-mac-metrics.sh {h['name']}"))
            cache[h["name"]] = pin
        elif h["name"] in cache:
            cache[h["name"]]["stale"] = True             # unreachable now; keep the last pin
        out.append(dict(h, address=cm.addr(h) if h["name"] not in cache else cache[h["name"]]["ip"]))
    tmp = cm.ADDR_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, cm.ADDR_CACHE)
    head = c["server"]["address"]
    node = [{"targets": [f"{h['address']}:9100"], "labels": {"host": h["name"]}} for h in out if h["address"] != head]
    want = json.dumps(node, indent=2)
    try:
        cur = open(NODE_TARGETS).read()
    except Exception:
        cur = None
    if cur is not None and json.loads(cur) != node:
        open(NODE_TARGETS + ".tmp", "w").write(want + "\n")
        os.chmod(NODE_TARGETS + ".tmp", 0o644)
        os.replace(NODE_TARGETS + ".tmp", NODE_TARGETS)
        print(f"rewrote {NODE_TARGETS}")
    return out

SNIPPET_MAC = r"""
ps -axo pid=,command= | grep -E 'mtplx serve|mlx_lm.server|vllm serve|llama-server|ollama serve|tensorfold(-native)? serve' | grep -v grep | while read -r pid cmd; do echo "CMD $pid $cmd"; done
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print "LISTEN " $9}'
"""
COMMON_PORTS = [8000, 8888, 8080, 8081, 30000, 30001, 11434, 5000, 9000, 1234]

SNIPPET = r"""
for p in $(pgrep -f 'vllm serve|vllm\.entrypoints|sglang\.launch_server|llama-server|text-generation-launcher|ollama serve|tensorfold(-native)? serve' 2>/dev/null); do
  printf 'CMD %s ' "$p"; tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null; echo
done
ss -Hltn 2>/dev/null | awk '{print "LISTEN " $4}'
docker ps --format 'CTR {{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null
"""


def api_key(host):
    try:
        return open(f"{KEYS}/{host}").read().strip()
    except Exception:
        return None


def run_on(c, h):
    if h.get("local") and h.get("os") == "macos":      # a Mac head: here, not over ssh to itself
        return subprocess.run(["bash", "-s"], input=SNIPPET_MAC, capture_output=True, text=True, timeout=40).stdout
    if h.get("os") == "macos":
        r = subprocess.run(["ssh", *cm.SSH_OPTS, "-i", c["server"]["ssh"]["key"],
                            f"{h.get('ssh_user') or c['server']['ssh']['user']}@{h['address']}", "bash -s"],
                           input=SNIPPET_MAC, capture_output=True, text=True, timeout=40)
        return r.stdout
    if h["address"] == c["server"]["address"]:
        r = subprocess.run(["bash", "-c", SNIPPET], capture_output=True, text=True, timeout=30)
        return r.stdout
    r = subprocess.run(["ssh", *cm.SSH_OPTS, "-i", c["server"]["ssh"]["key"],
                        f"{c['server']['ssh']['user']}@{h['address']}", "sudo -n bash -s"],
                       input=SNIPPET, capture_output=True, text=True, timeout=40)
    return r.stdout


def parse_cmd(cmd):
    """engine, model, tp, headless, port(from cmdline) for one server process."""
    eng = ("mtplx" if ("mtplx serve" in cmd or "mtplx.server" in cmd) else
           "vllm" if re.search(r"vllm( serve|\.entrypoints)", cmd) else
           "sglang" if "sglang" in cmd else "llamacpp" if "llama-server" in cmd else
           "tgi" if "text-generation-launcher" in cmd else "ollama" if "ollama" in cmd else
           "tensorfold" if re.search(r"tensorfold(-native)? serve", cmd) else None)
    if not eng:
        return None
    def opt(name, default=None):
        m = re.search(rf"(?:^|\s)--{name}(?:[= ]([^\s]+))?", cmd)
        return (m.group(1) if m and m.group(1) else default) if m else None
    model = opt("served-model-name") or opt("model") or opt("model-path") or opt("hf-repo")
    if eng == "tensorfold":                      # `tensorfold[-native] serve <path> --name X --tp N --rank R`
        model = opt("name") or model
    if not model and eng in ("vllm", "tensorfold"):
        m = re.search(rf"{eng}(?:-native)? serve\s+(\S+)", cmd)
        model = m.group(1) if m else None
    if model and "/" in model and not model.startswith("http"):
        model = model.rstrip("/").split("/")[-1] if model.count("/") > 1 else model
    tp = opt("tensor-parallel-size") or opt("tp-size") or opt("tp") or "1"
    # tensorfold: rank 0 owns the HTTP port, ranks >0 are TP workers without one
    headless = "--headless" in cmd or (eng == "tensorfold" and (opt("rank") or "0") != "0")
    port = opt("port") or {"vllm": "8000", "sglang": "30000", "llamacpp": "8080", "tgi": "80", "ollama": "11434",
                           "mtplx": "8888", "tensorfold": "8000"}[eng]
    # tensorfold ranks of one model share --master/--master-port even when a worker has no --name
    master = f"{opt('master')}:{opt('master-port')}" if eng == "tensorfold" and opt("master") else None
    return {"engine": eng, "model": model, "tp": int(tp) if str(tp).isdigit() else 1,
            "headless": headless, "cmd_port": int(port) if str(port).isdigit() else None, "master": master}


def model_info(addr, port, timeout=4, key=None):
    """OpenAI-compatible /v1/models: context length + load time, if offered."""
    try:
        req = urllib.request.Request(f"http://{addr}:{port}/v1/models", headers={"Authorization": f"Bearer {key}"} if key else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        m = (d.get("data") or [{}])[0]
        return {"max_model_len": m.get("max_model_len") or m.get("context_length"), "loaded_ts": m.get("created"), "model": m.get("id")}
    except Exception:
        return {}


def probe(addr, port, timeout=4, key=None):
    try:
        req = urllib.request.Request(f"http://{addr}:{port}/metrics", headers={"Authorization": f"Bearer {key}"} if key else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(200000).decode("utf-8", "replace")
    except Exception:
        return None
    if head.lstrip().startswith("{") and '"latest"' in head:     # MTPLX: JSON, not Prometheus text
        try:
            mi = model_info(addr, port, key=key)
            return {"engine": "mtplx", "model": None, **mi}
        except Exception:
            return {"engine": "mtplx", "model": None}
    for eng, pre in PREFIX.items():
        if re.search(rf"^{pre}[:_]", head, re.M):
            m = re.search(r'model(?:_name)?="([^"]+)"', head)
            return {"engine": eng, "model": m.group(1) if m else None}
    return None


def main():
    c = cm.cfg()
    hosts = [h for h in c["hosts"] if h.get("enabled", True) is not False and h.get("address")]
    hosts = pin_addresses(c, hosts)
    targets, topo = [], {"ts": time.time(), "hosts": {}}
    for h in hosts:
        try:
            out = run_on(c, h)
        except Exception as e:
            topo["hosts"][h["name"]] = {"error": str(e)[:100], "servers": []}
            continue
        cmds = [parse_cmd(l[4:].split(" ", 1)[1]) for l in out.splitlines() if l.startswith("CMD ")]
        cmds = [x for x in cmds if x]
        listen = {int(l.rsplit(":", 1)[1]) for l in out.splitlines() if l.startswith("LISTEN ") and l.rsplit(":", 1)[1].isdigit()}
        ctr_ports = {}
        for l in out.splitlines():
            if l.startswith("CTR "):
                name, image, ports = (l[4:].split("|") + ["", ""])[:3]
                for m in re.finditer(r":(\d+)->(\d+)/tcp", ports):
                    ctr_ports[int(m.group(1))] = f"{name} ({image})"
        servers, seen_ports = [], set()
        # one process per (engine, model): TP=2 with mp backend shows the same cmdline twice
        uniq = {}
        for x in cmds:
            uniq.setdefault((x["engine"], x["model"], x["headless"]), x)
        for x in uniq.values():
            entry = dict(x, role="worker" if x["headless"] else "serving", port=None, target=None)
            if not x["headless"]:
                cands = [p for p in [x["cmd_port"]] if p] + sorted(ctr_ports) + [p for p in COMMON_PORTS if p in listen]
                key = api_key(h["name"])
                for p in dict.fromkeys(cands):
                    if p in seen_ports:
                        continue
                    hit = probe(h["address"], p, key=key)
                    if hit and hit["engine"] == x["engine"]:
                        mi = model_info(h["address"], p, key=key)
                        entry.update(port=p, model=hit["model"] or mi.get("model") or x["model"], container=ctr_ports.get(p),
                                     **{k: v for k, v in mi.items() if k != "model"})
                        if x["engine"] == "mtplx":
                            # scraped through the translating exporter on the head node
                            entry.update(target=MTPLX_EXPORTER, upstream=f"http://{h['address']}:{p}",
                                         scrape_params={"host": h["name"]})
                        else:
                            entry.update(target=f"{h['address']}:{p}")
                        seen_ports.add(p)
                        break
            servers.append(entry)
        # a launcher and its child (MTPLX: `mtplx serve` + `-m mtplx.server.openai`) both
        # match: keep the one that owns the port, drop portless twins of the same engine
        servers = [x for x in servers if x.get("target") or x["role"] == "worker"
                   or not any(y.get("target") and y["engine"] == x["engine"] for y in servers)]
        # metrics endpoints that no known process explains (e.g. only a container)
        for p, who in ctr_ports.items():
            if p not in seen_ports:
                hit = probe(h["address"], p)
                if hit:
                    servers.append({"engine": hit["engine"], "model": hit["model"], "tp": None, "headless": False,
                                    "role": "serving", "port": p, "target": f"{h['address']}:{p}", "container": who,
                                    **model_info(h["address"], p)})
                    seen_ports.add(p)
        topo["hosts"][h["name"]] = {"servers": servers}
        for s_ in servers:
            if s_.get("target"):
                labels = {"host": h["name"], "engine": s_["engine"], "port": str(s_["port"])}
                if s_["engine"] == "tensorfold" and s_.get("model"):
                    labels["model_name"] = s_["model"]   # tensorfold-native's metrics carry no model label
                for k, v in (s_.get("scrape_params") or {}).items():
                    labels[f"__param_{k}"] = v
                targets.append({"targets": [s_["target"]], "labels": labels})
    # TP workers: pair with the serving head of the same model
    serving = [(hn, s_) for hn, d in topo["hosts"].items() for s_ in d.get("servers", []) if s_["role"] == "serving"]
    heads = {(s_["engine"], s_["model"]): hn for hn, s_ in serving}
    by_master = {(s_["engine"], s_["master"]): (hn, s_) for hn, s_ in serving if s_.get("master")}
    for hn, d in topo["hosts"].items():
        for s_ in d.get("servers", []):
            if s_["role"] == "worker":
                s_["worker_for"] = heads.get((s_["engine"], s_["model"]))
                if not s_["worker_for"] and (s_["engine"], s_.get("master")) in by_master:
                    s_["worker_for"], head = by_master[(s_["engine"], s_["master"])]
                    s_["model"] = head["model"]
    os.makedirs(os.path.dirname(TOPO), exist_ok=True)
    for path, data in ((TARGETS, targets), (TOPO, topo)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    print(f"{len(targets)} scrape target(s): " + ", ".join(f"{t['labels']['host']}:{t['labels']['port']} {t['labels']['engine']}" for t in targets))
    for hn, d in topo["hosts"].items():
        for s_ in d.get("servers", []):
            print(f"  {hn:<9} {s_['role']:<8} {s_['engine']:<8} {s_.get('model') or '?':<28} tp={s_.get('tp')} "
                  + (f"port {s_['port']}" if s_.get("port") else f"worker for {s_.get('worker_for') or '?'}"))


if __name__ == "__main__":
    main()
