#!/usr/bin/env bash
# LLM monitoring: discover vLLM/SGLang/llama.cpp servers on every node (timer),
# scrape their /metrics with Prometheus (job "llm"), alert rules. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
PY=/etc/prometheus/prometheus.yml
install -m 0644 "$HERE/llm-discover.service" "$HERE/llm-discover.timer" "$HERE/mtplx-exporter.service" /etc/systemd/system/
systemctl enable --now mtplx-exporter >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable --now llm-discover.timer >/dev/null
systemctl start llm-discover.service
if ! grep -q "job_name: llm$" "$PY"; then
  cat >> "$PY" <<'EOJ'

  # LLM inference servers (vLLM/SGLang/llama.cpp /metrics), targets from
  # noctyx llm-discover (timer, every 5 min). Plain HTTP, LAN only.
  - job_name: llm
    scrape_interval: 15s
    scrape_timeout: 10s
    file_sd_configs:
      - files: ['/etc/prometheus/llm-targets.json']
EOJ
fi
promtool check config "$PY" >/dev/null
systemctl reload prometheus
# alert rules live in rules.yml (LLM group); (re)install them
"$HERE/install-alerting.sh" >/dev/null
echo "llm targets: $(python3 -c "import json;print(len(json.load(open('/etc/prometheus/llm-targets.json'))))") · prometheus reloaded"
