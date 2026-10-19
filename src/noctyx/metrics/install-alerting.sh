#!/usr/bin/env bash
# Noctyx alerting: Prometheus scrapes the UI's /metrics, evaluates
# rules.yml, Alertmanager delivers to the UI webhook (+ optional
# external channel). Idempotent; run as root on the head node.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
PY=/etc/prometheus/prometheus.yml

command -v prometheus-alertmanager >/dev/null || \
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q prometheus-alertmanager >/dev/null

install -m 0644 "$HERE/rules.yml" /etc/prometheus/@UNIT_PREFIX@-rules.yml
promtool check rules /etc/prometheus/@UNIT_PREFIX@-rules.yml >/dev/null

# rules + scrape job in prometheus.yml (append-once, like install-node-metrics.sh)
if ! grep -q "@UNIT_PREFIX@-rules.yml" "$PY"; then
  python3 - "$PY" <<'EOP'
import re, sys
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"^rule_files:\s*\n(?!  -)", "rule_files:\n  - /etc/prometheus/@UNIT_PREFIX@-rules.yml\n", s, count=1, flags=re.M)
if "@UNIT_PREFIX@-rules.yml" not in s:
    s = re.sub(r"^rule_files:\s*\n", "rule_files:\n  - /etc/prometheus/@UNIT_PREFIX@-rules.yml\n", s, count=1, flags=re.M)
open(p, "w").write(s)
EOP
fi
if ! grep -q "job_name: @PROM_JOB_CONSOLE@$" "$PY"; then
  cat >> "$PY" <<'EOJ'

  # Noctyx's own state (backup health, RAID, endpoint, jobs) from the UI.
  - job_name: @PROM_JOB_CONSOLE@
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/@UNIT_PREFIX@-ca.crt
    static_configs:
      - targets: ['127.0.0.1:20996']
EOJ
fi
promtool check config "$PY" >/dev/null

# Alertmanager: base config + optional external receiver
AM=/etc/prometheus/alertmanager.yml
cp "$HERE/alertmanager.yml" "$AM.new"
CH=@ALERT_ENV@
if [[ -r $CH ]]; then
  # shellcheck disable=SC1090
  . "$CH"
  case ${CHANNEL:-} in
    ntfy)   # NTFY_URL=https://ntfy.sh/<topic>  [NTFY_TOKEN=...]
      python3 - "$AM.new" "$NTFY_URL" "${NTFY_TOKEN:-}" <<'EOP'
import sys
p, url, tok = sys.argv[1:4]
s = open(p).read().replace("  receiver: console\n", "  receiver: all\n")
s += ("- name: all\n  webhook_configs:\n  - url: https://127.0.0.1:20996/api/v1/alert-hook\n    send_resolved: true\n"
      "    http_config:\n      tls_config:\n        ca_file: @TLS@/ca.crt\n"
      f"  - url: {url}\n    send_resolved: true\n")
if tok:
    s += f"    http_config:\n      authorization:\n        credentials: {tok}\n"
open(p, "w").write(s)
EOP
      ;;
    email)  # SMTP_HOST=smtp.example:587 SMTP_FROM=... SMTP_USER=... SMTP_PASS=... ALERT_TO=...
      python3 - "$AM.new" "$SMTP_HOST" "$SMTP_FROM" "${SMTP_USER:-}" "${SMTP_PASS:-}" "$ALERT_TO" <<'EOP'
import sys
p, host, frm, user, pw, to = sys.argv[1:7]
s = open(p).read().replace("  receiver: console\n", "  receiver: all\n")
s += ("- name: all\n  webhook_configs:\n  - url: https://127.0.0.1:20996/api/v1/alert-hook\n    send_resolved: true\n"
      "    http_config:\n      tls_config:\n        ca_file: @TLS@/ca.crt\n"
      f"  email_configs:\n  - to: {to}\n    from: {frm}\n    smarthost: {host}\n    send_resolved: true\n")
if user:
    s += f"    auth_username: {user}\n    auth_password: {pw}\n"
open(p, "w").write(s)
EOP
      ;;
  esac
fi
install -m 0640 -o root -g prometheus "$AM.new" "$AM"; rm -f "$AM.new"
amtool check-config "$AM" >/dev/null
systemctl enable --now prometheus-alertmanager >/dev/null
systemctl restart prometheus-alertmanager
systemctl reload prometheus
sleep 3
echo "alertmanager: $(systemctl is-active prometheus-alertmanager) · receivers: $(grep -c '^- name:' "$AM")"
echo "prometheus: rules $(promtool check rules /etc/prometheus/@UNIT_PREFIX@-rules.yml | grep -oE '[0-9]+ rules found')"
