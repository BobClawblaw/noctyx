#!/usr/bin/env bash
# Noctyx — install/refresh the agent on one node, from the head node.
# Run through `noctyx install-agent --host NAME [--profile P] [--with-client-timer]`, which exports the
# site's paths (NX_*) and renders the agent for this site first.
#
# Secrets never travel on a command line (they would show in every node's `ps`). They are piped through
# stdin into `install`/`tee`, which write them 0600 root.
set -euo pipefail

: "${NOCTYX_CONFIG:?run through noctyx install-agent}" "${NX_AGENT_FILE:?}" "${NX_DUMPS_FILE:?}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG=$NOCTYX_CONFIG
PY=${NX_PY:-python3}                                   # the head's interpreter (a Mac's /usr/bin/python3 is 3.9)
SSH_USER=$($PY -c 'import json,sys;c=json.load(open(sys.argv[1]));print((c.get("server",{}).get("ssh") or c["ssh"])["user"])' "$CFG")
SSH_KEY=$($PY -c 'import json,sys;c=json.load(open(sys.argv[1]));print((c.get("server",{}).get("ssh") or {}).get("key","/root/.ssh/id_ed25519"))' "$CFG")
HOST=""; PROFILE=system; WITH_TIMER=0

while [[ $# -gt 0 ]]; do case $1 in
  --host) HOST=$2; shift 2;; --profile) PROFILE=$2; shift 2;;
  --with-client-timer) WITH_TIMER=1; shift;; --verify) shift;;
  *) echo "unknown arg: $1" >&2; exit 1;;
esac; done
[[ -n $HOST ]] || { echo "usage: $0 --host NAME [--profile P] [--with-client-timer]" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run as root (needs $SSH_KEY)" >&2; exit 1; }

read -r ADDR ARCH REPO LOCAL TOS < <($PY - "$CFG" "$HOST" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
h=[x for x in cfg["hosts"] if x["name"]==sys.argv[2] and x.get("enabled",True)!=False]
if not h: print("MISSING"); sys.exit(0)
h=h[0]
# local: the head backing itself up (commands run here, not over ssh); os: linux | macos
print(h.get("address") or "MISSING", h["arch"], h["repo"], 1 if h.get("local") else 0, h.get("os") or "linux")
PY
)
[[ $ADDR == MISSING || -z $ADDR ]] && { echo "host '$HOST' not in $CFG (or has no address)" >&2; exit 1; }

SRV=$($PY -c "import json;c=json.load(open('$CFG'));s=c['server'];print(s.get('rest_host') or s['address'],s['rest_port'],s['tls']['ca'],sep='\t')")
IFS=$'\t' read -r SRV_ADDR SRV_PORT CA_LOCAL <<<"$SRV"
KEY_LOCAL="$NX_REPO_KEYS/$REPO.key"
CREDS_LOCAL="$NX_REPO_CREDS/$NX_REPO_DIR_PREFIX$REPO.pass"
[[ -r $KEY_LOCAL ]]   || { echo "missing $KEY_LOCAL — create the repository first" >&2; exit 1; }
[[ -r $CREDS_LOCAL ]] || { echo "missing $CREDS_LOCAL" >&2; exit 1; }
RU="$NX_REPO_DIR_PREFIX$REPO"                       # the endpoint login, and the repository's directory

# a local host (the head itself) runs the same commands here: install-agent is root, so sudo -n passes
if [[ ${LOCAL:-0} == 1 ]]; then E="bash -c"
else E="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i $SSH_KEY $SSH_USER@$ADDR"; fi
MAC=0; [[ ${TOS:-linux} == macos ]] && MAC=1
RESTIC=restic; [[ $MAC == 1 ]] && RESTIC=/usr/local/bin/restic   # ssh to a Mac gets PATH=/usr/bin:/bin:/usr/sbin:/sbin
# put <mode> <local-file> <remote-path> — through stdin, so no secret is ever on a command line
if [[ $MAC == 1 ]]; then
  # a Mac has no group root (gid 0 is wheel), and an existing directory is never re-owned: a Mac's
  # /usr/local/bin often belongs to its user, for Homebrew
  put() { $E "{ [ -d '$(dirname "$3")' ] || sudo -n install -d -o root -g wheel '$(dirname "$3")'; } && sudo -n install -m '$1' -o root -g wheel /dev/stdin '$3'" < "$2"; }
else
  put() { $E "sudo -n install -d -o root -g root '$(dirname "$3")' && sudo -n install -m '$1' -o root -g root /dev/stdin '$3'" < "$2"; }
fi
run() { $E "$1"; }

echo "== Noctyx agent install: $HOST ($ADDR, $ARCH, repo=$REPO$([[ $LOCAL == 1 ]] && echo ', local'), ${TOS:-linux}) =="

# 1. restic present?
if [[ $MAC == 1 ]]; then
  # no package manager for root on a Mac: copy the head's own restic when the head is a Mac of the same CPU
  run "test -x $RESTIC" || {
    hb=$(command -v restic || true); rm_=$(run "uname -m")
    if [[ $(uname -s) == Darwin && -n $hb && $(uname -m) == "$rm_" ]]; then
      echo "  installing restic (the head's $hb)…"; put 0755 "$hb" "$RESTIC"
    else
      echo "  restic is missing on $HOST: install restic ${NX_RESTIC_VERSION:-0.19.1} for macOS at $RESTIC" >&2
      echo "  (github.com/restic/restic/releases: restic_<version>_darwin_$([[ $rm_ == arm64 ]] && echo arm64 || echo amd64).bz2)" >&2
      exit 1
    fi
  }
else
run "command -v restic >/dev/null" || {
  echo "  installing restic…"; run "sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q restic >/dev/null"
}
fi

# 2. agent (rendered for this site by the orchestrator)
put 0755 "$NX_AGENT_FILE" "$NX_AGENT_BIN"
echo "  agent        -> $NX_AGENT_BIN"

# 3. CA (also into the system trust store so curl/python/tooling agree)
put 0644 "$CA_LOCAL" "$NX_NODE_ETC/tls/ca.crt"
if [[ $MAC == 1 ]]; then
  # the System keychain; restic is handed --cacert anyway, so a refusal here (no GUI to authorise it) costs nothing
  if run "sudo -n security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $NX_NODE_ETC/tls/ca.crt >/dev/null 2>&1"; then
    echo "  CA           -> $NX_NODE_ETC/tls/ca.crt (+ System keychain)"
  else
    echo "  CA           -> $NX_NODE_ETC/tls/ca.crt (the System keychain refused it; restic uses the file)"
  fi
else
run "sudo -n cp $NX_NODE_ETC/tls/ca.crt /usr/local/share/ca-certificates/noctyx-ca.crt && sudo -n update-ca-certificates >/dev/null 2>&1" \
  && echo "  CA           -> $NX_NODE_ETC/tls/ca.crt (+ system trust)"
fi

# 4. repo key + endpoint credential + assembled repository location, root-only. The location embeds the
#    credential, so it is a 0600 file used through restic --repository-file, never -r on a command line.
put 0600 "$KEY_LOCAL"   "$NX_NODE_ETC/repo-password"
put 0600 "$CREDS_LOCAL" "$NX_NODE_ETC/rest-pass"
TMP=$(mktemp); chmod 600 "$TMP"
printf '%s' "rest:https://$RU:$(cat "$CREDS_LOCAL")@$SRV_ADDR:$SRV_PORT/$RU/$REPO" > "$TMP"
put 0600 "$TMP" "$NX_NODE_ETC/repo-url"; rm -f "$TMP"
echo "  key+creds    -> repo-password, rest-pass, repo-url (0600 root)"

# 5. client.conf
$E "sudo -n tee $NX_NODE_ETC/client.conf >/dev/null" <<EOF
# Noctyx node configuration — 0600 root, generated by install-agent.sh
HOST=$HOST
TARGET=$HOST
REPO_ID=$REPO
REST_URL=https://$SRV_ADDR:$SRV_PORT
REST_USER=$RU
PASSWORD_FILE=$NX_NODE_ETC/repo-password
REST_PASS_FILE=$NX_NODE_ETC/rest-pass
CA_FILE=$NX_NODE_ETC/tls/ca.crt
EOF

# 6. exclusions + this profile's include paths, from the site config (the only place a profile is
#    defined). A profile may ship its own exclusion list in the site's etc dir, used INSTEAD of the common one.
put 0644 "${NX_COMMON_FILE:?}" "$NX_NODE_ETC/exclusions-common.conf"
[[ $MAC == 1 ]] && put 0644 "$HERE/exclusions-macos.conf" "$NX_NODE_ETC/exclusions-macos.conf"
[[ -r $NX_ETC/exclusions-$PROFILE.conf ]] && put 0644 "$NX_ETC/exclusions-$PROFILE.conf" "$NX_NODE_ETC/exclusions-$PROFILE.conf"

profile_paths() {
  $PY - "$CFG" "$PROFILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1])); prof = sys.argv[2]
p = cfg.get("profiles", {}).get(prof)
if not p:
    sys.exit(f"profile '{prof}' is not defined in {sys.argv[1]}")
for x in p["paths"]:
    print(x)
PY
}
run "sudo -n mkdir -p $NX_NODE_ETC/profile-$PROFILE.d"
profile_paths | $E "sudo -n tee $NX_NODE_ETC/profile-$PROFILE.d/paths >/dev/null" \
  || { echo "could not resolve paths for profile '$PROFILE'" >&2; exit 1; }
echo "  profile      -> $PROFILE paths: $(profile_paths | tr '\n' ' ')"
# The `system` profile is `/`: /boot and /boot/efi come along because the agent deliberately does NOT
# pass --one-file-system (a --one-file-system "full system backup" quietly omits the bootloader).

# 7. hooks dir
run "sudo -n mkdir -p $NX_NODE_ETC/hooks/pre.d $NX_NODE_ETC/hooks/post.d && sudo -n chmod 750 $NX_NODE_ETC/hooks"

# 8. systemd: service always, timer only if asked for (it bypasses the head's per-spindle lock).
#    A Mac has no on-demand unit to write: launchd only for the opt-in timer.
if [[ $MAC == 1 ]]; then
  LABEL="${NX_LAUNCHD_PREFIX:-dev.noctyx}.backup"
  if [[ $WITH_TIMER == 1 ]]; then
    run "sudo -n tee /Library/LaunchDaemons/$LABEL.plist >/dev/null" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>$LABEL</string>
 <key>ProgramArguments</key><array><string>$NX_AGENT_BIN</string><string>backup</string><string>--profile</string><string>$PROFILE</string></array>
 <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>15</integer></dict>
 <key>LowPriorityIO</key><true/>
 <key>Nice</key><integer>10</integer>
 <key>StandardErrorPath</key><string>$NX_NODE_LIB/backup-timer.log</string>
</dict></plist>
PLIST
    run "sudo -n chown root:wheel /Library/LaunchDaemons/$LABEL.plist && sudo -n chmod 644 /Library/LaunchDaemons/$LABEL.plist && { sudo -n launchctl bootout system/$LABEL 2>/dev/null; sudo -n launchctl bootstrap system /Library/LaunchDaemons/$LABEL.plist; }"
    echo "  timer        -> autonomous nightly 02:15 (launchd $LABEL; bypasses the head's per-spindle lock)"
  else
    echo "  scheduling   -> by the head node (default), which holds the per-spindle lock"
  fi
else
run "sudo -n tee /etc/systemd/system/$NX_AGENT_UNIT.service >/dev/null" <<EOF
[Unit]
Description=Noctyx backup ($PROFILE)

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
ExecStart=$NX_AGENT_BIN backup --profile $PROFILE
TimeoutStartSec=3d
EOF
if [[ $WITH_TIMER == 1 ]]; then
  run "sudo -n tee /etc/systemd/system/$NX_AGENT_UNIT.timer >/dev/null" <<'EOF'
[Unit]
Description=Nightly Noctyx backup (autonomous)

[Timer]
OnCalendar=*-*-* 02:15:00
RandomizedDelaySec=2400
Persistent=true
AccuracySec=60

[Install]
WantedBy=timers.target
EOF
  run "sudo -n systemctl daemon-reload && sudo -n systemctl enable --now $NX_AGENT_UNIT.timer"
  echo "  timer        -> autonomous nightly (opt-in; bypasses the head's per-spindle lock)"
else
  run "sudo -n systemctl daemon-reload"
  echo "  scheduling   -> by the head node (default), which holds the per-spindle lock"
fi
fi

# 9. prove it: open the repository now, not at restore time
echo "  unlock test…"
if run "sudo -n $RESTIC --cacert $NX_NODE_ETC/tls/ca.crt --password-file $NX_NODE_ETC/repo-password --repository-file $NX_NODE_ETC/repo-url --no-cache snapshots >/dev/null 2>&1"; then
  echo "  unlock       -> OK (repository reachable, key correct, TLS trusted)"
else
  echo "  unlock       -> FAILED. In the order that matters:"
  echo "                   1. URL must be /<user>/<repo> — --private-repos refuses anything else"
  echo "                   2. the repository endpoint running? (noctyx nas-rest status)"
  echo "                   3. its htpasswd entry valid?"
  echo "                   4. CA trusted? repository key correct?"
  exit 2
fi

# 10. backup policy: per-profile exclusions, dump hook + config, current agent
if [[ -n ${NX_PY:-} ]]; then pa=( "$NX_PY" "${NX_ORCH:?}" ); else pa=( "${NX_ORCH:?}" ); fi
"${pa[@]}" policy-apply --host "$HOST" || { echo "  policy-apply FAILED" >&2; exit 2; }
echo "== installed: $HOST =="
