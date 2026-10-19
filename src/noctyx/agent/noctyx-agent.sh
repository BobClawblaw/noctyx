#!/usr/bin/env bash
# Noctyx node agent. Runs as root on a backup client.
# A template: noctyx fills its placeholders (node_etc, node_lib, result_token, …) from the site's paths
# when it installs it.
# POSIX-ish bash, no third-party deps beyond restic + coreutils.
#
#   noctyx-agent backup  --profile system|smoke|dataproxy [--dry-run] [--no-hooks]
#   noctyx-agent fs-meta          # capture boot/disk/AI-stack state only
#   noctyx-agent list|snapshots   # passthrough
#   noctyx-agent verify           # restic check + read-data-subset
#   noctyx-agent retention [--apply] [--keep-* ...]  # forget+prune; dry-run
#                                    UNLESS --apply. Flags come from the orchestrator.
#
# Emits exactly one machine-readable line on stdout:
#   @RESULT_TOKEN@ {"event":"backup",...}
# Everything else goes to stderr so the orchestrator can parse reliably.
set -uo pipefail

CONF=${NOCTYX_CONF:-@NODE_ETC@/client.conf}
STATE=@NODE_LIB@
META=$STATE/fsmeta
AUDIT=$STATE/audit.jsonl
HOOKS=@NODE_ETC@/hooks
LOCK=$STATE/agent.lock
VERBOSE=${NOCTYX_VERBOSE:-0}

log() { printf '[cm-agent] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; emit "{\"event\":\"error\",\"host\":\"$(hostname -s)\",\"message\":\"$1\"}"; exit 1; }

emit() { printf '@RESULT_TOKEN@ %s\n' "$1"; [[ -d $STATE ]] && printf '%s\n' "$1" >> "$AUDIT"; }

[[ $EUID -eq 0 ]] || die "must run as root (needs raw block metadata, xattrs, ACLs, device nodes)"
[[ -r $CONF ]]    || die "missing $CONF — install the agent first (head node: noctyx install-agent --host $(hostname -s))"
# shellcheck disable=SC1090
. "$CONF"
for v in REST_URL REST_USER PASSWORD_FILE CA_FILE REPO_ID; do
  [[ -n ${!v:-} ]] || die "$CONF missing $v"
done
[[ -r $PASSWORD_FILE ]] || die "cannot read $PASSWORD_FILE"
[[ -r ${REST_PASS_FILE:-/nonexistent} ]] || log "warning: REST_PASS_FILE unreadable; REST basic auth may fail"
command -v restic >/dev/null || die "restic is not installed"

# Two SEPARATE secrets, routinely conflated, and the cause of most "restic
# rejects the right password" debugging:
#   repo encryption key   -> --password-file (what decrypts the data)
#   endpoint credential   -> embedded in the repository location URL
# restic 0.16.4 does NOT take the endpoint credential from REST_USER/REST_PASSWORD
# in any form that works against rest-server; it must be in the URL.
# That location is therefore delivered as a root-only 0600 file and consumed via
# --repository-file, NOT passed with -r — an -r argument would expose the
# credential in the client's process list for the whole multi-hour backup.
# Verified with `ps -eo args`: zero occurrences of the credential.
REPO_URL_FILE=${REPO_URL_FILE:-@NODE_ETC@/repo-url}
[[ -r $REPO_URL_FILE ]] || die "missing $REPO_URL_FILE (repo location + endpoint credential)"
REPO_DISPLAY="rest:$REST_URL/$REST_USER/$REPO_ID"   # logs only — never the credential
RESTIC=( restic --cacert "$CA_FILE" --password-file "$PASSWORD_FILE" --repository-file "$REPO_URL_FILE" --no-cache )

# STATE must exist BEFORE the lock is opened. `exec 9>` fails on a missing
# directory, and that failure surfaces as "Bad file descriptor" followed by
# "another job is already running" — a completely misleading diagnosis for a
# missing directory, because it implies a concurrency problem that isn't there.
mkdir -p "$STATE" >/dev/null 2>&1 || die "cannot create state directory $STATE"

# ── survive the driver going away ─────────────────────────────────────
# The orchestrator drives us over ssh. When that ssh dies (head-node service
# restart, network blip), our stderr becomes a broken pipe: the next log line
# SIGPIPEs bash, restic's tee dies with it, and restic itself is killed on its
# final summary write — AFTER committing the snapshot but BEFORE emit(). Result:
# a real snapshot with no audit record, or a half-seed with a stale lock (two real
# incidents, both at console restarts).
# Ignore HUP/PIPE so a lost driver costs us log lines, never the backup.
trap '' HUP PIPE

# ── one job per host at a time ────────────────────────────────────────
# Taken only by commands that WRITE to the repo (see dispatch below). Read-only
# listing must never be blocked by a running backup: taking this lock for
# `snapshots` made every busy host report "no readable snapshot list".
take_job_lock() {
  exec 9>"$LOCK" || die "cannot open lock file $LOCK"
  # The holder list must come from the LOCK, never from pgrep -f on the agent's name:
  # the orchestrator's ssh command line contains that exact string, so pgrep
  # matches the job's own driver (on the head node even the orchestrator's
  # local ssh) and every retry "detects" the job that is doing the detecting.
  # fuser on the lock file names the actual fd holders — the truth.
  flock -n 9 || die "another noctyx job is genuinely running (lock holders: $(fuser "$LOCK" 2>/dev/null | tr ' ' '\n' | grep -v "^$$\$" | tr '\n' ' '))"
}

# ── filesystem / boot / AI-stack metadata ─────────────────────────────
# The reason this exists: a restore that puts the bytes back but not the
# bootloader, UUIDs, kernel, driver and toolkit state leaves a box that is
# "restored" and still down. Capture everything the restore image needs while
# the machine is healthy and can describe itself.
fs_meta() {
  local out=$1 d dev
  mkdir -p "$out"

  { echo "# generated $(date -Is) by noctyx-agent on $(hostname -s)"
    echo "# host=$(hostname -s) arch=$(uname -m) kernel=$(uname -r)"
    uname -a; echo; cat /etc/os-release 2>/dev/null; echo
    echo "cmdline: $(cat /proc/cmdline 2>/dev/null)"; echo; uptime; echo
    date -Is; echo "timezone: $(cat /etc/timezone 2>/dev/null)"
  } > "$out/system.txt" 2>&1

  # Block layer — the restore image rebuilds the disk from these.
  lsblk -b -P -o NAME,PATH,SIZE,TYPE,ROTA,TRAN,MODEL,SERIAL,UUID,PARTUUID,PARTLABEL,FSTYPE,MOUNTPOINT \
        > "$out/lsblk.txt" 2>&1
  lsblk -P -o NAME,SIZE,ROTA,TRAN,MOUNTPOINT,FSTYPE > "$out/lsblk-human.txt" 2>&1
  blkid > "$out/blkid.txt" 2>&1
  findmnt -R / > "$out/findmnt-root.txt" 2>&1
  df -B1 -P > "$out/df.txt" 2>&1
  cat /proc/partitions > "$out/proc-partitions.txt" 2>&1

  # Partition tables: sfdisk --dump is the replayable form.
  mkdir -p "$out/partitions"
  for d in $(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2=="disk"{print "/dev/"$1}'); do
    [[ -e $d ]] || continue
    # Skip USB-attached media: they are separate sources, not part of this
    # machine's boot chain, and replaying one onto a target disk is how you
    # wipe someone's archive.
    if [[ "$(lsblk -dn -o TRAN "$d" 2>/dev/null | head -1)" == "usb" ]]; then
      echo "skipped (usb): $d" >> "$out/partitions/SKIPPED.txt"; continue
    fi
    sfdisk --dump "$d" > "$out/partitions/$(basename "$d").sfdisk" 2>/dev/null
    parted -s -m "$d" unit B print 2>/dev/null > "$out/partitions/$(basename "$d").parted"
    dd if="$d" of="$out/partitions/$(basename "$d").gptbk" bs=512 count=34 2>/dev/null
    dd if="$d" of="$out/partitions/$(basename "$d").gptbk_end" bs=512 skip=$(( $(blockdev --getsz "$d") - 33 )) count=33 2>/dev/null
  done

  cp /etc/fstab "$out/fstab" 2>/dev/null
  cp /etc/crypttab "$out/crypttab" 2>/dev/null

  # LVM (head node): metadata is the only sane way back.
  if command -v pvs >/dev/null && pvs >/dev/null 2>&1 && [[ -n "$(pvs -a --noheadings 2>/dev/null)" ]]; then
    { pvs -v; vgs -v; lvs -a; } > "$out/lvm.txt" 2>&1
    mkdir -p "$out/lvm-backup"; cp -a /etc/lvm/archive /etc/lvm/backup "$out/lvm-backup/" 2>/dev/null
  fi

  # Bootloader. grub-install's *target* is what must be reproduced exactly
  # (arm64-efi vs x86_64-efi); guessing it is how restores come back unbootable.
  { echo "grub_version: $(grub-install --version 2>/dev/null | head -1)"
    echo "grub_target_dir: ${GRUB_TARGET_DIR:-/boot/grub}"
    for t in x86_64-efi arm64-efi i386-pc; do
      [[ -d "/usr/lib/grub/$t" ]] && echo "grub_module_available: $t"
    done
    echo "shim: $(dpkg -l shim-signed 2>/dev/null | awk '/^ii/{print $3}')"
    efibootmgr -v 2>/dev/null
    echo "efi_sys_part: $(findmnt -no SOURCE /boot/efi 2>/dev/null)"
    echo "boot_part:    $(findmnt -no SOURCE /boot 2>/dev/null)"
    echo "boot_cmd: $(cat /sys/firmware/efi/fw_platform_size 2>/dev/null | sed 's/^/efi_bits=/')"
  } > "$out/bootloader.txt" 2>&1

  ls -laR /boot > "$out/boot-listing.txt" 2>&1
  find /boot/efi -type f 2>/dev/null | sort > "$out/esp-files.txt"

  # Package + service state: what to compare against after a restore.
  dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' > "$out/packages.tsv" 2>/dev/null
  systemctl list-unit-files --no-pager --state=enabled,static > "$out/units-enabled.txt" 2>/dev/null
  systemctl list-units --type=service --state=running --no-pager > "$out/units-running.txt" 2>&1
  crontab -l > "$out/crontab-root.txt" 2>/dev/null
  ls -A /etc/cron.d /etc/cron.daily > "$out/cron-dirs.txt" 2>/dev/null
  # NOTE: every command in here MUST be redirected. A stray un-redirected
  # `ip -o addr` earlier made fs_meta's stdout multi-line, so the `meta=$(fs_meta …)`
  # capture became "inet 192.168…\n…\n<node_lib>/fsmeta/<ts>" and restic
  # was handed garbage as a source path — reported as "does not exist, skipping"
  # plus a partial-snapshot exit code, which reads like a permissions problem.
  ip -o addr > "$out/network-addresses.txt" 2>&1
  ip route > "$out/network-routes.txt" 2>&1
  cat /etc/machine-id > "$out/machine-id" 2>/dev/null
  hostnamectl > "$out/hostnamectl.txt" 2>&1

  # Permissions beyond the mode bits. restic does not store ACLs or xattrs, so
  # capture them as text for the important trees. Bounded by timeout+size because
  # an unbounded `getfacl -R /home` on a 1.6 TB tree is how a backup job eats a night.
  if command -v getfacl >/dev/null; then
    { echo "# POSIX ACLs for selected trees. restic does NOT back these up."
      echo "# Re-apply on the restored system with:  setfacl --restore=<this file>"
      echo "# Scope below is deliberate: /home,/root,/storage,/mnt are NOT included"
      echo "# (cost), so ACLs there are LOST on restore. Flagged, not hidden."
      timeout 300 getfacl -pR --exclusive /etc /boot /srv /root 2>/dev/null
    } > "$out/acls.txt" 2>/dev/null
    wc -c "$out/acls.txt" | awk '{printf "  acls.txt: %s bytes\n", $1}' >&2
  fi
  if command -v getfattr >/dev/null; then
    { echo "# Extended attributes for /etc /boot /srv (restic does not store these)."
      echo "# Re-apply with: getfattr --restore=<this file>"
      timeout 300 getfattr -R --absolute-names /etc /boot /srv 2>/dev/null
    } > "$out/xattrs.txt" 2>/dev/null
  fi

  # AI-cluster specifics. Restoring the filesystem but losing the driver/toolkit
  # pairing turns a "successful restore" into a node that cannot see its GPUs.
  { echo "nvidia_smi: $(command -v nvidia-smi || echo absent)"
    nvidia-smi --query-gpu=index,name,uuid,driver_version,memory.total --format=csv 2>/dev/null
    echo; nvidia-smi --query 2>/dev/null | head -40
    echo "dkms:"; dkms status 2>/dev/null
    echo "modules-load.d:"; cat /etc/modules-load.d/*.conf 2>/dev/null
    echo "loaded(nvidia*):"; lsmod | awk '/^nvidia|^amdgpu|^mlx/{print}'
  } > "$out/gpu.txt" 2>&1
  { for c in nvcc nvidia-smi cuda --version; do :; done
    nvcc --version 2>/dev/null
    ls -d /usr/local/cuda* 2>/dev/null
    python3 -c "import torch,sys;print('torch',torch.__version__,'cuda',torch.version.cuda,'avail',torch.cuda.is_available())" 2>/dev/null
    pip list 2>/dev/null | egrep -i '^(torch|tensorflow|jax|triton|nvidia-|vllm)' 
  } > "$out/ai-toolchain.txt" 2>&1

  ln -sfn "$out" "$META/current"
  echo "$out"
}

# ── backup ────────────────────────────────────────────────────────────
cmd_backup() {
  local profile="system" dry="" hooks=1 t0 t1 rc meta
  while [[ $# -gt 0 ]]; do case $1 in
    --profile) profile=$2; shift 2;;
    --dry-run) dry="--dry-run"; shift;;
    --no-hooks) hooks=0; shift;;
    *) die "unknown argument: $1";;
  esac; done

  local prof_conf="@NODE_ETC@/profile-${profile}.d"
  local paths_file="$prof_conf/paths" excl_file="$prof_conf/exclusions"
  [[ -r $paths_file ]] || die "no paths file for profile '$profile' ($paths_file)"

  local paths; mapfile -t paths < "$paths_file"
  # per-host policy (generated by `noctyx policy-apply`): one restic pattern per
  # line, "!pattern" re-includes. Passed AFTER the baseline list so a negation in it
  # wins (restic applies the last matching pattern). Used to read each line as an
  # exclude-FILE path — harmless only because the file was always empty.
  local -a excl=()
  [[ -r $excl_file ]] && excl=( "--exclude-file=$excl_file" )

  meta=$(fs_meta "$META/$(date -u +%Y%m%dT%H%M%SZ)" | tail -1) || die "fs-meta capture failed"
  # Validate, don't trust: fs_meta's contract is "print exactly one directory
  # path on stdout". Without this check a stray stdout line turns into a partial
  # backup that still saves a snapshot and still looks green in the summary.
  [[ -n $meta && -d $meta ]] || die "fs_meta returned '$meta' which is not a directory — refusing to back up with incomplete boot metadata"

  # a profile whose paths ARE separate mounts (a data volume) ships its own list:
  # the common one excludes the separate mounts (/mnt/** and the site's extras)
  local common_excl=@NODE_ETC@/exclusions-common.conf
  [[ -r @NODE_ETC@/exclusions-$profile.conf ]] && common_excl=@NODE_ETC@/exclusions-$profile.conf

  local -a tags=( --tag "@SNAPSHOT_TAG@" --tag "profile-$profile" --tag "host-$(hostname -s)" )
  local hostname_arg=( --host "$(hostname -s)" )
  local -a opts=()
  # restic — including the pinned 0.19.1 — has NO xattr or POSIX-ACL capture. There
  # is no --xattrs and no --with-acl; a flag like that fails with "unknown flag",
  # so the omission is loud rather than silent. The consequence is real though:
  # ACLs and extended attributes are NOT in the backup. We capture them as text in
  # fsmeta (see acls.txt below) so a restore can re-apply them with setfacl, but
  # that is a manual step, not a restic guarantee. Do not claim otherwise.
  #
  # Also deliberate: --one-file-system is NEVER used. /boot and /boot/efi are
  # separate mounts on these machines, so a --one-file-system "full system
  # backup" would silently omit the kernel and bootloader and still print success.
  # It is the single most dangerous edit available in this file.
  [[ -n $dry ]] && opts+=( "$dry" )
  [[ $VERBOSE == 1 ]] && opts+=( --verbose=2 )
  # PARENT. restic picks a parent by host + the exact path set; our path set
  # includes the timestamped fsmeta dir, so it NEVER matched and every run
  # re-read the whole filesystem ("no parent snapshot found, will read all
  # files": 3h52m on one node). Name the newest snapshot of this
  # host+profile explicitly; unchanged files are then skipped by mtime/size.
  local parent
  parent=$("${RESTIC[@]}" snapshots --no-lock --host "$(hostname -s)" --tag "profile-$profile" --latest 1 --json 2>/dev/null \
           | python3 -c 'import json,sys
try:
    s=json.load(sys.stdin); print(s[-1]["id"] if s else "")
except Exception: print("")' 2>/dev/null)
  if [[ -n $parent ]]; then opts+=( --parent "$parent" ); log "parent snapshot ${parent:0:8}"
  else log "no previous $profile snapshot for this host — first full read"; fi

  if [[ $hooks == 1 ]]; then
    for h in "$HOOKS/pre.d"/*.sh; do [[ -x $h ]] || continue
      log "pre-hook $(basename "$h")"; "$h" "$profile" || log "pre-hook failed (continuing): $(basename "$h")"; done
  fi

  t0=$(date +%s)
  log "backup start profile=$profile paths=${paths[*]} repo=$REPO_DISPLAY"
  # live transfer status over SSH (operator, 2026-09-26: "can't see any file
  # transfer status"). restic has no --progress on this build and writes
  # nothing on a non-TTY anyway, so the AGENT provides the heartbeat itself:
  # NIC tx-bytes (uploaded) + the restic process's read_bytes (scanned), every
  # 30 s. Version-agnostic, works even on 0.16 distro builds.
  local restic_pid=""
  # restic writes to a FILE, never to our (possibly doomed) stderr pipe; a
  # separate tail streams it to the driver and is the only thing that dies if
  # the driver goes away.
  "${RESTIC[@]}" backup "${opts[@]}" "${tags[@]}" "${hostname_arg[@]}" \
    --exclude-file="$common_excl" "${excl[@]}" \
    "$meta" "${paths[@]}" > "$STATE/last-backup.log" 2>&1 &
  restic_pid=$!
  tail -n +1 --pid="$restic_pid" -f "$STATE/last-backup.log" >&2 2>/dev/null &
  local ifc tx0=0 tx=0 rb=0 dur
  ifc=$(ip route get "${REST_URL#https://}" 2>/dev/null | awk '{for(i=1;i<=NF;i++)if($i=="dev")print $(i+1)}' | head -1)
  ifc=${ifc:-$(ip route show default 2>/dev/null | awk '{print $5; exit}')}
  [[ -n $ifc && -r /sys/class/net/$ifc/statistics/tx_bytes ]] && tx0=$(cat /sys/class/net/$ifc/statistics/tx_bytes)
  while sleep 30; do
    kill -0 "$restic_pid" 2>/dev/null || break
    rb=0; dur=$(( $(date +%s) - t0 ))
    tx=$tx0
    [[ -n $ifc && -r /sys/class/net/$ifc/statistics/tx_bytes ]] && tx=$(cat /sys/class/net/$ifc/statistics/tx_bytes)
    [[ -r /proc/$restic_pid/io ]] && rb=$(awk '/^read_bytes/{print $2}' /proc/$restic_pid/io)
    log "progress ${dur}s: uploaded $(( (tx - tx0) / 1000000 )) MB · scanned $(( rb / 1000000 )) MB"
    # same line for the UI's Machine Report, which reads it over its own ssh —
    # visible whoever started the job (tick, UI, CLI)
    printf '%s profile=%s elapsed=%ss uploaded_mb=%s scanned_mb=%s\n' "$(date -u +%FT%TZ)" "$profile" \
      "$dur" "$(( (tx - tx0) / 1000000 ))" "$(( rb / 1000000 ))" > "$STATE/progress"
  done
  wait "$restic_pid"; rc=$?
  rm -f "$STATE/progress"
  t1=$(date +%s)

  if [[ $hooks == 1 ]]; then
    for h in "$HOOKS/post.d"/*.sh; do [[ -x $h ]] || continue
      log "post-hook $(basename "$h")"; "$h" "$profile" "$rc" || true; done
  fi

  local snap; snap=$("${RESTIC[@]}" snapshots --no-lock --host "$(hostname -s)" --json 2>/dev/null \
                    | python3 -c 'import json,sys
try:
    s=json.load(sys.stdin); print(s[-1]["short_id"] if s else "")
except Exception: print("")' 2>/dev/null)
  local dur=$(( t1 - t0 ))
  emit "{\"event\":\"backup\",\"host\":\"$(hostname -s)\",\"profile\":\"$profile\",\"repo\":\"$REPO_ID\",\"snapshot\":\"$snap\",\"seconds\":$dur,\"rc\":$rc,\"ts\":\"$(date -u -Is)\"}"
  # Emit the audit line FIRST, then propagate the failure. A wrapper that dies
  # before writing the audit record turns one failed backup into an invisible
  # one — which is the difference between an alert and a missing file in March.
  [[ $rc -eq 0 ]] || { log "restic backup exited $rc"; exit "$rc"; }
}

cmd_verify() {
  local subset=${1:-2%} rc
  log "restic check (structural)"; "${RESTIC[@]}" check 2>&1 | tail -3 >&2; rc=$?
  log "restic check --read-data-subset=$subset"; "${RESTIC[@]}" check --read-data-subset="$subset" 2>&1 | tail -3 >&2; rc=$(( rc + $? ))
  emit "{\"event\":\"verify\",\"host\":\"$(hostname -s)\",\"repo\":\"$REPO_ID\",\"rc\":$rc,\"ts\":\"$(date -u -Is)\"}"
  return $rc
}

# ── retention (forget + prune) ─────────────────────────────────────────
# Retention flags arrive on the command line from the orchestrator (cluster.json
# is the single source of truth; the agent never hardcodes a policy).
# Safety notes, learned the hard way elsewhere:
#   * group-by includes TAGS: the agent tags snapshots profile-*; without tags
#     in the grouping a `system` and a `smoke` snapshot of the same host/day
#     would compete for the same keep-slot and silently delete the wrong one.
#   * NOT paths: each snapshot includes its own fsmeta/<timestamp> dir, so a
#     path grouping is a group of one per snapshot and nothing is ever removed.
#   * --prune only with --dry-run absent; forget --dry-run alone never deletes.
cmd_retention() {
  local dry="--dry-run" rc
  while [[ $# -gt 0 ]]; do case $1 in
    --apply) dry=""; shift;;
    *) break;;
  esac; done
  [[ -n $dry ]] || log "APPLY mode: snapshots matching the forget policy will be DELETED"
  log "forget ${dry:---apply} $*"
  "${RESTIC[@]}" forget $dry --group-by host,tags "$@" 2>&1 | tee "$STATE/last-retention.log" >&2
  rc=${PIPESTATUS[0]}
  if [[ $rc -eq 0 && -z $dry ]]; then
    log "prune (removing unreferenced pack data)"
    "${RESTIC[@]}" prune 2>&1 | tail -3 >&2; rc=$(( rc + $? ))
  fi
  emit "{\"event\":\"retention\",\"host\":\"$(hostname -s)\",\"repo\":\"$REPO_ID\",\"apply\":$([[ -z $dry ]] && echo true || echo false),\"rc\":$rc,\"ts\":\"$(date -u -Is)\"}"
  return $rc
}

# ── hygiene: find host-housekeeping problems; fix the provably safe ones ──
# Items carry a tier: "auto" (cannot break anything: ghost units, a killed user
# manager, a reboot flag that is demonstrably stale), "manual" (reclaims space
# or removes packages — a human clicks), or none (report only: a real failed
# service, a real pending reboot). `hygiene [--fix auto|manual|ID,ID]`
hyg_items=()
hyg_add() { # <id> <severity> <title> <detail> <tier> [fix-label]  — fix-label names what the button will do
  hyg_items+=("$(python3 -c 'import json,sys;a=sys.argv[1:];print(json.dumps({"id":a[0],"severity":a[1],"title":a[2],"detail":a[3],"tier":a[4] or None,"fixed":False,"fix":(a[5] if len(a)>5 and a[5] else None)}))' "$@")"); }
hyg_mark_fixed() { local id=$1 i; for i in "${!hyg_items[@]}"; do
    [[ ${hyg_items[$i]} == *"\"id\": \"$id\""* ]] && hyg_items[$i]=${hyg_items[$i]/\"fixed\": false/\"fixed\": true}; done; }
hyg_want_fix() { # <tier> <id>
  [[ -z $HYG_FIX ]] && return 1
  [[ $HYG_FIX == all ]] && return 0
  [[ $HYG_FIX == "$1" ]] && return 0
  [[ ",$HYG_FIX," == *",$2,"* ]] && return 0
  return 1; }

cmd_hygiene() {
  HYG_FIX=""
  while [[ $# -gt 0 ]]; do case $1 in --fix) HYG_FIX=$2; shift 2;; *) die "hygiene: unknown argument $1";; esac; done
  local u ls ss rc

  # 1. failed units — ghosts (unit file gone) vs real
  while read -r u; do
    [[ -n $u ]] || continue
    ls=$(systemctl show "$u" -p LoadState --value 2>/dev/null)
    if [[ $ls == not-found ]]; then
      hyg_add "unit:$u" info "ghost failed unit $u" "its unit file no longer exists; only the failed state lingers" auto
      hyg_want_fix auto "unit:$u" && systemctl reset-failed "$u" 2>/dev/null && hyg_mark_fixed "unit:$u"
    elif [[ $u == user@*.service ]] && [[ $(systemctl show "$u" -p Result --value) == signal ]]; then
      hyg_add "unit:$u" warning "user session manager $u killed" "killed by a signal ($(systemctl show "$u" -p ExecMainStatus --value)); per-user services are down until restarted" auto
      hyg_want_fix auto "unit:$u" && systemctl reset-failed "$u" && systemctl start "$u" 2>/dev/null && hyg_mark_fixed "unit:$u"
    else
      # a real failed unit: offer the fix that fits. Something meant to be running (enabled,
      # or Restart= set) gets a restart; a one-shot / benchmark / static unit just gets its
      # failed state cleared. Only by explicit id — never by tier — so `--fix manual`
      # (bulk space reclaim) and the "fix all" button can never restart a service.
      local en rs fix
      en=$(systemctl is-enabled "$u" 2>/dev/null); rs=$(systemctl show "$u" -p Restart --value 2>/dev/null)
      if [[ $en == enabled || $en == enabled-runtime || ( -n $rs && $rs != no ) ]]; then fix=restart; else fix="clear failed state"; fi
      hyg_add "unit:$u" warning "failed unit $u" "$(journalctl -u "$u" -n 3 --no-pager -o cat 2>/dev/null | tr '\n' ' ' | cut -c1-240)" manual "$fix"
      if hyg_want_fix explicit "unit:$u"; then
        if [[ $fix == restart ]]; then systemctl reset-failed "$u" 2>/dev/null; systemctl restart "$u" 2>/dev/null && hyg_mark_fixed "unit:$u"
        else systemctl reset-failed "$u" 2>/dev/null && hyg_mark_fixed "unit:$u"; fi
      fi
    fi
  done < <(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}')

  # 2. reboot flag — stale if the running kernel is the newest installed and the
  #    loaded NVIDIA module matches its package
  if [[ -e /var/run/reboot-required ]]; then
    local run newest nvmod nvpkg stale=1 why=""
    run=$(uname -r)
    newest=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1)
    [[ -n $newest && $newest != "$run" ]] && { stale=0; why="kernel $newest installed, running $run"; }
    if [[ -r /sys/module/nvidia/version ]]; then
      nvmod=$(cat /sys/module/nvidia/version)
      nvpkg=$(dpkg-query -W -f='${Version}\n' 'nvidia-kernel-common-*' 2>/dev/null | head -1 | cut -d- -f1)
      [[ -n $nvpkg && $nvpkg != "$nvmod" ]] && { stale=0; why="${why:+$why; }nvidia module $nvmod, package $nvpkg"; }
    fi
    if [[ $stale == 1 ]]; then
      hyg_add reboot-flag info "reboot flag is stale" "set by $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null); running kernel $run is the newest installed and the GPU driver matches" auto
      hyg_want_fix auto reboot-flag && rm -f /var/run/reboot-required /var/run/reboot-required.pkgs && hyg_mark_fixed reboot-flag
    else
      hyg_add reboot-needed warning "reboot needed" "$why" ""
    fi
  fi

  # 3. space: autoremovable packages, old kernels, journal, docker leftovers
  local n
  n=$(apt-get -s autoremove 2>/dev/null | grep -c '^Remv ')
  if (( n > 0 )); then
    hyg_add apt-autoremove info "$n package(s) no longer needed" "$(apt-get -s autoremove 2>/dev/null | grep -i 'disk space will be freed' | head -1 | sed 's/^After this operation, //')" manual
    hyg_want_fix manual apt-autoremove && DEBIAN_FRONTEND=noninteractive apt-get -y -q autoremove --purge >/dev/null 2>&1 && hyg_mark_fixed apt-autoremove
  fi
  local runk old
  runk=$(uname -r)
  old=$(dpkg-query -W -f='${Package}\n' 'linux-image-[0-9]*' 2>/dev/null | grep -v "$runk" | sort -V | head -n -1 | tr '\n' ' ')
  if [[ -n ${old// /} ]]; then
    hyg_add old-kernels info "old kernels installed" "$old (running $runk; newest kept)" manual
    hyg_want_fix manual old-kernels && DEBIAN_FRONTEND=noninteractive apt-get -y -q purge $old >/dev/null 2>&1 && hyg_mark_fixed old-kernels
  fi
  local jb
  jb=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | head -1)
  if [[ $jb == *G ]] && (( ${jb%.*G} >= 1 )); then
    hyg_add journal info "journal uses $jb" "vacuum to 500M keeps recent history" manual
    hyg_want_fix manual journal && journalctl --vacuum-size=500M >/dev/null 2>&1 && hyg_mark_fixed journal
  fi
  if command -v docker >/dev/null 2>&1; then
    local dang stopped
    dang=$(docker images -f dangling=true -q 2>/dev/null | wc -l); stopped=$(docker ps -aq -f status=exited 2>/dev/null | wc -l)
    if (( dang + stopped > 0 )); then
      hyg_add docker-prune info "docker leftovers" "$dang dangling image(s), $stopped exited container(s)" manual
      hyg_want_fix manual docker-prune && docker image prune -f >/dev/null 2>&1 && docker container prune -f >/dev/null 2>&1 && hyg_mark_fixed docker-prune
    fi
  fi

  # 4. report-only: logrotate config errors, root filesystem, services needing restart
  local lr
  lr=$(logrotate -d /etc/logrotate.conf 2>&1 | grep -iE '^error' | head -2 | tr '\n' ' ')
  [[ -n $lr ]] && hyg_add logrotate warning "logrotate config error" "$lr" ""
  local rootp
  rootp=$(df -P / | awk 'NR==2{gsub("%","",$5);print $5}')
  (( rootp >= 85 )) && hyg_add root-space warning "root filesystem ${rootp}% full" "$(df -h / | awk 'NR==2{print $4" free"}')" ""
  if command -v needrestart >/dev/null 2>&1; then
    local svcs
    svcs=$(needrestart -b -r l 2>/dev/null | awk '/^NEEDRESTART-SVC:/{print $2}' | grep -vE '^(@UNIT_PREFIX@|vllm|docker|containerd|ssh)' | tr '\n' ' ')
    [[ -n ${svcs// /} ]] && hyg_add needrestart info "services running outdated binaries" "$svcs" ""
  fi

  local json
  json=$(printf '%s\n' "${hyg_items[@]}" | python3 -c 'import json,sys;items=[json.loads(l) for l in sys.stdin if l.strip()];print(json.dumps(items))')
  emit "{\"event\":\"hygiene\",\"host\":\"$(hostname -s)\",\"fix\":\"${HYG_FIX}\",\"items\":$json,\"ts\":\"$(date -u -Is)\"}"
}

mkdir -p "$STATE"
case "${1:-backup}" in
  hygiene) shift; take_job_lock; cmd_hygiene "$@";;
  backup) shift; take_job_lock; cmd_backup "$@";;
  fs-meta) take_job_lock; fs_meta "$META/$(date -u +%Y%m%dT%H%M%SZ)";;
  verify) shift; take_job_lock; cmd_verify "$@";;
  retention) shift; take_job_lock; cmd_retention "$@";;
  # NOTE the ordering bug this replaced: `shift` then `"${1:-snapshots}"` meant
  # `--json` became the subcommand and `snapshots` was passed as a path.
  list|snapshots)
    shift
    # --no-lock: read-only, and must work while a backup holds the repo
    exec "${RESTIC[@]}" snapshots --no-lock --host "$(hostname -s)" "$@";;
  # Clear stale repo locks left by a killed/interrupted backup. Safe only when
  # no backup is genuinely running here — the orchestrator calls this after a
  # lock-shaped error and immediately retries, so a live writer would show up
  # as a lock failure on the retry instead (restic re-locks on start).
  unlock)
    shift; take_job_lock
    "${RESTIC[@]}" unlock "$@" > "$STATE/last-unlock.log" 2>&1; rc=$?
    tail -3 "$STATE/last-unlock.log" >&2
    emit "{\"event\":\"unlock\",\"host\":\"$(hostname -s)\",\"repo\":\"$REPO_ID\",\"rc\":$rc,\"ts\":\"$(date -u -Is)\"}";;
  # Restore a path from a snapshot. Default target is a fresh directory under
  # $STATE/restore/ — never the live tree; --target / is the operator's explicit
  # in-place choice (the orchestrator only sends it with --in-place --yes).
  restore)
    shift; take_job_lock
    snap=latest; rpath=""; target="$STATE/restore/$(date -u +%Y%m%dT%H%M%SZ)"
    while [[ $# -gt 0 ]]; do case $1 in
      --snapshot) snap=$2; shift 2;; --path) rpath=$2; shift 2;; --target) target=$2; shift 2;;
      *) die "restore: unknown argument $1";; esac; done
    [[ -n $rpath && ${rpath:0:1} == / ]] || die "restore: --path must be absolute"
    [[ $snap =~ ^([0-9a-f]{8,64}|latest)$ ]] || die "restore: bad --snapshot"
    mkdir -p "$target"
    log "restore $snap $rpath -> $target"
    "${RESTIC[@]}" restore "$snap" --host "$(hostname -s)" --include "$rpath" --target "$target" 2>&1 \
      | tee "$STATE/last-restore.log" >&2; rc=${PIPESTATUS[0]}
    emit "{\"event\":\"restore\",\"host\":\"$(hostname -s)\",\"snapshot\":\"$snap\",\"path\":\"$rpath\",\"target\":\"$target\",\"rc\":$rc,\"ts\":\"$(date -u -Is)\"}"
    exit "$rc";;
  check)
    shift; take_job_lock
    exec "${RESTIC[@]}" check "$@";;
  *) die "unknown subcommand: ${1:-}";;
esac
