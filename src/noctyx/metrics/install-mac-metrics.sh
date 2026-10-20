#!/usr/bin/env bash
# Put a Mac (Apple silicon) on the Noctyx dashboard: monitored only, no backup.
#   sudo noctyx tool install-mac-metrics mac1
# Needs: the host in the site config with os: macos + ssh_user; that user on the Mac
# with our root key authorised, Homebrew installed, and passwordless sudo for
# the LaunchDaemon step (or run the printed commands there by hand).
# Installs node_exporter (brew, TLS + basic auth like the Linux nodes, textfile
# dir), the powermetrics collector as a root LaunchDaemon (every 15 s), and
# adds the Prometheus target.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG=${NOCTYX_CONFIG:?run through: noctyx tool <name>}
NAME=${1:?usage: install-mac-metrics.sh <host name in the site config>}
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
# address: the wired IP llm-discover last pinned (addresses.json) when the site config names the host
PY3=${NX_PY:-python3}
read -r ADDR USER < <("$PY3" - "$CFG" "$NAME" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); h = [x for x in c["hosts"] if x["name"] == sys.argv[2]][0]
try:
    a = json.load(open("@CONSOLE_STATE@/addresses.json")).get(h["name"], {}).get("ip")
except Exception:
    a = None
print(a or h["address"], h.get("ssh_user") or (c.get("server", {}).get("ssh") or c["ssh"])["user"])
PY
)
SSH_KEY=$("$PY3" -c "import json;print(json.load(open('$CFG'))['server']['ssh']['key'])")
E=(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" "$USER@$ADDR")
PASS=/etc/prometheus/node-exporter.pass
HASH=$(tr -d '\n' < "$PASS" | htpasswd -niB -C 10 prometheus | cut -d: -f2-)   # bcrypt; the password on stdin
PKI=@ETC@/metrics; install -d -m 0700 "$PKI"
echo "== $NAME ($USER@$ADDR)"
# The cert must name the IP Prometheus scrapes. A DHCP, dual-homed Mac has several: put every
# IPv4 the box holds right now in the SANs (Ethernet + Wi-Fi) plus its names, and reissue
# whenever the pinned address is missing from the current cert (e.g. a new DHCP lease).
IPS=$("${E[@]}" "ifconfig | awk '/inet /{print \$2}' | grep -v '^127\.'" | tr '\n' ' ')
SAN="IP:$ADDR"; for ip in $IPS; do [[ $ip == "$ADDR" ]] || SAN="$SAN,IP:$ip"; done; SAN="$SAN,DNS:$NAME,DNS:$NAME.local"
if [[ ! -s $PKI/$NAME.crt ]] || ! openssl x509 -in "$PKI/$NAME.crt" -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$ADDR\b"; then
  echo "   issuing node_exporter certificate for $SAN"
  openssl req -new -newkey rsa:2048 -nodes -keyout "$PKI/$NAME.key" -subj "/O=Noctyx/CN=$NAME" -out "$PKI/$NAME.csr" 2>/dev/null
  openssl x509 -req -in "$PKI/$NAME.csr" -CA @TLS@/ca.crt -CAkey @TLS@/ca.key -CAcreateserial -days 825 \
    -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "$SAN") -out "$PKI/$NAME.crt" 2>/dev/null
  chmod 0600 "$PKI/$NAME.key"; rm -f "$PKI/$NAME.csr"
fi
BREW=$("${E[@]}" 'ls /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -1')
[[ -n $BREW ]] || { echo "Homebrew not found on $NAME — install it first (https://brew.sh)" >&2; exit 1; }
PREFIX=$(dirname "$(dirname "$BREW")")
"${E[@]}" "$BREW list node_exporter >/dev/null 2>&1 || $BREW install node_exporter" >/dev/null
# macmon: CPU/GPU die temperatures for the collector (powermetrics has none on Apple silicon)
"${E[@]}" "$BREW list macmon >/dev/null 2>&1 || $BREW install macmon" >/dev/null || echo "   !! macmon not installed: no temperatures from $NAME"
ETC=$PREFIX/etc/node_exporter; TF=$PREFIX/var/lib/node_exporter/textfile
"${E[@]}" "mkdir -p $ETC $TF"
put() { "${E[@]}" "cat > '$3' && chmod $1 '$3'" < "$2"; }
put 0644 "$PKI/$NAME.crt" "$ETC/node_exporter.crt"
put 0600 "$PKI/$NAME.key" "$ETC/node_exporter.key"
printf 'tls_server_config:\n  cert_file: %s/node_exporter.crt\n  key_file: %s/node_exporter.key\nbasic_auth_users:\n  prometheus: %s\n' "$ETC" "$ETC" "$HASH" | put 0644 /dev/stdin "$ETC/web.yml"
# node_exporter as a user LaunchAgent via brew services, with our flags
"${E[@]}" "cat > $PREFIX/etc/node_exporter/args <<A
--web.config.file=$ETC/web.yml --collector.textfile.directory=$TF
A"
"${E[@]}" "$BREW services stop node_exporter >/dev/null 2>&1; true"
# brew's plist ignores args: write our own LaunchAgent
"${E[@]}" "mkdir -p ~/Library/LaunchAgents && cat > ~/Library/LaunchAgents/@LAUNCHD_PREFIX@.node_exporter.plist <<P
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
 <key>Label</key><string>@LAUNCHD_PREFIX@.node_exporter</string>
 <key>ProgramArguments</key><array><string>$PREFIX/bin/node_exporter</string><string>--web.config.file=$ETC/web.yml</string><string>--collector.textfile.directory=$TF</string></array>
 <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
 <key>StandardErrorPath</key><string>$PREFIX/var/log/node_exporter.log</string>
</dict></plist>
P
launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/@LAUNCHD_PREFIX@.node_exporter.plist 2>/dev/null; launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/@LAUNCHD_PREFIX@.node_exporter.plist"
# powermetrics collector: root LaunchDaemon every 15 s
put 0755 "$HERE/mac-textfile.sh" "/tmp/nx-mac-textfile.sh"
"${E[@]}" "sudo -n install -m 0755 /tmp/nx-mac-textfile.sh /usr/local/bin/@TEXTFILE_UNIT@ && sudo -n sh -c 'cat > /Library/LaunchDaemons/@LAUNCHD_PREFIX@.textfile.plist <<P
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
 <key>Label</key><string>@LAUNCHD_PREFIX@.textfile</string>
 <key>ProgramArguments</key><array><string>/usr/local/bin/@TEXTFILE_UNIT@</string></array>
 <key>EnvironmentVariables</key><dict><key>TEXTFILE_DIR</key><string>$TF</string></dict>
 <key>RunAtLoad</key><true/><key>StartInterval</key><integer>15</integer>
</dict></plist>
P
launchctl bootout system /Library/LaunchDaemons/@LAUNCHD_PREFIX@.textfile.plist 2>/dev/null; launchctl bootstrap system /Library/LaunchDaemons/@LAUNCHD_PREFIX@.textfile.plist'" \
  || echo "   !! could not install the powermetrics LaunchDaemon (needs passwordless sudo on the Mac); node_exporter still works without GPU stats"
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert @TLS@/ca.crt "https://$ADDR:9100/metrics" || true)
n=$(curl -s --cacert @TLS@/ca.crt -u "prometheus:$(cat "$PASS")" "https://$ADDR:9100/metrics" | grep -c '^node_' || true)
g=$(curl -s --cacert @TLS@/ca.crt -u "prometheus:$(cat "$PASS")" "https://$ADDR:9100/metrics" | grep -c '^@METRIC_PREFIX@_gpu' || true)
echo "   anonymous: HTTP $code (want 401) · authenticated: $n node_* series · gpu series: $g"
# Prometheus target list: llm-discover owns it (pins named hosts to their wired IP, rewrites on change)
"$PY3" -m noctyx.metrics.llm_discover >/dev/null 2>&1 || true
if grep -q "managed by noctyx setup metrics" @PROM_CONFIG@ 2>/dev/null; then
  curl -s -X POST http://127.0.0.1:9090/-/reload >/dev/null || true
else
  systemctl reload prometheus
fi
echo "   prometheus target added; the dashboard picks $NAME up on its next refresh"
