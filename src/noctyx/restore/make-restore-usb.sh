#!/usr/bin/env bash
# Build a Noctyx restore USB.
#
# The strategy is NOT to bake a custom ISO (that's a phase-4 build project).
# Instead: any Ubuntu arm64 live/installer USB already has network + a shell;
# this script drops a payload onto it (keys + creds + this-box's restore script
# + a bootstrap) so the rescue box needs exactly one curl|bash at 3 a.m.
#
#   make-restore-usb.sh --usb /dev/sdX1        # partition must be FAT (label @STICK_LABEL@)
#   make-restore-usb.sh --dry-run              # show what would be copied
#
# After this: boot target -> "Try Ubuntu" -> ensure network ->
#   curl -sk https://<head>:20999/api/v1/restore-script -o /tmp/noctyx-restore.sh
#   sudo bash /tmp/noctyx-restore.sh --host NAME
# (or with no network: run the copy on the stick)
# Run through `noctyx tool make-restore-usb`, which fills in this site's paths.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRV=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["address"])' "${NOCTYX_CONFIG:?run through: noctyx tool make-restore-usb}")
MNT=/mnt/nx-usb
DRY=""
if [[ "${1:-}" == "--dry-run" ]]; then DRY=1; fi
USB=""
while [[ $# -gt 0 ]]; do case $1 in
  --usb) USB=$2; shift 2;; --dry-run) DRY=1; shift;;
  *) echo "usage: $0 --usb /dev/sdX1 | --dry-run" >&2; exit 1;;
esac; done
[[ -n $USB ]] || { echo "usage: $0 --usb /dev/sdX1" >&2; exit 1; }
[[ -b $USB ]] || { echo "$USB is not a block device" >&2; exit 1; }

run(){ if [[ -n $DRY ]]; then echo "DRY: $*"; else "$@"; fi; }
[[ -n $DRY ]] || {
  mkdir -p $MNT; mount -o umask=000 "$USB" $MNT
  trap 'umount $MNT 2>/dev/null || true' EXIT
  FS=$(blkid -o value -s TYPE "$USB")
  [[ $FS == vfat || $FS == fat32 || $FS == fat16 ]] || { echo "$USB is $FS, not FAT — EFI firmware + the Ubuntu installer live env expects FAT labels; refusing" >&2; exit 1; }
}

put(){ [[ -n $DRY ]] && { echo "DRY: install -m $1 src $MNT/$2"; return; }
       install -m "$1" "$2" "$MNT/$3"; }
echo "== building restore payload on ${USB:-dry-run} =="
run mkdir -p $MNT/keys $MNT/creds
for k in @REPO_KEYS@/*.key;      do put 600 "$k" keys keys/$(basename "$k"); done
for p in @REPO_CREDS@/*.pass;    do put 600 "$p" creds creds/$(basename "$p"); done
run install -m 700 "$HERE/noctyx-restore.sh" $MNT/noctyx-restore.sh
run install -m 700 "$HERE/noctyx-menu.sh" $MNT/noctyx-menu.sh
# the stick's fallback server when UDP discovery gets no answer
[[ -n $DRY ]] || printf 'DEFAULT_SERVER=%s
' "$SRV" > $MNT/stick.conf
# restic is not in the Ubuntu live image: carry the pinned binary for both arches,
# checksum-verified against the release's SHA256SUMS (the site config's restic_version)
RV=@RESTIC_VERSION@
RC=@ISO_DIR@/restic-$RV; mkdir -p "$RC"
for a in arm64 amd64; do
  if [[ ! -x $RC/restic-$a ]]; then
    f=restic_${RV}_linux_${a}.bz2; base=https://github.com/restic/restic/releases/download/v$RV
    curl -fsSL -o "$RC/SHA256SUMS" "$base/SHA256SUMS"
    curl -fsSL -o "$RC/$f" "$base/$f"
    ( cd "$RC" && grep "[A-Za-z0-9._-]*linux_${a}\.bz2\$" SHA256SUMS | sha256sum -c - >/dev/null ) \
      || { echo "restic $a CHECKSUM FAILED — refusing" >&2; exit 1; }
    bzip2 -dc "$RC/$f" > "$RC/restic-$a"; chmod 755 "$RC/restic-$a"
  fi
done
run mkdir -p $MNT/bin
for a in arm64 amd64; do put 755 "$RC/restic-$a" bin bin/restic-$a; done
run install -m 600 @TLS@/ca.crt $MNT/restore-ca.crt
[[ -n $DRY ]] && { echo "(dry-run — nothing written)"; exit 0; }
echo "payload present:"; ls -la $MNT
echo
echo "USB ready. Boot the sick box from the UBUNTU INSTALLER stick, pick 'Try Ubuntu',"
echo "then (mount this stick too if auto-mount didn't): "
echo "  sudo mkdir -p /run/nx-keys /run/nx-creds /run/nx-usb"
echo "  sudo mount /dev/sdXN /run/nx-usb    # the @STICK_LABEL@ partition: lsblk -f | grep @STICK_LABEL@ (p3 on a self-booting stick)"
echo "  sudo cp /run/nx-usb/keys/* /run/nx-keys/ ; sudo cp /run/nx-usb/creds/* /run/nx-creds/"
echo "  sudo bash /run/nx-usb/noctyx-restore.sh --server $SRV --host NAME"
echo "Keys on a stick = acceptable blast radius (repo keys decrypt backups that the"
echo "same stick + server can re-copy); keep the stick IN the drawer when not restoring."
