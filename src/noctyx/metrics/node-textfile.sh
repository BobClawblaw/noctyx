#!/usr/bin/env bash
# Noctyx textfile collector for node_exporter: GPU + Docker containers.
# Runs every 15 s (@TEXTFILE_UNIT@.timer) and atomically replaces
# $DIR/@TEXTFILE_NAME@, which node_exporter's textfile collector publishes.
#
# GB10 (unified memory) answers memory.used/total with "[N/A]": such fields are
# omitted rather than faked, and @METRIC_PREFIX@_gpu_process_memory_bytes (the sum of what
# compute processes hold) is published instead so the UI still has a number.
set -uo pipefail
DIR=${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}
OUT=$DIR/@TEXTFILE_NAME@
mkdir -p "$DIR"
TMP=$(mktemp "$DIR/.@TEXTFILE_NAME@.XXXXXX") || exit 1
trap 'rm -f "$TMP"' EXIT

num() { [[ $1 =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && printf '%s' "$1"; }
lbl() { printf '%s' "$1" | tr -d '"\\\n' | sed 's/^ *//;s/ *$//'; }

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

  echo "# HELP @METRIC_PREFIX@_textfile_last_run_timestamp_seconds When this collector last ran."
  echo "# TYPE @METRIC_PREFIX@_textfile_last_run_timestamp_seconds gauge"
  echo "@METRIC_PREFIX@_textfile_last_run_timestamp_seconds $(date +%s)"
} > "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" "$OUT"
trap - EXIT
