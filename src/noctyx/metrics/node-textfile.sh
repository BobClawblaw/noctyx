#!/usr/bin/env bash
# Noctyx textfile collector for node_exporter: GPU, Docker containers, sshd logins, NFS mounts.
# Runs every 15 s (@TEXTFILE_UNIT@.timer) and atomically replaces
# $DIR/@TEXTFILE_NAME@, which node_exporter's textfile collector publishes.
#
# The NFS probe (a timed write, read and small-file round trip on every read-write NFS mount) runs every
# 5 minutes as its own transient unit (@TEXTFILE_UNIT@ --nfs-probe) with its own file: a hung mount stalls
# that unit, never this collector, and the time it has been running is what shows the hang.
#
# GB10 (unified memory) answers memory.used/total with "[N/A]": such fields are
# omitted rather than faked, and @METRIC_PREFIX@_gpu_process_memory_bytes (the sum of what
# compute processes hold) is published instead so the UI still has a number.
set -uo pipefail
DIR=${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}
OUT=$DIR/@TEXTFILE_NAME@
NFS_OUT=$DIR/$(basename "@TEXTFILE_NAME@" .prom)-nfs.prom
NFS_UNIT=@TEXTFILE_UNIT@-nfsprobe
NFS_EVERY=300
PROBE_MB=64
mkdir -p "$DIR"
num() { [[ $1 =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && printf '%s' "$1"; }
lbl() { printf '%s' "$1" | tr -d '"\\\n' | sed 's/^ *//;s/ *$//'; }
nfs_mounts() { findmnt -rn -t nfs,nfs4 -O rw -o TARGET,SOURCE 2>/dev/null; }
ns() { date +%s%N; }

if [[ ${1:-} == --nfs-probe ]]; then
  # each mount: PROBE_MB written with O_DIRECT and fsync, read back with O_DIRECT, then ten 4 KiB
  # create+fsync+remove round trips (what a backup's lock and index files cost); every step has a timeout
  TMPN=$(mktemp "$DIR/.nfs.XXXXXX") || exit 1
  trap 'rm -f "$TMPN"' EXIT
  {
    echo "# HELP @METRIC_PREFIX@_nfs_probe_ok 1: the last probe of this NFS mount wrote, read and removed its file."
    echo "# TYPE @METRIC_PREFIX@_nfs_probe_ok gauge"
    echo "# HELP @METRIC_PREFIX@_nfs_probe_write_bytes_per_second Direct, fsynced write throughput of the probe."
    echo "# TYPE @METRIC_PREFIX@_nfs_probe_write_bytes_per_second gauge"
    echo "# HELP @METRIC_PREFIX@_nfs_probe_read_bytes_per_second Direct read throughput of the probe."
    echo "# TYPE @METRIC_PREFIX@_nfs_probe_read_bytes_per_second gauge"
    echo "# HELP @METRIC_PREFIX@_nfs_probe_small_op_seconds Mean time of a 4 KiB create+fsync+remove."
    echo "# TYPE @METRIC_PREFIX@_nfs_probe_small_op_seconds gauge"
    while read -r mnt src; do
      l="mountpoint=\"$(lbl "$mnt")\",source=\"$(lbl "$src")\""
      d="$mnt/.@UNIT_PREFIX@-probe"; f="$d/$(hostname -s)"; ok=0
      if timeout 30 mkdir -p "$d" 2>/dev/null; then
        t0=$(ns)
        if timeout 120 dd if=/dev/zero of="$f" bs=1M count=$PROBE_MB oflag=direct conv=fsync status=none 2>/dev/null; then
          t1=$(ns)
          if timeout 120 dd if="$f" of=/dev/null bs=1M iflag=direct status=none 2>/dev/null; then
            t2=$(ns); good=1
            for i in 1 2 3 4 5 6 7 8 9 10; do
              timeout 20 dd if=/dev/zero of="$f.s" bs=4k count=1 conv=fsync status=none 2>/dev/null && timeout 20 rm -f "$f.s" || { good=0; break; }
            done
            t3=$(ns)
            if [[ $good == 1 ]]; then
              ok=1
              echo "@METRIC_PREFIX@_nfs_probe_write_bytes_per_second{$l} $(( PROBE_MB * 1048576 * 1000000000 / (t1 - t0 + 1) ))"
              echo "@METRIC_PREFIX@_nfs_probe_read_bytes_per_second{$l} $(( PROBE_MB * 1048576 * 1000000000 / (t2 - t1 + 1) ))"
              printf '@METRIC_PREFIX@_nfs_probe_small_op_seconds{%s} %s\n' "$l" "$(awk -v n=$(( (t3 - t2) / 10 )) 'BEGIN{printf "%.4f", n/1e9}')"
            fi
          fi
        fi
        timeout 30 rm -f "$f" "$f.s" 2>/dev/null
      fi
      echo "@METRIC_PREFIX@_nfs_probe_ok{$l} $ok"
    done < <(nfs_mounts)
    echo "@METRIC_PREFIX@_nfs_probe_last_run_timestamp_seconds $(date +%s)"
  } > "$TMPN"
  chmod 0644 "$TMPN"; mv -f "$TMPN" "$NFS_OUT"; trap - EXIT
  exit 0
fi
TMP=$(mktemp "$DIR/.@TEXTFILE_NAME@.XXXXXX") || exit 1
trap 'rm -f "$TMP"' EXIT


{
  echo "# HELP @METRIC_PREFIX@_gpu_utilization_percent GPU utilisation (nvidia-smi)."
  echo "# TYPE @METRIC_PREFIX@_gpu_utilization_percent gauge"
  echo "# HELP @METRIC_PREFIX@_gpu_temperature_celsius GPU core temperature."
  echo "# TYPE @METRIC_PREFIX@_gpu_temperature_celsius gauge"
  echo "# HELP @METRIC_PREFIX@_gpu_power_watts GPU board power draw."
  echo "# TYPE @METRIC_PREFIX@_gpu_power_watts gauge"
  echo "# HELP @METRIC_PREFIX@_gpu_sm_clock_mhz GPU SM clock."
  echo "# TYPE @METRIC_PREFIX@_gpu_sm_clock_mhz gauge"
  echo "# HELP @METRIC_PREFIX@_gpu_memory_used_bytes GPU memory used (absent on unified-memory GPUs)."
  echo "# TYPE @METRIC_PREFIX@_gpu_memory_used_bytes gauge"
  echo "# HELP @METRIC_PREFIX@_gpu_memory_total_bytes GPU memory total (absent on unified-memory GPUs)."
  echo "# TYPE @METRIC_PREFIX@_gpu_memory_total_bytes gauge"
  if command -v nvidia-smi >/dev/null 2>&1; then
    while IFS=, read -r idx name util temp power sm mused mtotal driver; do
      l="gpu=\"$(lbl "$idx")\",name=\"$(lbl "$name")\""
      v=$(num "$(lbl "$util")") && echo "@METRIC_PREFIX@_gpu_utilization_percent{$l} $v"
      v=$(num "$(lbl "$temp")") && echo "@METRIC_PREFIX@_gpu_temperature_celsius{$l} $v"
      v=$(num "$(lbl "$power")") && echo "@METRIC_PREFIX@_gpu_power_watts{$l} $v"
      v=$(num "$(lbl "$sm")") && echo "@METRIC_PREFIX@_gpu_sm_clock_mhz{$l} $v"
      v=$(num "$(lbl "$mused")") && echo "@METRIC_PREFIX@_gpu_memory_used_bytes{$l} $((${v%.*} * 1048576))"
      v=$(num "$(lbl "$mtotal")") && echo "@METRIC_PREFIX@_gpu_memory_total_bytes{$l} $((${v%.*} * 1048576))"
      echo "@METRIC_PREFIX@_gpu_info{$l,driver=\"$(lbl "$driver")\"} 1"
    done < <(timeout 10 nvidia-smi --format=csv,noheader,nounits \
      --query-gpu=index,name,utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,memory.total,driver_version 2>/dev/null)
    used=$(timeout 10 nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits 2>/dev/null \
           | awk '$1 ~ /^[0-9]+$/ {s += $1} END {print s + 0}')
    echo "# HELP @METRIC_PREFIX@_gpu_process_memory_bytes GPU memory held by compute processes (works on unified memory)."
    echo "# TYPE @METRIC_PREFIX@_gpu_process_memory_bytes gauge"
    echo "@METRIC_PREFIX@_gpu_process_memory_bytes $(( ${used:-0} * 1048576 ))"
  fi

  if command -v docker >/dev/null 2>&1; then
    echo "# HELP @METRIC_PREFIX@_container_running Running Docker container (1 per container)."
    echo "# TYPE @METRIC_PREFIX@_container_running gauge"
    timeout 10 docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | while IFS=$'\t' read -r n i; do
      echo "@METRIC_PREFIX@_container_running{name=\"$(lbl "$n")\",image=\"$(lbl "$i")\"} 1"
    done
  fi

  # sshd: failed and accepted logins, counted from the journal since the last run (a cursor kept beside),
  # and the addresses a login was ever accepted from (a new one is worth a look)
  if command -v journalctl >/dev/null 2>&1; then
    python3 - "$DIR/.@TEXTFILE_NAME@.ssh.json" "@METRIC_PREFIX@" <<'PYSSH' 2>/dev/null
import json, re, subprocess, sys, time
path, px = sys.argv[1], sys.argv[2]
try:
    st = json.load(open(path))
except Exception:
    st = {}
first = "cursor" not in st
for k in ("failed", "invalid"):
    st.setdefault(k, 0)
for k in ("accepted", "seen", "new", "fail_src"):
    st.setdefault(k, {})
args = ["journalctl", "-o", "json", "--no-pager", "-q", "_COMM=sshd", "+", "_COMM=sshd-session"]
args += ["--since", "-30d"] if first else ["--after-cursor", st["cursor"]]
try:
    out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
except Exception:
    out = ""
q = lambda s: str(s).replace("\\", "").replace('"', "").replace("\n", "")[:80]
for line in out.splitlines():
    try:
        e = json.loads(line)
    except ValueError:
        continue
    st["cursor"] = e.get("__CURSOR", st.get("cursor"))
    m, ts = e.get("MESSAGE"), int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    if not isinstance(m, str):
        continue
    a = re.match(r"Accepted (\S+) for (\S+) from (\S+) port", m)
    if a:
        st["accepted"][a[1]] = st["accepted"].get(a[1], 0) + 1
        if a[3] not in st["seen"]:
            st["seen"][a[3]] = 0 if first else ts      # the first run's 30 days are the known addresses
            if not first:
                st["new"][a[3]] = [ts, a[2]]
        continue
    f = re.match(r"Failed \S+ for (?:invalid user )?(\S+) from (\S+) port", m)
    if f:
        st["failed"] += 1
        st["fail_src"][f[2]] = st["fail_src"].get(f[2], 0) + 1
        continue
    if re.match(r"Invalid user \S* ?from \S+", m):
        st["invalid"] += 1
if "cursor" not in st:
    st["cursor"] = ""                                    # nothing logged yet: count from now on
now = time.time()
st["new"] = {k: v for k, v in st["new"].items() if now - v[0] < 7 * 86400}
st["fail_src"] = dict(sorted(st["fail_src"].items(), key=lambda kv: -kv[1])[:20])
json.dump(st, open(path + ".tmp", "w"))
import os
os.replace(path + ".tmp", path)
print(f"# HELP {px}_ssh_failed_logins_total Failed sshd authentications (password, key, keyboard-interactive).")
print(f"# TYPE {px}_ssh_failed_logins_total counter")
print(f"{px}_ssh_failed_logins_total {st['failed']}")
print(f"# HELP {px}_ssh_invalid_user_attempts_total sshd connections for a user that does not exist.")
print(f"# TYPE {px}_ssh_invalid_user_attempts_total counter")
print(f"{px}_ssh_invalid_user_attempts_total {st['invalid']}")
print(f"# HELP {px}_ssh_accepted_logins_total Accepted sshd logins, by method.")
print(f"# TYPE {px}_ssh_accepted_logins_total counter")
for k, v in st["accepted"].items():
    print(f'{px}_ssh_accepted_logins_total{{method="{q(k)}"}} {v}')
print(f"# HELP {px}_ssh_known_sources Addresses a login was ever accepted from.")
print(f"# TYPE {px}_ssh_known_sources gauge")
print(f"{px}_ssh_known_sources {len(st['seen'])}")
print(f"# HELP {px}_ssh_new_source_login_timestamp_seconds The first accepted login from an address not seen before (last 7 days).")
print(f"# TYPE {px}_ssh_new_source_login_timestamp_seconds gauge")
for k, (t, u) in st["new"].items():
    print(f'{px}_ssh_new_source_login_timestamp_seconds{{addr="{q(k)}",user="{q(u)}"}} {t:.0f}')
print(f"# HELP {px}_ssh_failed_by_source_total Failed authentications from the 20 busiest addresses.")
print(f"# TYPE {px}_ssh_failed_by_source_total counter")
for k, v in st["fail_src"].items():
    print(f'{px}_ssh_failed_by_source_total{{addr="{q(k)}"}} {v}')
PYSSH
  fi

  # the NFS probe: started when it is due and not running; how long a running one has run shows a hang
  if [[ -n $(nfs_mounts) ]] && command -v systemd-run >/dev/null 2>&1; then
    since=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$NFS_UNIT" 2>/dev/null)
    if systemctl is-active --quiet "$NFS_UNIT" 2>/dev/null && [[ $since =~ ^[0-9]+$ && $since -gt 0 ]]; then
      up=$(awk '{print int($1)}' /proc/uptime)
      echo "# HELP @METRIC_PREFIX@_nfs_probe_running_seconds How long the NFS probe running now has run (a hung mount keeps it running)."
      echo "# TYPE @METRIC_PREFIX@_nfs_probe_running_seconds gauge"
      echo "@METRIC_PREFIX@_nfs_probe_running_seconds $(( up - since / 1000000 ))"
    elif [[ ! -s $NFS_OUT ]] || (( $(date +%s) - $(stat -c %Y "$NFS_OUT") >= NFS_EVERY )); then
      systemctl reset-failed "$NFS_UNIT" 2>/dev/null
      systemd-run --quiet --collect --unit="$NFS_UNIT" -p Nice=10 -E TEXTFILE_DIR="$DIR" /bin/bash "$0" --nfs-probe >/dev/null 2>&1 || true
    fi
  fi

  echo "# HELP @METRIC_PREFIX@_textfile_last_run_timestamp_seconds When this collector last ran."
  echo "# TYPE @METRIC_PREFIX@_textfile_last_run_timestamp_seconds gauge"
  echo "@METRIC_PREFIX@_textfile_last_run_timestamp_seconds $(date +%s)"
} > "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" "$OUT"
trap - EXIT
