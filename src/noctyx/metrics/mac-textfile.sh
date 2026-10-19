#!/bin/bash
# Noctyx textfile collector for macOS (Apple silicon): GPU + package power
# via powermetrics (needs root — installed as a root LaunchDaemon), memory
# pressure, and running LLM servers. Publishes the SAME metric names as the
# Linux collector so the UI needs no special case.
set -uo pipefail
DIR=${TEXTFILE_DIR:-/opt/homebrew/var/lib/node_exporter/textfile}
OUT=$DIR/@TEXTFILE_NAME@
mkdir -p "$DIR"
TMP=$(mktemp "$DIR/.@TEXTFILE_NAME@.XXXXXX") || exit 1
trap 'rm -f "$TMP"' EXIT
CHIP=$(sysctl -n machdep.cpu.brand_string 2>/dev/null | tr -d '"\\')
{
  echo "# HELP cm_gpu_utilization_percent GPU active residency (powermetrics)."
  echo "# TYPE cm_gpu_utilization_percent gauge"
  echo "# HELP cm_gpu_power_watts GPU power (powermetrics)."
  echo "# TYPE cm_gpu_power_watts gauge"
  echo "# HELP cm_cpu_power_watts CPU package power (powermetrics)."
  echo "# TYPE cm_cpu_power_watts gauge"
  echo "# HELP cm_ane_power_watts Neural Engine power (powermetrics)."
  echo "# TYPE cm_ane_power_watts gauge"
  if pm=$(/usr/bin/powermetrics --samplers gpu_power,cpu_power,ane_power -i 1000 -n 1 2>/dev/null); then
    gpu_res=$(echo "$pm" | awk '/GPU HW active residency/{gsub("%","",$5); print $5; exit}')
    gpu_mw=$(echo "$pm"  | awk '/^GPU Power/{print $3; exit}')
    cpu_mw=$(echo "$pm"  | awk '/^CPU Power/{print $3; exit}')
    ane_mw=$(echo "$pm"  | awk '/^ANE Power/{print $3; exit}')
    [[ -n ${gpu_res:-} ]] && echo "cm_gpu_utilization_percent{gpu=\"0\",name=\"$CHIP\"} $gpu_res"
    [[ -n ${gpu_mw:-} ]] && echo "cm_gpu_power_watts{gpu=\"0\",name=\"$CHIP\"} $(echo "$gpu_mw" | awk '{printf "%.2f",$1/1000}')"
    [[ -n ${cpu_mw:-} ]] && echo "cm_cpu_power_watts $(echo "$cpu_mw" | awk '{printf "%.2f",$1/1000}')"
    [[ -n ${ane_mw:-} ]] && echo "cm_ane_power_watts $(echo "$ane_mw" | awk '{printf "%.2f",$1/1000}')"
    echo "cm_gpu_info{gpu=\"0\",name=\"$CHIP\",driver=\"macOS $(sw_vers -productVersion)\"} 1"
  fi
  # temperatures: powermetrics only gives a pressure level on Apple silicon; macmon
  # (brew, sudoless, IOReport/HID) reads the die sensors. Absent macmon = no series.
  MACMON=/opt/homebrew/bin/macmon
  if [[ -x $MACMON ]] && mm=$("$MACMON" pipe -s 1 -i 500 2>/dev/null | head -1); then
    echo "$mm" | /usr/bin/python3 -c 'import json,sys
t=json.loads(sys.stdin.read()).get("temp") or {}
chip=sys.argv[1]
if t.get("cpu_temp_avg"):
    print("# HELP cm_system_temperature_celsius CPU/SoC die temperature (macmon).")
    print("# TYPE cm_system_temperature_celsius gauge")
    print("cm_system_temperature_celsius{sensor=\"cpu\"} %.1f" % t["cpu_temp_avg"])
if t.get("gpu_temp_avg"):
    print("# HELP cm_gpu_temperature_celsius GPU die temperature (macmon).")
    print("# TYPE cm_gpu_temperature_celsius gauge")
    print("cm_gpu_temperature_celsius{gpu=\"0\",name=\"%s\"} %.1f" % (chip, t["gpu_temp_avg"]))' "$CHIP" 2>/dev/null
  fi
  # unified memory: what the UI shows as "gpu memory" = memory held by GPU-heavy processes isn't
  # exposed by macOS; report memory pressure instead (0..100 = % used incl. compressed)
  vm_stat | awk -v total=$(sysctl -n hw.memsize) 'BEGIN{ps=16384} /page size of/{ps=$8} /Pages active/{a=$3} /Pages wired/{w=$4} /Pages occupied by compressor/{c=$5} END{gsub(/\./,"",a);gsub(/\./,"",w);gsub(/\./,"",c); printf "# HELP cm_memory_used_bytes active+wired+compressed.\n# TYPE cm_memory_used_bytes gauge\ncm_memory_used_bytes %d\n", (a+w+c)*ps}'
  echo "# HELP cm_textfile_last_run_timestamp_seconds When this collector last ran."
  echo "# TYPE cm_textfile_last_run_timestamp_seconds gauge"
  echo "cm_textfile_last_run_timestamp_seconds $(date +%s)"
} > "$TMP"
chmod 0644 "$TMP"; mv -f "$TMP" "$OUT"; trap - EXIT
