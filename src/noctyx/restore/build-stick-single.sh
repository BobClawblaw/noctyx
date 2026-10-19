#!/usr/bin/env bash
# build-stick-single.sh — one command: self-booting Noctyx recovery USB.
#
#   build-stick-single.sh --arch amd64 --usb /dev/sdX            (full build)
#   build-stick-single.sh --arch arm64 --usb /dev/sdX
#   build-stick-single.sh --arch amd64 --dry-run                 (no writes)
#   build-stick-single.sh --fetch-only --arch amd64              (refresh cache)
#
# arm64 ISOs (DGX Spark-class nodes) have an MBR table, not a GPT: same verbatim
# dd, then p3 is appended to the MBR with sfdisk instead of the GPT rewrite.
#
# Method: the verified way an Ubuntu live ISO boots — write it VERBATIM at the
# raw device start (isohybrid layout: protective MBR + GPT with p1 ISO9660 +
# p2 ESP, all at the ISO's own LBAs). Then rewrite the GPT for the real stick
# size (stick_gpt.py): p1/p2 entries kept byte-identical (labels +
# PARTUUIDs unchanged, so firmware and casper see the media they expect), p3
# @STICK_LABEL@ appended in the tail space. The ISO does NOT depend on its
# backup-GPT location (firmware reads the primary table; loop tests prove
# partitions mount when the backup CRC fails), so the tail relocation is safe.
#
# Steps: stage (download + SHA256 verify) → gated dd (type-device name) →
# GPT relocate+append → mkfs @STICK_LABEL@ → live payload via
# make-restore-usb.sh → verify (GPT valid, ISO slice byte-exact, payload
# sha256 parity, key decrypts live repo, engine matches directory copy).
#
# Nothing destructive happens before the typed-device gate.
# Run through `noctyx tool build-stick-single`, which fills in this site's paths.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISO_DIR="${NX_ISO_DIR:-@ISO_DIR@}"
SUDO=sudo
MIN_STICK_MB=24000

ARCH="" USB="" DRY="" FETCH_ONLY="" FORCE_ISO="" FINISH=""
while [[ $# -gt 0 ]]; do case $1 in
  --arch) ARCH=$2; shift 2;;
  --usb) USB=$2; shift 2;;
  --iso) FORCE_ISO=$2; shift 2;;
  --dry-run) DRY=1; shift;;
  --fetch-only) FETCH_ONLY=1; shift;;
  --finish) FINISH=$2; shift 2;;   # resume: device already dd'd, p3 exists
  *) echo "usage: $0 --arch amd64|arm64 [--usb /dev/sdX | --dry-run | --fetch-only | --finish /dev/sdX] [--iso PATH]" >&2; exit 1;;
esac; done

log(){ printf '\n\033[1m[noctyx-stick]\033[0m %s\n' "$*"; }
# amd64 live ISOs carry an isohybrid GPT (stick_gpt.py rewrites it); arm64 ones only an MBR
iso_has_gpt(){ python3 -c "import sys;f=open('$ISO','rb');f.seek(512);sys.exit(0 if f.read(8)==b'EFI PART' else 1)"; }
die(){ printf '\n\033[31m[noctyx-stick FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

case $ARCH in amd64|arm64) :;; *) die "--arch must be amd64 or arm64";; esac
NAME="noble-live-server-$ARCH.iso"
BASE="https://cdimage.ubuntu.com/ubuntu-server/noble/daily-live/current"

# ── 1. stage: verified ISO in cache ─────────────────────────────────────
stage_iso(){
  $SUDO mkdir -p "$ISO_DIR"
  if [[ -n $FORCE_ISO ]]; then
    [[ -f $FORCE_ISO ]] || die "--iso $FORCE_ISO not found"
    ISO=$FORCE_ISO; return
  fi
  local want have
  want=$($SUDO sh -c "curl -fsSL $BASE/SHA256SUMS | awk -v n=$NAME '{gsub(\"\\\\*\",\" \");if(\$2==n)print \$1}'") \
       || die "could not fetch SHA256SUMS from $BASE"
  [[ -n $want ]] || die "$NAME not listed in upstream SHA256SUMS"
  ISO=$ISO_DIR/$NAME
  if have=$($SUDO sh -c "[ -f $ISO ] && sha256sum $ISO | cut -d' ' -f1") && [[ "$have" == "$want" ]]; then
    log "cache hit: $NAME (sha256 verified against upstream)"
  else
    local size
    size=$(curl -sI "$BASE/$NAME" | awk 'tolower($1)=="content-length:"{print $2}' | tr -d '\r')
    log "downloading $NAME ($((${size:-0}/1000/1000)) MB) → $ISO"
    [[ -n $DRY ]] && { echo "DRY: curl -o $ISO $BASE/$NAME"; return; }
    $SUDO curl -fL --retry 3 -o "$ISO.part" "$BASE/$NAME"
    $SUDO mv "$ISO.part" "$ISO"
    have=$($SUDO sha256sum "$ISO" | cut -d' ' -f1)
    [[ "$have" == "$want" ]] || { $SUDO rm -f "$ISO"; die "sha256 mismatch after download (got $have want $want) — deleted"; }
    log "sha256 verified: $have"
  fi
  echo "$want  $NAME" | $SUDO tee "$ISO.sha256" >/dev/null
}

# ── 2. gated write ──────────────────────────────────────────────────────
write_stick(){
  [[ -b $USB ]] || die "$USB is not a block device"
  RP=$(realpath "$USB")
  [[ $RP == /dev/sd* ]] || die "unexpected resolved device $RP"
  local bytes model
  bytes=$(blockdev --getsize64 "$RP")
  (( bytes/1000/1000 >= MIN_STICK_MB )) || die "$RP is $((bytes/1000/1000)) MB < ${MIN_STICK_MB} MB minimum — wrong device?"
  local iso_bytes; iso_bytes=$(stat -c %s "$ISO")
  (( bytes > iso_bytes + 800*1024*1024 )) || die "ISO ($iso_bytes) leaves < 800 MB tail on $RP for @STICK_LABEL@"
  lsblk -nrpo MOUNTPOINT "$RP" | grep -q . && die "a partition of $RP is mounted — unmount first"
  model=$(lsblk -dnro MODEL "$RP" | tr -s ' ')
  echo
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  DESTRUCTIVE: $RP  ($((bytes/1000/1000)) MB  ${model:-no-model})"
  echo "║  EVERY partition on it will be erased.                       ║"
  echo "║  Type the device name to proceed (anything else aborts):     ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  local ans; read -rp "> " ans || die "no confirmation"
  [[ "$ans" == "$(basename "$RP")" || "$ans" == "$RP" ]] || die "confirmation '$ans' did not match — aborted"

  log "dd $ISO → $RP (isohybrid, offset 0)"
  $SUDO dd if="$ISO" of="$RP" bs=4M status=progress oflag=sync
  $SUDO sync

  if iso_has_gpt; then
    log "rewriting GPT for stick size: p1/p2 kept verbatim, p3 @STICK_LABEL@ appended"
    $SUDO python3 "$HERE/stick_gpt.py" "$ISO" "$RP"
  else
    # The arm64 live ISO ships an MBR table only (p1 ISO9660 type cd, p2 ESP type ef,
    # bootaa64.efi inside) — UEFI boots MBR media fine. Append p3 (FAT32 LBA) in the
    # tail; sfdisk picks the first free, 1 MiB-aligned sector. Kernel re-read handled below.
    log "ISO has an MBR table (arm64 image): appending p3 @STICK_LABEL@ to the MBR, p1/p2 untouched"
    printf 'type=0c\n' | $SUDO sfdisk --append --no-reread --no-tell-kernel "$RP" >/dev/null
  fi
  # Kernel reread often fails right after dd (stale handles on the old slices).
  # The table on disk is what matters; if the kernel won't see it, re-attach
  # the USB device and continue on the new node name.
  # The kernel usually catches up within a second or two of partprobe (dmesg shows
  # "sde: sde1 sde2 sde3" ~0.5 s after an earlier build declared failure): poll first.
  sees_p3(){ local i; for i in 1 2 3 4 5 6 7 8 9 10; do
               $SUDO partprobe "$RP" 2>/dev/null || $SUDO blockdev --rereadpt "$RP" 2>/dev/null || true
               lsblk -nro NAME "$RP" 2>/dev/null | grep -q "$(basename "$RP")3" && return 0; sleep 1; done; return 1; }
  if ! sees_p3; then
    log "kernel kept the old table busy — re-attaching the USB device"
    local devpath hubport
    devpath=$(udevadm info -q path -n "$RP" 2>/dev/null)
    hubport=$(echo "$devpath" | grep -oE "[0-9]+-[0-9.]+")
    if [[ -n $hubport && -e /sys/bus/usb/devices/$hubport/authorized ]]; then
      echo 0 | $SUDO tee /sys/bus/usb/devices/$hubport/authorized >/dev/null
      sleep 2
      echo 1 | $SUDO tee /sys/bus/usb/devices/$hubport/authorized >/dev/null
      sleep 5
      RP=$(lsblk -dnro NAME,MODEL | awk '/[Ff]lash|[Uu]SB [Mm]ass/ {print "/dev/"$1; exit}')
      [[ -b $RP ]] || die "device did not come back after USB reset — replug it manually, then re-run"
      log "device is back as $RP"
    fi
  fi
  sees_p3 || die "kernel still cannot see ${RP}3 — replug the stick, then resume with: $0 --arch $ARCH --finish $RP (after mkfs.vfat -F 32 -n @STICK_LABEL@ ${RP}3)"
  if iso_has_gpt; then
    $SUDO sgdisk -v "$RP" >/dev/null || die "GPT invalid after rewrite (dd the ISO again to reset, then re-run)"
  else
    $SUDO sfdisk --verify "$RP" >/dev/null 2>&1 || die "MBR table invalid after append (dd the ISO again to reset, then re-run)"
  fi

  log "mkfs.vfat @STICK_LABEL@ on ${RP}3"
  $SUDO mkfs.vfat -F 32 -n @STICK_LABEL@ "${RP}3"
  $SUDO partprobe "$RP"; sleep 1

  log "deploying LIVE payload via make-restore-usb.sh"
  $SUDO env NOCTYX_CONFIG="$NOCTYX_CONFIG" bash "$HERE/make-restore-usb.sh" --usb "${RP}3"

  log "recovery README onto the ESP (documentation; firmware ignores it)"
  $SUDO mkdir -p /mnt/nx-esp
  if $SUDO mount "${RP}2" /mnt/nx-esp; then
    $SUDO tee /mnt/nx-esp/README.TXT >/dev/null <<'EOF'
@STICK_LABEL@ RECOVERY
1. Boot this stick. Grub: "Try or Install Ubuntu Server" -> at the first
   installer screen choose "Try Ubuntu" (live session) - NOT install.
2. lsblk -f | grep @STICK_LABEL@   -> sudo mount <that-partition> /mnt
3. sudo bash /mnt/noctyx-restore.sh --server @HEAD_ADDR@ --host NAME
   (it finds the keys and logins in /mnt/keys and /mnt/creds by itself)
   The head node itself: sudo bash /mnt/noctyx-restore-head.sh --help
EOF
    $SUDO umount /mnt/nx-esp
  fi
}

# ── 3. verify ───────────────────────────────────────────────────────────
verify_stick(){
  RP=$(realpath "$USB")
  log "verify: partition table valid, @STICK_LABEL@ present"
  if iso_has_gpt; then $SUDO sgdisk -v "$RP" >/dev/null || die "GPT validation failed on $RP"
  else $SUDO sfdisk --verify "$RP" >/dev/null 2>&1 || die "MBR validation failed on $RP"; fi
  $SUDO lsblk -o NAME,FSTYPE,LABEL "$RP" | grep -q @STICK_LABEL@ || die "@STICK_LABEL@ label missing"
  log "verify: ISO slice (${RP}1) byte-exact vs cached ISO + casper present"
  # p1 starts at the ISO's LBA 64 (isohybrid GPT skips the first 32 KiB);
  # compare ISO[64*2048 .. +64 MiB] against p1[0 .. +64 MiB].
  $SUDO dd if="${RP}1" of=/tmp/nx-verify-p1.bin bs=1M count=64 status=none
  $SUDO dd if="$ISO" of=/tmp/nx-verify-iso.bin bs=512 count=131072 skip=64 status=none   # ISO LBA 64 onward, 64 MiB
  $SUDO cmp /tmp/nx-verify-p1.bin /tmp/nx-verify-iso.bin \
    || die "stick ISO slice differs from the cached ISO"
  $SUDO rm -f /tmp/nx-verify-p1.bin /tmp/nx-verify-iso.bin
  # a previous failed verify may have left these mounted
  for m in /mnt/nx-iso /mnt/nx-esp /mnt/nx-usb; do $SUDO umount "$m" 2>/dev/null || true; done
  $SUDO mkdir -p /mnt/nx-iso
  $SUDO mount -o ro "${RP}1" /mnt/nx-iso || die "cannot mount ISO slice ${RP}1"
  $SUDO test -f /mnt/nx-iso/casper/vmlinuz || die "casper/vmlinuz missing"
  $SUDO test -f /mnt/nx-iso/casper/initrd || die "casper/initrd missing"
  # amd64 ISO: EFI/boot; arm64 ISO: efi/boot. What firmware actually loads is the ESP (p2).
  $SUDO test -d /mnt/nx-iso/EFI/boot || $SUDO test -d /mnt/nx-iso/efi/boot || die "EFI boot dir missing"
  $SUDO umount /mnt/nx-iso
  log "verify: ESP (${RP}2) carries a UEFI boot loader"
  $SUDO mkdir -p /mnt/nx-esp
  $SUDO mount -o ro "${RP}2" /mnt/nx-esp || die "cannot mount ESP ${RP}2"
  $SUDO sh -c 'ls /mnt/nx-esp/EFI/boot/boot*.efi /mnt/nx-esp/efi/boot/boot*.efi 2>/dev/null | grep -q .' \
    || die "no boot*.efi on the ESP"
  $SUDO umount /mnt/nx-esp
  log "verify: payload sha256 parity vs this head node's sources"
  $SUDO mkdir -p /mnt/nx-usb
  $SUDO mount "${RP}3" /mnt/nx-usb || die "cannot mount ${RP}3"
  trap '$SUDO umount /mnt/nx-usb 2>/dev/null || true' EXIT
  local expected
  expected=$($SUDO sh -c "{ for f in @REPO_KEYS@/*.key; do sha256sum \"\$f\" | sed 's|  .*/|  keys/|'; done;
      for f in @REPO_CREDS@/*.pass; do sha256sum \"\$f\" | sed 's|  .*/|  creds/|'; done;
      sha256sum \"$HERE/noctyx-restore.sh\" | sed 's|  .*/noctyx-restore.sh$|  noctyx-restore.sh|';
      sha256sum @TLS@/ca.crt | sed 's|  .*|  restore-ca.crt|'; }")
  ( cd /mnt/nx-usb && $SUDO sha256sum -c <<<"$expected" ) || die "payload parity FAILED"
  # Exactly what a restore does: the endpoint from the site config, the stick's CA,
  # basic-auth cred and repo key.
  local VERIFY REST_HOST
  VERIFY=$(python3 - "${NOCTYX_CONFIG:?}" "@REPO_KEYS@" <<'PY'
import json, os, sys
c = json.load(open(sys.argv[1]))
print(next((h["repo"] for h in c["hosts"] if h.get("repo") and os.path.exists(f"{sys.argv[2]}/{h['repo']}.key")), ""))
PY
)
  [[ -n $VERIFY ]] || die "no host in the site config has a repository key in @REPO_KEYS@"
  REST_HOST=$(python3 -c "import json,sys;s=json.load(open(sys.argv[1]))['server'];print(f\"{s['address']}:{s['rest_port']}\")" "$NOCTYX_CONFIG")
  log "verify: stick key + creds + CA open the live $VERIFY repository over the rest endpoint"
  $SUDO sh -c "umask 077; printf 'rest:https://@REPO_DIR_PREFIX@$VERIFY:%s@$REST_HOST/@REPO_DIR_PREFIX@$VERIFY/$VERIFY' \"\$(cat /mnt/nx-usb/creds/@REPO_DIR_PREFIX@$VERIFY.pass)\" > /tmp/nx-verify-loc; \
    restic --cacert /mnt/nx-usb/restore-ca.crt --password-file /mnt/nx-usb/keys/$VERIFY.key \
      --repository-file /tmp/nx-verify-loc --no-lock --no-cache cat config >/dev/null; rc=\$?; rm -f /tmp/nx-verify-loc; exit \$rc" \
    || die "stick $VERIFY key/creds failed to open the live repository at $REST_HOST"
  log "verify: stick engine == directory-served engine"
  # sudo kills process substitution (/dev/fd race); use temp files.
  $SUDO cat /mnt/nx-usb/noctyx-restore.sh > /tmp/nx-eng-stick.sh
  curl -sk https://@HEAD_ADDR@:20999/api/v1/restore-script > /tmp/nx-eng-served.sh
  diff /tmp/nx-eng-stick.sh /tmp/nx-eng-served.sh >/dev/null \
    || die "stick engine differs from server-served engine"
  rm -f /tmp/nx-eng-stick.sh /tmp/nx-eng-served.sh
  $SUDO umount /mnt/nx-usb; trap - EXIT
  log "ALL CHECKS PASSED — self-booting Noctyx recovery drive ($ARCH)"
  echo
  echo "Boot: pick the stick in the firmware menu. Default grub entry = Ubuntu live."
  echo "Then follow README.TXT (ESP) or README.txt (@STICK_LABEL@) — five commands to restore."
}

# ── main ────────────────────────────────────────────────────────────────
stage_iso
[[ -n $FETCH_ONLY ]] && { log "fetch-only: $ISO ready"; exit 0; }
if [[ -n $FINISH ]]; then
  # resume path: ISO already dd'd + GPT rewritten + (operator) p3 formatted.
  RP=$(realpath "$FINISH")
  [[ -b ${RP}3 ]] || die "${RP}3 not present — dd + GPT step not done yet"
  $SUDO blkid "${RP}3" | grep -qi @STICK_LABEL@ || die "${RP}3 is not formatted @STICK_LABEL@ yet"
  log "deploying LIVE payload via make-restore-usb.sh"
  $SUDO env NOCTYX_CONFIG="$NOCTYX_CONFIG" bash "$HERE/make-restore-usb.sh" --usb "${RP}3"
  $SUDO mkdir -p /mnt/nx-esp
  if $SUDO mount "${RP}2" /mnt/nx-esp; then
    $SUDO tee /mnt/nx-esp/README.TXT >/dev/null <<'EOF'
@STICK_LABEL@ RECOVERY
1. Boot this stick. Grub: "Try or Install Ubuntu Server" -> at the first
   installer screen choose "Try Ubuntu" (live session) - NOT install.
2. lsblk -f | grep @STICK_LABEL@   -> sudo mount <that-partition> /mnt
3. sudo bash /mnt/noctyx-restore.sh --server @HEAD_ADDR@ --host NAME
   (it finds the keys and logins in /mnt/keys and /mnt/creds by itself)
   The head node itself: sudo bash /mnt/noctyx-restore-head.sh --help
EOF
    $SUDO umount /mnt/nx-esp
  fi
  USB=$RP
  verify_stick
  exit 0
fi
if [[ -n $DRY ]]; then
  echo "DRY: dd $ISO → \$USB (raw, isohybrid); stick_gpt rewrite; @STICK_LABEL@+payload+verify (needs --usb)"
  exit 0
fi
[[ -n $USB ]] || die "need --usb /dev/sdX (or --dry-run / --fetch-only)"
write_stick
verify_stick
