#!/bin/bash
# noctyx-menu.sh — Noctyx recovery menu. Started on tty1 at boot by the stick's
# CIDATA seed (cloud-init → noctyx-recovery.service), in place of the Ubuntu
# installer. Runs from the @STICK_LABEL@ partition next to noctyx-restore.sh.
#
# It never writes a disk itself: every restore is `noctyx-restore.sh`, which still
# asks for the device name to be typed before it destroys anything.
#
#   targets:  server (discovered, or the stick's default) · host (pre-selected by
#             this machine's MAC, else by arch) · snapshot (newest `system`) · disk
#   actions:  dry run · RESTORE · shell · reboot · power off
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ENGINE="$HERE/noctyx-restore.sh"
LOG=/run/nx-menu.log
T="Noctyx recovery"
export NEWT_COLORS='root=white,blue'

# stick defaults (written by make-restore-usb.sh)
DEFAULT_SERVER=""                      # stick.conf sets it (the head node when the stick was built)
[[ -r $HERE/stick.conf ]] && . "$HERE/stick.conf"

ARCH=$(uname -m)
SERVER="" HOST="" SNAP="" SNAP_DESC="" DISK=""
H=$(tput lines 2>/dev/null || echo 24); W=$(tput cols 2>/dev/null || echo 80)
(( H > 40 )) && H=40; (( W > 100 )) && W=100
LH=$(( H - 8 ))

msg(){ whiptail --title "$T" --msgbox "$1" $((H-4)) $((W-4)); }
info(){ whiptail --title "$T" --infobox "$1" 8 $((W-4)); }

# ── network: the restore needs the head node; wait for a lease, offer a fix ──
wait_net(){
  local i
  for i in $(seq 1 45); do
    ip -4 route show default 2>/dev/null | grep -q . && return 0
    info "Waiting for the network (DHCP)… ${i}s\n\n$(ip -br -4 addr | grep -v '^lo')"
    sleep 1
  done
  return 1
}

net_fix(){
  local ifs ifc addr gw
  ifs=$(ip -br link | awk '$1!="lo"{print $1, $2}')
  # shellcheck disable=SC2086
  ifc=$(whiptail --title "$T" --menu "No network. Pick the interface cabled to the LAN:" $H $W $LH \
        $(echo "$ifs" | awk '{print $1, $2}') 3>&1 1>&2 2>&3) || return 1
  ip link set "$ifc" up
  if whiptail --title "$T" --yesno "Try DHCP on $ifc first?" 8 60; then
    info "DHCP on $ifc…"; (dhclient -1 -timeout 20 "$ifc" || networkctl reconfigure "$ifc") >/dev/null 2>&1
    sleep 3; ip -4 route show default | grep -q . && return 0
  fi
  addr=$(whiptail --title "$T" --inputbox "Static address for $ifc (CIDR):" 8 60 "${DEFAULT_SERVER%.*}.240/24" 3>&1 1>&2 2>&3) || return 1
  gw=$(whiptail --title "$T" --inputbox "Gateway:" 8 60 "${DEFAULT_SERVER%.*}.1" 3>&1 1>&2 2>&3) || return 1
  ip addr add "$addr" dev "$ifc" && ip route replace default via "$gw" dev "$ifc"
}

# ── server: discovery first, the stick's default second ──
discover(){
  local r
  r=$(printf 'NX_DISCOVER?' | timeout 4 nc -u -b -w2 -p 40001 255.255.255.255 20997 2>/dev/null)
  [[ -n $r ]] && printf '%s' "$r" | python3 -c 'import json,sys; print(json.load(sys.stdin)["address"])' 2>/dev/null
}

engine(){ bash "$ENGINE" --server "$SERVER" "$@" 2>>"$LOG"; }

load_hosts(){
  HOSTS=$(engine --list hosts) && [[ -n $HOSTS ]]
}

# this machine's MACs vs the inventory → the host it most likely is
guess_host(){
  local mine h macs arch m
  mine=$(cat /sys/class/net/*/address 2>/dev/null | sort -u)
  while IFS=$'\t' read -r h arch _ _ _ macs; do
    for m in ${macs//,/ }; do grep -qx "$m" <<<"$mine" && { echo "$h"; return; }; done
  done <<<"$HOSTS"
  # no MAC match (new board, swapped NIC): first host of this arch
  awk -F'\t' -v a="$ARCH" '$2==a{print $1; exit}' <<<"$HOSTS"
}

host_field(){ awk -F'\t' -v h="$HOST" -v f="$1" '$1==h{print $f}' <<<"$HOSTS"; }

pick_server(){
  local s
  s=$(whiptail --title "$T" --inputbox "Noctyx head node address:" 8 60 "$SERVER" 3>&1 1>&2 2>&3) || return
  SERVER=$s
  info "Reading inventory from $SERVER…"
  load_hosts || msg "Could not read the inventory from $SERVER.\n\n$(tail -5 $LOG)"
}

pick_host(){
  local args=() h arch repo tpl note
  while IFS=$'\t' read -r h arch repo _ tpl _; do
    note="$arch  repo=$repo"
    [[ $arch != "$ARCH" ]] && note="$note  (other arch)"
    [[ $tpl == gpt-esp-boot-lvm ]] && note="$note  (head node: see runbook)"
    args+=("$h" "$note")
  done <<<"$HOSTS"
  h=$(whiptail --title "$T" --default-item "$HOST" --menu "Which machine is being restored?" $H $W $LH "${args[@]}" 3>&1 1>&2 2>&3) || return
  [[ $h != "$HOST" ]] && { HOST=$h; SNAP="" SNAP_DESC="" SNAPS=""; DISK=$(host_field 4); load_latest; }
}

load_latest(){
  info "Reading snapshots of $HOST…"
  SNAPS=$(engine --host "$HOST" --list snapshots) || { SNAPS=""; return 1; }
  local line; line=$(awk -F'\t' '$3=="system"{print; exit}' <<<"$SNAPS")
  SNAP=$(cut -f1 <<<"$line"); SNAP_DESC=$(cut -f2 <<<"$line")
}

pick_snapshot(){
  [[ -n ${SNAPS:-} ]] || load_latest || { msg "Could not list snapshots of $HOST.\n\n$(tail -5 $LOG)"; return; }
  local args=() id t p sz
  while IFS=$'\t' read -r id t p sz; do
    [[ $p == system ]] || continue           # only whole-system snapshots rebuild a disk
    args+=("$id" "$t UTC  $sz")
  done <<<"$SNAPS"
  (( ${#args[@]} )) || { msg "$HOST has no 'system' snapshots."; return; }
  id=$(whiptail --title "$T" --default-item "$SNAP" --menu "Snapshot of $HOST (newest first):" $H $W $LH "${args[@]}" 3>&1 1>&2 2>&3) || return
  SNAP=$id; SNAP_DESC=$(awk -F'\t' -v i="$id" '$1==i{print $2}' <<<"$SNAPS")
}

pick_disk(){
  local args=() n s m t boot
  boot=$(findmnt -nvo SOURCE /cdrom 2>/dev/null; lsblk -nro PKNAME,LABEL | awk '$2=="@STICK_LABEL@"{print "/dev/"$1}')
  while read -r n s t m; do
    grep -q "^$n$" <<<"$boot" && continue    # never offer the stick itself
    args+=("$n" "$s  ${m:-?}  ($t)")
  done < <(lsblk -dnpo NAME,SIZE,TRAN,MODEL -e 7,11 | awk '{n=$1;s=$2;t=$3;$1=$2=$3="";sub(/^ +/,"");print n,s,t,$0}')
  (( ${#args[@]} )) || { msg "No disks found."; return; }
  n=$(whiptail --title "$T" --default-item "$DISK" --menu "Disk to ERASE and rebuild (inventory says $(host_field 4)):" $H $W $LH "${args[@]}" 3>&1 1>&2 2>&3) || return
  DISK=$n
}

run_engine(){   # run_engine [--dry-run]
  clear
  local tpl; tpl=$(host_field 5)
  if [[ $tpl == gpt-esp-boot-lvm ]]; then
    msg "$HOST is the head node (LVM). This menu's engine does not rebuild it.\n\nUse docs/runbooks/rebuild-head-node.md (noctyx-restore-head.sh) from the shell."
    return
  fi
  echo "noctyx-restore.sh --server $SERVER --host $HOST --snapshot $SNAP --disk $DISK $*"
  echo
  bash "$ENGINE" --server "$SERVER" --host "$HOST" --snapshot "$SNAP" --disk "$DISK" "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  echo; echo "── exit code $rc ── press Enter to return to the menu"; read -r _
  if [[ $rc == 0 && $* != *--dry-run* ]] && whiptail --title "$T" --yesno "Restore finished.\n\nRemove the stick and reboot now?" 10 60; then
    reboot
  fi
}

# ── start ──
clear
wait_net || { msg "No default route after 45 s."; net_fix; }
info "Looking for the Noctyx server…"
SERVER=$(discover); SERVER=${SERVER:-$DEFAULT_SERVER}
until load_hosts; do
  whiptail --title "$T" --yesno "Cannot read the inventory from $SERVER.\n\n$(tail -4 $LOG)\n\nFix the network / server and retry?" $((H-4)) $((W-4)) || break
  net_fix || pick_server
done
HOST=$(guess_host); DISK=$(host_field 4)
[[ -n $HOST ]] && load_latest

while :; do
  ip4=$(ip -4 -br addr | awk '$1!="lo"{print $1" "$3}' | head -2 | tr '\n' ' ')
  c=$(whiptail --title "$T" --ok-button Select --cancel-button Shell --menu \
"This machine: $ARCH · $ip4
Nothing is written until you pick RESTORE and type the disk name." $H $W $LH \
    1 "Host       ${HOST:-?}" \
    2 "Snapshot   ${SNAP:-?}  ${SNAP_DESC:+($SNAP_DESC UTC)}" \
    3 "Disk       ${DISK:-?}  $(lsblk -dnro SIZE,MODEL "$DISK" 2>/dev/null | head -1)" \
    4 "Server     $SERVER" \
    5 "Dry run (reads everything, writes nothing)" \
    6 "RESTORE" \
    7 "Shell" \
    8 "Reboot" \
    9 "Power off" 3>&1 1>&2 2>&3) || c=7
  case $c in
    1) pick_host;; 2) pick_snapshot;; 3) pick_disk;; 4) pick_server; HOST=$(guess_host);;
    5) [[ -n $HOST && -n $SNAP && -n $DISK ]] && run_engine --dry-run || msg "Pick host, snapshot and disk first.";;
    6) [[ -n $HOST && -n $SNAP && -n $DISK ]] && run_engine || msg "Pick host, snapshot and disk first.";;
    7) clear; echo "Shell. 'exit' returns to the menu. Engine: $ENGINE  log: $LOG"; bash -l;;
    8) reboot;; 9) poweroff;;
  esac
done
