#!/usr/bin/env bash
# Noctyx metrics pipeline: node_exporter on every node -> Prometheus on the
# head node -> Noctyx UI. Run on the head node as root:
#   sudo noctyx tool install-node-metrics            # all enabled hosts
#   sudo noctyx tool install-node-metrics --host node1
# Idempotent. Per client it:
#   1. issues a TLS cert for the client's IP from the Noctyx CA
#   2. writes the exporter's TLS + basic-auth web config and ARGS FIRST, then
#      installs the packages with --force-confold, so the exporter never
#      listens unprotected (no client runs a firewall; a VPN may reach them too)
#   3. installs the GPU/Docker textfile collector (15 s timer), and runs Noctyx's pinned node_exporter
#      (checked against its SHA-256) in place of the distro's binary: the same unit, user and ARGS, with the
#      two defaults the distro build changes kept (the systemd collector on, textfiles from
#      /var/lib/prometheus/node-exporter)
#   4. proves an authenticated TLS scrape from here
# and finally (re)writes Prometheus's file_sd target list + scrape job.
#
# The head node itself keeps its pre-existing exporter/scrape (job "node",
# localhost:9100, set up before Noctyx); it only gains the textfile
# collector and a host="@HEAD_HOST@" label.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG=${NOCTYX_CONFIG:?run through: noctyx tool <name>}
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ONLY=""; [[ ${1:-} == --host ]] && ONLY=${2:?}

CA_CRT=@TLS@/ca.crt; CA_KEY=@TLS@/ca.key
PKI=@ETC@/metrics; install -d -m 0700 "$PKI"
PASS=/etc/prometheus/node-exporter.pass
PY3=${NX_PY:-python3}                 # Noctyx's own interpreter (a Mac's /usr/bin/python3 may be missing)
SSH_KEY=$("$PY3" -c "import json;print(json.load(open('$CFG'))['server']['ssh']['key'])")
SSH_USER=$("$PY3" -c "import json;print(json.load(open('$CFG'))['server']['ssh']['user'])")
HEAD=$("$PY3" -c "import json;print(json.load(open('$CFG'))['server']['address'])")

# one scrape credential for the fleet; bcrypt hash goes to the clients
# Prometheus reads the password: the distro's prometheus user, or the repository owner that runs
# Noctyx's own Prometheus (noctyx setup metrics)
PGROUP=prometheus; "$PY3" -c "import grp;grp.getgrnam('prometheus')" 2>/dev/null || PGROUP=@REPO_OWNER@
if [[ ! -s $PASS ]]; then
  install -d -m 0755 "$(dirname "$PASS")"
  install -m 0640 -o root -g "$PGROUP" /dev/null "$PASS"
  openssl rand -hex 24 > "$PASS"
fi
# bcrypt by htpasswd (on every Linux head and every Mac), the password on stdin, never a command line
HASH=$(tr -d '\n' < "$PASS" | htpasswd -niB -C 10 prometheus | cut -d: -f2-)
[[ $HASH == '$2'* ]] || { echo "could not hash the exporter password (htpasswd -B)" >&2; exit 1; }

HOSTS=()                               # bash 3.2 (a Mac head) has no mapfile
while IFS= read -r row; do HOSTS+=("$row"); done < <("$PY3" - "$CFG" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
for h in c["hosts"]:
    if h.get("enabled", True) is not False and h.get("address") and h.get("os", "linux") == "linux":
        print(h["name"], h["address"], h.get("arch") or "x86_64")
EOF
)

# Noctyx's pinned node_exporter, one download per architecture, checked against its pinned SHA-256
NE_DIR=$(mktemp -d); trap 'rm -rf "$NE_DIR"' EXIT
ne_bin() {
  local f=$NE_DIR/node_exporter.$1
  if [[ ! -s $f ]]; then
    "$PY3" - "$1" "$f" >&2 <<'PYEOF'
import sys
from noctyx import setup
open(sys.argv[2], "wb").write(setup._from_tar(setup._fetch("node_exporter", "linux", sys.argv[1]), ["node_exporter"])["node_exporter"])
PYEOF
  fi
  echo "$f"
}
NE_DROPIN=$HERE/node-exporter.conf    # the unit's drop-in (noctyx heal puts it back when it drifts)
pin_exporter() {   # the pinned binary and the unit's drop-in (the caller restarts the exporter)
  put 0755 "$(ne_bin "$ARCH")" /usr/local/bin/node_exporter
  put 0644 "$NE_DROPIN" /etc/systemd/system/prometheus-node-exporter.service.d/noctyx.conf
}

for row in "${HOSTS[@]}"; do
  read -r NAME ADDR ARCH <<<"$row"
  [[ -n $ONLY && $NAME != "$ONLY" ]] && continue
  E=(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" "$SSH_USER@$ADDR")
  put() { "${E[@]}" "sudo -n install -d '$(dirname "$3")' && sudo -n install -m '$1' -o root -g root /dev/stdin '$3'" < "$2"; }
  echo "== $NAME ($ADDR)"

  put 0755 "$HERE/node-textfile.sh" /usr/local/bin/@TEXTFILE_UNIT@
  put 0644 "$HERE/node-textfile.service" /etc/systemd/system/@TEXTFILE_UNIT@.service
  put 0644 "$HERE/node-textfile.timer" /etc/systemd/system/@TEXTFILE_UNIT@.timer

  if [[ $ADDR == "$HEAD" ]]; then
    "${E[@]}" "sudo -n systemctl daemon-reload && sudo -n systemctl enable --now @TEXTFILE_UNIT@.timer" >/dev/null
    if "${E[@]}" "systemctl cat prometheus-node-exporter.service" >/dev/null 2>&1; then
      # the distro's exporter (Noctyx's own, from `setup metrics`, is upgraded there): pinned binary, same scrape
      pin_exporter
      "${E[@]}" "sudo -n systemctl daemon-reload && sudo -n systemctl restart prometheus-node-exporter"
      sleep 2
      v=$(curl -s -m 30 http://127.0.0.1:9100/metrics | sed -n 's/^node_exporter_build_info{.*version="\([^"]*\)".*/\1/p')
      echo "   head node: textfile collector; node_exporter ${v:-?} (existing scrape kept)"
      [[ -n $v ]] || { echo "   !! the head's exporter does not answer on 127.0.0.1:9100" >&2; exit 1; }
    else
      echo "   head node: textfile collector only (existing exporter + scrape kept)"
    fi
    continue
  fi

  # 1. cert for this client's IP, signed by the Noctyx CA
  if [[ ! -s $PKI/$NAME.crt ]] || ! openssl x509 -in "$PKI/$NAME.crt" -noout -checkend 2592000 >/dev/null; then
    openssl req -new -newkey rsa:2048 -nodes -keyout "$PKI/$NAME.key" -subj "/O=Noctyx/CN=$NAME" \
      -out "$PKI/$NAME.csr" 2>/dev/null
    openssl x509 -req -in "$PKI/$NAME.csr" -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial -days 825 \
      -extfile <(printf 'subjectAltName=IP:%s,DNS:%s\nextendedKeyUsage=serverAuth\n' "$ADDR" "$NAME") \
      -out "$PKI/$NAME.crt" 2>/dev/null
    chmod 0600 "$PKI/$NAME.key"; rm -f "$PKI/$NAME.csr"
  fi

  # 2. protection BEFORE the package can start the exporter
  WEB=$(mktemp); ARGSF=$(mktemp)
  printf 'tls_server_config:\n  cert_file: /etc/prometheus/node-exporter.crt\n  key_file: /etc/prometheus/node-exporter.key\nbasic_auth_users:\n  prometheus: %s\n' "$HASH" > "$WEB"
  printf '# managed by Noctyx noctyx tool install-node-metrics\nARGS="--web.config.file=/etc/prometheus/node-exporter-web.yml"\n' > "$ARGSF"
  put 0644 "$PKI/$NAME.crt" /etc/prometheus/node-exporter.crt
  put 0600 "$PKI/$NAME.key" /etc/prometheus/node-exporter.key
  put 0644 "$WEB" /etc/prometheus/node-exporter-web.yml
  put 0644 "$ARGSF" /etc/default/prometheus-node-exporter
  rm -f "$WEB" "$ARGSF"

  pin_exporter

  # 3. packages (keep our /etc/default), then hand the key to the exporter user.
  #    The package starts the exporter before the chown: it fails closed (cannot
  #    read the key) and trips systemd's start limit — hence reset-failed.
  "${E[@]}" "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
      -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef \
      prometheus-node-exporter prometheus-node-exporter-collectors smartmontools nvme-cli >/dev/null \
    && sudo -n chown prometheus:prometheus /etc/prometheus/node-exporter.key \
    && sudo -n systemctl daemon-reload \
    && sudo -n systemctl reset-failed prometheus-node-exporter \
    && sudo -n systemctl restart prometheus-node-exporter \
    && sudo -n systemctl enable --now @TEXTFILE_UNIT@.timer >/dev/null \
    && sudo -n systemctl start @TEXTFILE_UNIT@.service"

  # 4. prove it: authenticated TLS scrape succeeds, anonymous one is refused
  sleep 2
  code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' --cacert "$CA_CRT" "https://$ADDR:9100/metrics" || true)
  body=$(curl -s -m 30 --cacert "$CA_CRT" -u "prometheus:$(cat "$PASS")" "https://$ADDR:9100/metrics" || true)
  n=$(grep -c '^node_' <<<"$body" || true)
  v=$(sed -n 's/^node_exporter_build_info{.*version="\([^"]*\)".*/\1/p' <<<"$body")
  echo "   anonymous: HTTP $code (want 401) · authenticated: $n node_* series · node_exporter ${v:-?}"
  [[ $code == 401 && $n -gt 100 ]] || { echo "   !! $NAME exporter check FAILED" >&2; exit 1; }
done

# Prometheus: file_sd target list from the site config (re-read automatically),
# a scrape job for it, and host="@HEAD_HOST@" on the pre-existing head-node job.
install -m 0644 "$CA_CRT" /etc/prometheus/@UNIT_PREFIX@-ca.crt
"$PY3" - "$CFG" <<'EOF' > @PROM_NODE_TARGETS@
import json, sys
c = json.load(open(sys.argv[1]))
head = c["server"]["address"]
try:                                   # named hosts -> the wired IP llm-discover pinned (see llm-discover)
    pins = json.load(open("@CONSOLE_STATE@/addresses.json"))
except Exception:
    pins = {}
def addr(h):
    return pins.get(h["name"], {}).get("ip") or h["address"]
print(json.dumps([{"targets": [f"{addr(h)}:9100"], "labels": {"host": h["name"]}}
                  for h in c["hosts"]
                  if h.get("enabled", True) is not False and h.get("address") and h["address"] != head],
                 indent=2))
EOF
PY=@PROM_CONFIG@
if grep -q "managed by noctyx setup metrics" "$PY" 2>/dev/null; then
  # Noctyx's own Prometheus already has the job and the head's target: just re-read the targets
  curl -s -X POST http://127.0.0.1:9090/-/reload >/dev/null || true
  echo "prometheus (noctyx setup metrics): reloaded; targets in @PROM_NODE_TARGETS@"
  exit 0
fi
if ! grep -q "job_name: @PROM_JOB_NODES@" "$PY"; then
  cp -a "$PY" "$PY.pre-noctyx"
  "$PY3" - "$PY" <<'EOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
s = s.rstrip("\n") + """

  # Noctyx fleet (noctyx tool install-node-metrics). Targets come from
  # the site config via file_sd; each exporter is TLS (Noctyx CA) + basic auth.
  - job_name: @PROM_JOB_NODES@
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/@UNIT_PREFIX@-ca.crt
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/node-exporter.pass
    file_sd_configs:
      - files: ['@PROM_NODE_TARGETS@']
"""
open(p, "w").write(s + "\n")
EOF
fi
# the head node's pre-existing job gets the same `host` label the UI queries
# by. Anchored on its targets line (the stock file has comments between
# job_name and static_configs), applied on its own so it is never skipped.
if ! grep -q "host: @HEAD_HOST@" "$PY"; then
  "$PY3" - "$PY" <<'EOF2'
import sys
p = sys.argv[1]; s = open(p).read()
line = "      - targets: ['localhost:9100']\n"
assert s.count(line) == 1, "cannot find the head node's node_exporter target"
open(p, "w").write(s.replace(line, line + "        labels:\n          host: @HEAD_HOST@\n"))
EOF2
fi
promtool check config "$PY" >/dev/null
systemctl reload prometheus
echo "prometheus: config OK, reloaded; targets in @PROM_NODE_TARGETS@"
