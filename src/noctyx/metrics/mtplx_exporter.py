#!/usr/bin/env python3
"""Prometheus exporter for MTPLX (Apple-silicon LLM server) — translates its
JSON /metrics (per-request records in `latest` + `recent`) into the vLLM metric
names the Noctyx console already queries, so a Mac shows up in the LLM views
exactly like a vLLM node.

Serves http://127.0.0.1:9411/metrics?host=<name>. Which hosts to poll comes
from llm-topology.json (engine "mtplx", written by llm-discover); the API key
from <llm_keys>/<host>. Each request id is counted once; the
first poll seeds the seen-set so history is not replayed into the counters.
"""
import json, os, pathlib, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from noctyx.paths import P   # noqa: E402

TOPO = f"{P['console_state']}/llm-topology.json"
KEYS = P["llm_keys"]
PORT = int(os.environ.get("MTPLX_EXPORTER_PORT", "9411"))
POLL = 5
# vLLM-style buckets
B_TIME = [0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0, 40.0, 80.0, 160.0, 640.0, 2560.0]
B_TOK = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 150000, 200000, 250000, 300000, 400000, 500000, 1000000]


class Hist:
    def __init__(self, buckets):
        self.b, self.counts, self.sum, self.count = buckets, [0] * len(buckets), 0.0, 0
    def observe(self, v, n=1):
        if v is None:
            return
        for i, le in enumerate(self.b):
            if v <= le:
                self.counts[i] += n
        self.sum += v * n; self.count += n
    def lines(self, name, lbl):
        out = [f'{name}_bucket{{{lbl},le="{le}"}} {c}' for le, c in zip(self.b, self.counts)]
        out.append(f'{name}_bucket{{{lbl},le="+Inf"}} {self.count}')
        out += [f"{name}_sum{{{lbl}}} {self.sum}", f"{name}_count{{{lbl}}} {self.count}"]
        return out


class HostState:
    def __init__(self, host, base, key):
        self.host, self.base, self.key = host, base, key
        self.seen, self.seeded, self.model = set(), False, None
        self.c = {"prompt_tokens": 0, "generation_tokens": 0, "requests": 0, "cache_hits": 0, "cache_queries": 0,
                  "drafted": 0, "accepted": 0}
        self.h = {"ttft": Hist(B_TIME), "e2e": Hist(B_TIME), "prefill": Hist(B_TIME), "decode": Hist(B_TIME),
                  "queue": Hist(B_TIME), "itl": Hist(B_TIME), "ptok": Hist(B_TOK), "gtok": Hist(B_TOK)}
        self.g = {"kv_pct": None, "started": None, "context_len": None, "kv_capacity": None, "last_decode_tps": None,
                  "up": 0, "running": 0}
        self.lock = threading.Lock()

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.key}"} if self.key else {})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def poll(self):
        try:
            m = self.get("/metrics")
            try:
                h = self.get("/health")
                self.g["started"] = ((h.get("startup") or {}).get("started_at"))
                self.model = h.get("model") or self.model
            except Exception:
                pass
            recs = list(m.get("recent") or [])
            if m.get("latest"):
                recs.append(m["latest"])
            with self.lock:
                self.g["up"] = 1
                for r in recs:
                    rid = r.get("request_id") or f"{r.get('completed_at_s')}-{r.get('completion_tokens')}"
                    if rid in self.seen:
                        continue
                    self.seen.add(rid)
                    if not self.seeded:
                        continue
                    self.model = r.get("served_model_id") or self.model
                    pt, ct = r.get("prompt_tokens") or 0, r.get("completion_tokens") or 0
                    self.c["prompt_tokens"] += pt; self.c["generation_tokens"] += ct; self.c["requests"] += 1
                    self.c["cache_hits"] += r.get("cached_tokens") or 0; self.c["cache_queries"] += pt
                    self.c["drafted"] += r.get("drafted_tokens") or 0; self.c["accepted"] += r.get("accepted_drafts") or 0
                    self.h["ttft"].observe(r.get("ttft_s")); self.h["e2e"].observe(r.get("request_elapsed_s"))
                    self.h["prefill"].observe((r.get("prompt_eval_time_s") or 0) + (r.get("cache_restore_time_s") or 0))
                    self.h["decode"].observe(r.get("decode_elapsed_s")); self.h["queue"].observe(r.get("lock_wait_time_s"))
                    if ct and r.get("decode_elapsed_s"):
                        self.h["itl"].observe(r["decode_elapsed_s"] / ct, n=ct)   # per-token, weighted
                    self.h["ptok"].observe(pt); self.h["gtok"].observe(ct)
                    self.g["last_decode_tps"] = r.get("decode_tok_s")
                    self.g["context_len"] = r.get("context_len")
                    self.g["kv_capacity"] = r.get("paged_kv_capacity_tokens")
                    wc = r.get("prefill_wide_chunk") or {}
                    if wc.get("threshold_bytes") and r.get("cache_memory_bytes") is not None:
                        self.g["kv_pct"] = min(1.0, r["cache_memory_bytes"] / wc["threshold_bytes"])
                self.seeded = True
                if len(self.seen) > 5000:
                    self.seen = set(list(self.seen)[-2000:])
        except Exception as e:
            with self.lock:
                was, self.g["up"] = self.g["up"], 0
            if was != 0 or not getattr(self, "_err_logged", False):   # once per outage, not every 5 s
                print(f"{self.host}: poll failed against {self.base}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                self._err_logged = True
            return
        if getattr(self, "_err_logged", False):
            print(f"{self.host}: polling {self.base} again", flush=True)
            self._err_logged = False

    def render(self):
        with self.lock:
            lbl = f'model_name="{self.model or "?"}",engine="0"'
            out = [f"up_mtplx{{host=\"{self.host}\"}} {self.g['up']}",
                   f"mtplx_upstream_info{{host=\"{self.host}\",upstream=\"{self.base}\"}} 1"]
            out += [f"vllm:prompt_tokens_total{{{lbl}}} {self.c['prompt_tokens']}",
                    f"vllm:generation_tokens_total{{{lbl}}} {self.c['generation_tokens']}",
                    f"vllm:request_success_total{{{lbl},finished_reason=\"stop\"}} {self.c['requests']}",
                    f"vllm:prefix_cache_hits_total{{{lbl}}} {self.c['cache_hits']}",
                    f"vllm:prefix_cache_queries_total{{{lbl}}} {self.c['cache_queries']}",
                    f"vllm:num_requests_running{{{lbl}}} {self.g['running']}",
                    f"vllm:num_requests_waiting{{{lbl}}} 0",
                    f"mtplx_drafted_tokens_total{{{lbl}}} {self.c['drafted']}",
                    f"mtplx_accepted_drafts_total{{{lbl}}} {self.c['accepted']}"]
            if self.g["kv_pct"] is not None:
                out.append(f"vllm:kv_cache_usage_perc{{{lbl}}} {self.g['kv_pct']:.4f}")
            if self.g["last_decode_tps"] is not None:
                out.append(f"mtplx_last_decode_tokens_per_second{{{lbl}}} {self.g['last_decode_tps']}")
            if self.g["started"]:
                out.append(f"process_start_time_seconds {self.g['started']}")
            if self.g["kv_capacity"]:
                out.append(f'vllm:cache_config_info{{{lbl},num_gpu_blocks="{int(self.g["kv_capacity"])}",block_size="1",gpu_memory_utilization="",cache_dtype="paged"}} 1')
            for k, name in (("ttft", "vllm:time_to_first_token_seconds"), ("e2e", "vllm:e2e_request_latency_seconds"),
                            ("prefill", "vllm:request_prefill_time_seconds"), ("decode", "vllm:request_decode_time_seconds"),
                            ("queue", "vllm:request_queue_time_seconds"), ("itl", "vllm:inter_token_latency_seconds"),
                            ("ptok", "vllm:request_prompt_tokens"), ("gtok", "vllm:request_generation_tokens")):
                out += self.h[k].lines(name, lbl)
            return "\n".join(out) + "\n"


STATES: dict[str, HostState] = {}


def discover():
    try:
        topo = json.load(open(TOPO))
    except Exception:
        return
    for hn, t in (topo.get("hosts") or {}).items():
        for srv in t.get("servers", []):
            if srv.get("engine") == "mtplx" and srv.get("upstream"):
                if hn in STATES and STATES[hn].base != srv["upstream"]:
                    print(f"{hn}: upstream {STATES[hn].base} -> {srv['upstream']}", flush=True)
                    STATES[hn].base = srv["upstream"]        # the Mac moved address (DHCP)
                if hn not in STATES:
                    key = None
                    try:
                        key = open(f"{KEYS}/{hn}").read().strip()
                    except Exception:
                        pass
                    STATES[hn] = HostState(hn, srv["upstream"], key)
    for hn in [h for h in STATES if h not in (topo.get("hosts") or {})]:
        STATES.pop(hn, None)


def loop():
    while True:
        discover()
        for st in list(STATES.values()):
            st.poll()
        time.sleep(POLL)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
        if path != "/metrics":
            self.send_response(404); self.end_headers(); return
        host = q.get("host")
        body = "".join(s.render() for hn, s in STATES.items() if not host or hn == host).encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
