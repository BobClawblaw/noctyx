#!/usr/bin/env bash
# build-stick.sh — ONE stick that boots on x86_64 AND arm64.
#
# Layout:
#   p1 @LIVE_LABEL@  ext4: ubuntu-amd64.iso (full ISO; grub loop-mounts it,
#                     loopback.cfg = standard Ubuntu live x86 entries)
#                     + ubuntu-arm64.iso + /grub/{vmlinuz,initrd} (that ISO's own
#                     casper kernel+initrd; the arm64 entry boots them with
#                     iso-scan/filename=/ubuntu-arm64.iso — VM-verified 2026-09-29)
#   p2 ESP          = bootx64.efi (shim) + bootaa64.efi (shim);
#                     boot/grub/x86_64-efi/grub.cfg -> loopback x86 entries
#                     boot/grub/arm64-efi/grub.cfg  -> arm64 kernel entry
#                     boot/grub/grub.cfg            -> fallback both-arch menu
#   p3 @STICK_LABEL@   = keys/creds/engine payload (arch-neutral, via
#                     make-restore-usb.sh)
#
# Usage (the normal path is two commands):
#   sudo build-stick.sh - --stage            # p1.img + esp.img under @ISO_DIR@/stage-@STICK_LABEL@ (~9 GB, once)
#   sudo build-stick.sh /dev/sdX --build     # gated: dd p1, GPT, ESP, mkfs p3, payload, full verify
# Then prove it boots without a machine:  noctyx tool vm-boot-test --arch arm64 --media /dev/sdX
# (and --arch amd64). Both entries are VM-verified.
# A file instead of a stick (tests): losetup -fP --show stick.img, then NX_ALLOW_LOOP=1 … /dev/loopN --build
# Step-wise modes remain for repair: --boot (rewrite ESP), --payload (redeploy payload + verify).
# Run through `noctyx tool build-stick`, which fills in this site's paths.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEV=${1:-}; MODE=${2:-}
[[ $MODE == --stage || -b "$DEV" ]] || { echo "usage: $0 /dev/sdX --build | - --stage | /dev/sdX --boot|--payload" >&2; exit 2; }

ISO_X=@ISO_DIR@/noble-live-server-amd64.iso
ISO_A=@ISO_DIR@/noble-live-server-arm64.iso
LABEL_LIVE=@LIVE_LABEL@
STAGE=@ISO_DIR@/stage-@STICK_LABEL@
IMG=$STAGE/p1.img
ESPI=$STAGE/esp.img
ESP_MB=64
S=${SUDO:-sudo}
# partition names of $1 in order. `lsblk -nro NAME /dev/sdX` lists the DISK
# first, so `sed -n 1p` was sdX itself: --payload deployed onto the ESP and
# checked the whole disk's label (2026-10-11, first build on real media).
parts() { lsblk -nro NAME,TYPE "$1" | awk '$2=="part"{print $1}' | paste -sd' '; }
# partition N of a device: sdX -> sdX2, but nvme0n1 -> nvme0n1p2 and loop0 -> loop0p2
pn() { case $1 in *[0-9]) echo "${1}p$2";; *) echo "$1$2";; esac; }
log(){ echo "[noctyx-stick] $*"; }
die(){ echo "[noctyx-stick FATAL] $*" >&2; exit 1; }

[[ -f $ISO_X && -f $ISO_A ]] || die "missing cached ISOs under @ISO_DIR@"
SX=$(stat -c%s $ISO_X); SA=$(stat -c%s $ISO_A)
p1_sectors() { [[ -f $IMG ]] || die "$IMG missing — run --stage first"; echo $(( $(stat -c%s $IMG) / 512 )); }

case $MODE in
# ── STAGE: build p1.img (ext4: ISO + arm kernel) and esp.img ─────────────
--stage)
  mkdir -p $STAGE
  log "creating p1 image (amd64 ISO $(numfmt --to=iec $SX) + arm64 ISO $(numfmt --to=iec $SA) + slack)"
  rm -f $IMG
  truncate -s $(( ((SX + SA + 400*1024*1024 + 1048575)/1048576)*1048576 )) $IMG
  LOOP=$($S losetup -f --show $IMG)
  $S mkfs.ext4 -q -F -L $LABEL_LIVE $LOOP
  $S mkdir -p $STAGE/mnt; $S mount $LOOP $STAGE/mnt
  $S cp $ISO_X $STAGE/mnt/ubuntu-amd64.iso
  $S cp $ISO_A $STAGE/mnt/ubuntu-arm64.iso
  # arm64 boots the ISO's OWN casper kernel+initrd; iso-scan loop-mounts the ISO from p1.
  # (The noble netboot tarball's initrd wants an NFS root and hangs "looking for a medium"
  #  when booted from a stick.)
  $S mkdir -p $STAGE/mnt/grub; T=$STAGE/isomnt; $S mkdir -p $T
  $S mount -o ro,loop $ISO_A $T
  $S cp $T/casper/vmlinuz $STAGE/mnt/grub/vmlinuz; $S cp $T/casper/initrd $STAGE/mnt/grub/initrd
  $S umount $T
  $S umount $STAGE/mnt; $S losetup -d $LOOP
  log "p1.img: $(numfmt --to=iec $(stat -c%s $IMG))"

  log "building dual-arch ESP image"
  EX=$STAGE/bootstage; rm -rf $EX; mkdir -p $EX
  T=$STAGE/isomnt; $S mkdir -p $T
  $S mount -o ro,loop $ISO_X $T
  $S cp -a $T/EFI $T/boot $EX/
  $S umount $T
  $S mount -o ro,loop $ISO_A $T
  AF=$($S find $T -name bootaa64.efi | head -1); [[ -n $AF ]] || die "no bootaa64.efi in arm64 ISO"
  GR=$($S find $T -name grubaa64.efi | head -1)
  MODS=$($S find $T -type d -name arm64-efi | head -1)
  $S cp $AF $EX/EFI/boot/bootaa64.efi
  [[ -n $GR ]] && $S cp $GR $EX/EFI/boot/grubaa64.efi
  $S mkdir -p $EX/boot/grub/arm64-efi
  $S cp $MODS/*.mod $MODS/*.lst $EX/boot/grub/arm64-efi/ 2>/dev/null || true
  $S umount $T

  cat > $EX/boot/grub/x86_64-efi/grub.cfg <<'XCFG'
set timeout=5
search --no-floppy --set=part --label @LIVE_LABEL@
# Ubuntu's loopback.cfg expects $iso_path and root=(loop) and has no timeout of its own
# (VM test 2026-09-29 sat at its menu forever): use our own entry instead.
set iso_path=/ubuntu-amd64.iso
loopback cmiso ($part)$iso_path
menuentry 'Noctyx Recovery - Ubuntu Live (x86_64)' {
  set gfxpayload=keep
  linux (cmiso)/casper/vmlinuz iso-scan/filename=$iso_path console=tty0 console=ttyS0,115200n8 ---
  initrd (cmiso)/casper/initrd
}
XCFG
  cat > $EX/boot/grub/arm64-efi/grub.cfg <<'ACFG'
set timeout=5
search --no-floppy --set=part --label @LIVE_LABEL@
# Offline arm64 live session: the arm64 ISO's casper kernel/initrd, ISO loop-mounted
# by iso-scan from p1. Boots to the Subiquity live shell in an aarch64 UEFI VM.
menuentry 'Noctyx Recovery - Ubuntu Live (arm64)' {
  linux ($part)/grub/vmlinuz iso-scan/filename=/ubuntu-arm64.iso ---
  initrd ($part)/grub/initrd
}
ACFG
  cat > $EX/boot/grub/grub.cfg <<'BOTH'
set timeout=5
menuentry 'Ubuntu Live (x86_64)' {
    search --no-floppy --set=part --label @LIVE_LABEL@
    set iso_path=/ubuntu-amd64.iso
    loopback cmiso ($part)$iso_path
    set gfxpayload=keep
    linux (cmiso)/casper/vmlinuz iso-scan/filename=$iso_path console=tty0 console=ttyS0,115200n8 ---
    initrd (cmiso)/casper/initrd
}
menuentry 'Ubuntu Live (arm64)' {
    search --no-floppy --set=part --label @LIVE_LABEL@
    linux ($part)/grub/vmlinuz iso-scan/filename=/ubuntu-arm64.iso ---
    initrd ($part)/grub/initrd
}
BOTH
  printf 'Noctyx dual-arch stick: x86_64 loop-boots ubuntu-amd64.iso from p1; arm64 boots ubuntu-arm64.iso from p1 via iso-scan.\r\n' > $EX/README.TXT

  rm -f $ESPI; truncate -s $((ESP_MB*1024*1024)) $ESPI
  # stage FAT image world-readable so mtools (no sudo) can post-verify
  $S chmod 0666 $ESPI
  mformat -i $ESPI -h 64 -s 32 -t $((ESP_MB*1024*1024/512/2048)) -c 8 -r 32 -S 2 -v ESP ::
  mcopy -s -n -i $ESPI $EX/EFI $EX/boot $EX/README.TXT ::
  mdir -i $ESPI /EFI/boot | grep -qi bootaa64 || die "esp.img missing bootaa64"
  log "esp.img built ($(numfmt --to=iec $(stat -c%s $ESPI)))"

  PS=$(p1_sectors)
  ESP_START=$(( (2048 + PS + 2047)/2048*2048 ))
  echo
  echo "  staged. Next:  sudo $0 /dev/sdX --build      (p1 needs $PS sectors; ESP would start at $ESP_START)"
  ;;

# ── BUILD: everything, gated on the typed device name ───────────────────
--build)
  [[ -f $IMG && -f $ESPI ]] || die "stage artifacts missing — run: $0 - --stage"
  RP=$(realpath "$DEV")
  [[ $RP == /dev/sd* || $RP == /dev/nvme* || ( $RP == /dev/loop* && ${NX_ALLOW_LOOP:-} == 1 ) ]] || die "unexpected device $RP"
  bytes=$(blockdev --getsize64 "$RP"); need=$(( $(stat -c%s $IMG) + ESP_MB*1024*1024 + 512*1024*1024 ))
  (( bytes > need )) || die "$RP ($((bytes/1000/1000)) MB) too small: need > $((need/1000/1000)) MB"
  lsblk -nrpo MOUNTPOINT "$RP" | grep -q . && die "a partition of $RP is mounted — unmount first"
  echo; echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  DESTRUCTIVE: $RP  ($((bytes/1000/1000)) MB  $(lsblk -dnro MODEL "$RP" | tr -s ' '))"
  echo "║  EVERY partition on it will be erased. Type the device name:  ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  read -rp "> " ans || die "no confirmation"
  [[ "$ans" == "$(basename "$RP")" || "$ans" == "$RP" ]] || die "confirmation '$ans' did not match — aborted"
  PS=$(p1_sectors)
  log "wiping old signatures; dd p1.img → $RP @1 MiB ($(numfmt --to=iec $(stat -c%s $IMG)))"
  $S wipefs -aq "$RP" 2>/dev/null || true
  $S dd if=$IMG of="$RP" bs=1M seek=1 conv=fsync status=progress
  log "GPT: p1 @LIVE_LABEL@ / p2 ESP / p3 @STICK_LABEL@ (dual_gpt.py)"
  $S python3 "$HERE/dual_gpt.py" "$RP" "$PS" "$ESP_MB"
  # let the kernel see the new table (usually within a second or two)
  seen=""; for i in $(seq 1 15); do $S partprobe "$RP" 2>/dev/null || $S blockdev --rereadpt "$RP" 2>/dev/null || true
    lsblk -nro NAME "$RP" | grep -qx "$(basename "$(pn "$RP" 4)")" && { seen=1; break; }; sleep 1; done
  [[ -n $seen ]] || die "kernel does not see $(pn "$RP" 3) — replug the stick, then: $0 $RP --boot ; mkfs.vfat -F 32 -n @STICK_LABEL@ $(pn "$RP" 3) ; $0 $RP --payload"
  $S sgdisk -v "$RP" | grep -qi "no problems found" || die "GPT invalid after write"
  log "writing ESP image → $(pn "$RP" 2)"
  $S dd if=$ESPI of="$(pn "$RP" 2)" bs=4M status=none conv=fsync
  log "mkfs.vfat @STICK_LABEL@ → $(pn "$RP" 3)"
  $S mkfs.vfat -F 32 -n @STICK_LABEL@ "$(pn "$RP" 3)" >/dev/null
  log "mkfs.vfat CIDATA → $(pn "$RP" 4)"
  $S mkfs.vfat -n CIDATA "$(pn "$RP" 4)" >/dev/null
  $S partprobe "$RP" 2>/dev/null || true; sleep 1
  exec "$0" "$RP" --payload
  ;;

# ── BOOT: p1/p2/p3 exist (GPT from dual_gpt.py); write ESP partition ──
--boot)
  [[ -f $ESPI ]] || die "esp.img missing — run --stage first"
  read -r P1 P2 P3 < <(parts "$DEV")
  [[ /dev/$P3 == "$(pn "$(realpath "$DEV")" 3)" ]] || die "device has no p3 — run the GPT step first"
  $S sgdisk -v "$DEV" >/dev/null || die "GPT invalid"
  log "writing ESP image to /dev/$P2"
  $S dd if=$ESPI of=/dev/$P2 bs=4M status=none conv=fsync
  sync
  log "ESP written"
  echo
  echo "  ── Operator step — run exactly: ──"
  echo "  sudo mkfs.vfat -F 32 -n @STICK_LABEL@ /dev/$P3"
  echo "  then:  sudo $0 $DEV --payload"
  ;;

# ── PAYLOAD: deploy @STICK_LABEL@ payload + full verification ───────────────
--payload)
  read -r P1 P2 P3 P4 < <(parts "$DEV")
  blkid -o value -s TYPE /dev/$P3 2>/dev/null | grep -q fat || die "/dev/$P3 not FAT yet — operator mkfs.vfat step missing"
  log "deploying payload to /dev/$P3"
  $S mkdir -p /mnt/nx-usb; $S umount /mnt/nx-usb 2>/dev/null || true
  $S env NOCTYX_CONFIG="$NOCTYX_CONFIG" bash "$HERE/make-restore-usb.sh" --usb /dev/$P3 >/tmp/dual-payload.log 2>&1 \
    || { tail -5 /tmp/dual-payload.log; die "payload deploy failed"; }
  # boot hook: cloud-init (NoCloud, label CIDATA) starts noctyx-menu.sh on tty1
  [[ -n ${P4:-} ]] || die "no p4 (CIDATA) — rebuild with --build (older 3-partition layout)"
  log "deploying boot hook (cloud-init seed) to /dev/$P4"
  $S mkdir -p /mnt/nx-ci; $S umount /mnt/nx-ci 2>/dev/null || true
  $S mount /dev/$P4 /mnt/nx-ci
  $S install -m 644 "$HERE/cidata/user-data" "$HERE/cidata/meta-data" /mnt/nx-ci/
  $S umount /mnt/nx-ci

  log "verify: GPT"
  $S sgdisk -v $DEV | grep -qi "no problems found" || die "GPT invalid"
  log "verify: p1 ext4 + ISO copy + arm kernel"
  $S mkdir -p /mnt/cdrom; $S umount /mnt/cdrom 2>/dev/null || true
  [[ $($S blkid -o value -s LABEL /dev/$P1) == $LABEL_LIVE ]] || die "p1 label wrong"
  $S mount -o ro /dev/$P1 /mnt/cdrom
  $S cmp -n 67108864 /mnt/cdrom/ubuntu-amd64.iso $ISO_X || die "p1 amd64 ISO copy differs (first 64 MiB)"
  $S cmp -n 67108864 /mnt/cdrom/ubuntu-arm64.iso $ISO_A || die "p1 arm64 ISO copy differs (first 64 MiB)"
  [[ $($S stat -c%s /mnt/cdrom/grub/vmlinuz) -gt 10000000 ]] || die "arm vmlinuz missing/small"
  [[ $($S stat -c%s /mnt/cdrom/grub/initrd) -gt 10000000 ]] || die "arm initrd missing/small"
  $S umount /mnt/cdrom
  log "verify: ESP has both arch bootloaders + cfgs"
  $S mkdir -p /mnt/nx-esp; $S umount /mnt/nx-esp 2>/dev/null || true
  $S mount -o ro /dev/$P2 /mnt/nx-esp
  for f in EFI/boot/bootx64.efi EFI/boot/bootaa64.efi boot/grub/x86_64-efi/grub.cfg boot/grub/arm64-efi/grub.cfg; do
    $S test -f /mnt/nx-esp/$f || die "ESP missing $f"
  done
  $S umount /mnt/nx-esp
# the repository the checks open: the first host whose key is on this head node
  VERIFY=$(python3 - "${NOCTYX_CONFIG:?}" "@REPO_KEYS@" <<'PY'
import json, os, sys
c = json.load(open(sys.argv[1]))
print(next((h["repo"] for h in c["hosts"] if h.get("repo") and os.path.exists(f"{sys.argv[2]}/{h['repo']}.key")), ""))
PY
)
  [[ -n $VERIFY ]] || die "no host in the site config has a repository key in @REPO_KEYS@"
  REST_HOST=$(python3 -c "import json,sys;s=json.load(open(sys.argv[1]))['server'];print(f\"{s['address']}:{s['rest_port']}\")" "$NOCTYX_CONFIG")
  log "verify: payload files + the stick's key opens the live $VERIFY repository"
  $S mount -o ro /dev/$P3 /mnt/nx-usb
  (cd /mnt/nx-usb && $S test -f "keys/$VERIFY.key" && $S test -f noctyx-restore.sh && $S test -f noctyx-menu.sh \
     && $S test -s bin/restic-arm64 && $S test -s bin/restic-amd64) || die "payload files missing"
  [[ $(/mnt/nx-usb/bin/restic-amd64 version | awk '{print $2}') == $(restic version | awk '{print $2}') ]] \
    || die "stick restic differs from the fleet's"
  $S mount -o ro /dev/$P4 /mnt/nx-ci && { $S test -f /mnt/nx-ci/user-data && $S test -f /mnt/nx-ci/meta-data; r=$?; $S umount /mnt/nx-ci; [[ $r == 0 ]]; } \
    || die "CIDATA seed missing"
  $S sh -c "umask 077; printf 'rest:https://@REPO_DIR_PREFIX@$VERIFY:%s@$REST_HOST/@REPO_DIR_PREFIX@$VERIFY/$VERIFY' \"\$(cat /mnt/nx-usb/creds/@REPO_DIR_PREFIX@$VERIFY.pass)\" > /tmp/nx-verify-loc; \
    restic --cacert /mnt/nx-usb/restore-ca.crt --password-file /mnt/nx-usb/keys/$VERIFY.key --repository-file /tmp/nx-verify-loc --no-lock --no-cache cat config >/dev/null; rc=\$?; rm -f /tmp/nx-verify-loc; exit \$rc" \
    || { $S umount /mnt/nx-usb; die "the stick's $VERIFY key + credential do not open the live repository"; }
  $S umount /mnt/nx-usb
  log "ALL CHECKS PASSED — dual-arch stick (bootx64 + bootaa64 on one ESP)"
  echo "Prove it boots: noctyx tool vm-boot-test --arch amd64 --media $DEV  (and --arch arm64)."
  ;;

*)
  echo "usage: $0 - --stage | /dev/sdX --build | /dev/sdX --boot | /dev/sdX --payload" >&2
  exit 2
  ;;
esac
