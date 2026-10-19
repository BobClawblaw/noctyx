#!/usr/bin/env bash
# Noctyx HEAD-NODE restore (GPT + ESP + /boot + LVM root) — the layout
# noctyx-restore.sh refuses. Runs from any rescue Linux (recovery stick, live ISO,
# rescue VM) that can reach a repository endpoint — the one on the NAS works
# with the head node dead.
#
#   noctyx-restore-head.sh --disk /dev/nvme0n1 --repo-file F --key F --ca F \
#                      [--host NAME] [--snapshot latest|ID] [--yes] [--rehearsal]
#
# Faithful by design: the partition table, LVM PV/VG/LV and every filesystem are
# recreated with their ORIGINAL UUIDs from the snapshot's own boot metadata
# (fsmeta), so fstab, GRUB and the dm-uuid root= path work unmodified.
# --rehearsal: add a serial console + skip the EFI NVRAM write (VM boot test).
#
# NOT restored here, by the backup profile's design (exclusions-common.conf):
# /storage, /mnt/*, /var/lib/docker, /snap, apt lists. Those come from their own
# disks or their own backups — see the Restore guide (docs/guide/restore.md).
set -Eeuo pipefail
trap 'echo "CM-RESTORE-HEAD: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR

DISK="" REPOF="" KEY="" CA="" HOST=@HEAD_HOST@ SNAP=latest YES=no REHEARSAL=no
MNT=/mnt/nx-target WORK=/tmp/nx-restore-head
log() { printf '\n[noctyx-restore-head %s] %s\n' "$(date -u +%T)" "$*"; }
die() { echo "CM-RESTORE-HEAD: FATAL: $*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do case $1 in
  --disk) DISK=$2; shift 2;; --repo-file) REPOF=$2; shift 2;; --key) KEY=$2; shift 2;;
  --ca) CA=$2; shift 2;; --host) HOST=$2; shift 2;; --snapshot) SNAP=$2; shift 2;;
  --yes) YES=yes; shift;; --rehearsal) REHEARSAL=yes; shift;;
  -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) die "unknown argument $1";; esac; done
[[ -b $DISK && -r $REPOF && -r $KEY && -r $CA ]] || die "need --disk <block dev> --repo-file --key --ca"
for t in restic sfdisk pvcreate vgcfgrestore vgchange mkfs.ext4 mkfs.vfat python3 chroot blkid; do
  command -v $t >/dev/null || die "missing tool: $t"; done
T0=$(date +%s)
RESTIC=(restic --cacert "$CA" --password-file "$KEY" --repository-file "$REPOF" --no-cache)
mkdir -p "$WORK"

# ── 1. snapshot + its boot metadata ───────────────────────────────────
log "resolving snapshot '$SNAP' for host $HOST"
"${RESTIC[@]}" snapshots --host "$HOST" --tag profile-system --json > "$WORK/snaps.json"
read -r SNAPID SNAPTIME META_SRC < <(python3 - "$WORK/snaps.json" "$SNAP" <<'EOF'
import json, sys
s = sorted(json.load(open(sys.argv[1])), key=lambda x: x["time"])
want = sys.argv[2]
s = [x for x in s if want in ("latest", x["id"], x["short_id"], (x.get("original") or "")[:8])]
if not s: sys.exit("no matching system snapshot")
x = s[-1]
meta = next(p for p in x["paths"] if p.startswith("@NODE_LIB@/fsmeta/"))
print(x["short_id"], x["time"][:19], meta)
EOF
)
log "snapshot $SNAPID ($SNAPTIME), metadata $META_SRC"
"${RESTIC[@]}" restore "$SNAPID" --include "$META_SRC" --target "$WORK/meta" >/dev/null
M=$WORK/meta$META_SRC
[[ -f $M/lsblk.txt && -d $M/partitions ]] || die "snapshot has no usable fsmeta"

# ── 2. which captured disk was the root disk ──────────────────────────
ESP_SRC=$(sed -n 's/.*PATH="\([^"]*\)".*MOUNTPOINT="\/boot\/efi".*/\1/p' "$M/lsblk.txt" | head -1)
SF=$(grep -l "^$ESP_SRC " "$M"/partitions/*.sfdisk | head -1)
SRCDISK=$(sed -n 's/^device: //p' "$SF")
LASTLBA=$(sed -n 's/^last-lba: //p' "$SF")
need=$(( (LASTLBA + 34) * 512 )); have=$(blockdev --getsize64 "$DISK")
log "source root disk $SRCDISK ($(basename "$SF")) -> target $DISK"
(( have >= need )) || die "target $DISK is $have bytes; the source layout needs $need"
pdev() { [[ $1 =~ [0-9]$ ]] && echo "${1}p$2" || echo "$1$2"; }   # nvme0n1 -> p1, vdb -> 1

if [[ $YES != yes ]]; then
  lsblk "$DISK"; read -r -p "ERASE $DISK and rebuild it as $SRCDISK from $SNAPID? type ERASE: " a
  [[ $a == ERASE ]] || die "aborted"
fi

# ── 3. partition table (original PARTUUIDs, label-id, geometry) ───────
log "replaying partition table"
wipefs -a "$DISK" >/dev/null
sed -e '/^device:/d' -e "s#^${SRCDISK}p\?\([0-9]\+\) :#$(pdev "$DISK" X | sed 's/X$//')\1 :#" "$SF" \
  | sfdisk --no-reread --wipe always --wipe-partitions always "$DISK" >/dev/null
partprobe "$DISK" 2>/dev/null || true; udevadm settle
N=$(grep -c "^$(pdev "$DISK" X | sed 's/X$//')" <(sfdisk -d "$DISK"))

# ── 4. filesystems / LVM with their original UUIDs (from blkid.txt) ──
bl() { grep "^$1:" "$M/blkid.txt" | head -1; }
field() { sed -n "s/.* $1=\"\([^\"]*\)\".*/\1/p"; }
mkfs_like() {   # <captured device path> <target device>
  local line type uuid label; line=$(bl "$1")
  type=$(field TYPE <<<" $line"); uuid=$(field UUID <<<" $line"); label=$(field LABEL <<<" $line")
  case $type in
    vfat) mkfs.vfat -F 32 -i "${uuid//-/}" ${label:+-n "$label"} "$2" >/dev/null;;
    ext4) mkfs.ext4 -F -q -U "$uuid" ${label:+-L "$label"} -E lazy_itable_init=1,lazy_journal_init=1 "$2";;
    swap) mkswap -U "$uuid" "$2" >/dev/null;;
    LVM2_member)
      local vgf; vgf=$(grep -l "id = \"$uuid\"" "$M"/lvm-backup/backup/* | head -1)
      [[ -n $vgf ]] || die "no LVM metadata backup contains PV $uuid"
      local vg; vg=$(basename "$vgf")
      pvcreate -ff -y --uuid "$uuid" --restorefile "$vgf" "$2" >/dev/null
      vgcfgrestore -f "$vgf" "$vg" >/dev/null
      vgchange -ay "$vg" >/dev/null; udevadm settle
      for lvp in $(sed -n 's/.*PATH="\(\/dev\/mapper\/[^"]*\)".*TYPE="lvm".*/\1/p' "$M/lsblk.txt"); do
        [[ -b $lvp ]] && { log "  LV $lvp"; mkfs_like "$lvp" "$lvp"; }
      done;;
    "") log "  $1: no filesystem captured — left empty";;
    *) die "don't know how to recreate $type on $1";;
  esac
}
for n in $(seq 1 "$N"); do
  log "partition $n: recreating $(bl "$(pdev "$SRCDISK" "$n")" | field TYPE)"
  mkfs_like "$(pdev "$SRCDISK" "$n")" "$(pdev "$DISK" "$n")"
done

# ── 5. mount the rebuilt tree exactly as it was mounted ──────────────
log "mounting"
declare -A DEVMAP
for n in $(seq 1 "$N"); do DEVMAP[$(pdev "$SRCDISK" "$n")]=$(pdev "$DISK" "$n"); done
while read -r p; do DEVMAP[$p]=$p; done < <(sed -n 's/.*PATH="\(\/dev\/mapper\/[^"]*\)".*TYPE="lvm".*/\1/p' "$M/lsblk.txt")
mkdir -p "$MNT"
while read -r mp dev; do
  mkdir -p "$MNT$mp"; mount "$dev" "$MNT$mp"; log "  $dev -> $MNT$mp"
done < <(for src in "${!DEVMAP[@]}"; do
           mp=$(sed -n "s#.*PATH=\"$src\".*MOUNTPOINT=\"\([^\"]*\)\".*#\1#p" "$M/lsblk.txt" | head -1)
           [[ -n $mp ]] && echo "$mp ${DEVMAP[$src]}"
         done | sort -k1,1)   # "/" < "/boot" < "/boot/efi"

# ── 6. the data ───────────────────────────────────────────────────────
log "restic restore $SNAPID -> $MNT (the long part)"
( while sleep 120; do log "  restored so far: $(df -h --output=used "$MNT" | tail -1 | tr -d ' ')"; done ) &
HB=$!
"${RESTIC[@]}" restore "$SNAPID" --target "$MNT" 2>&1 | tail -5
kill $HB 2>/dev/null || true

# ── 7. what the profile excludes but a booting system needs ──────────
log "recreating excluded skeleton"
install -d -m 0555 "$MNT"/proc "$MNT"/sys
install -d -m 0755 "$MNT"/dev "$MNT"/run "$MNT"/mnt "$MNT"/media "$MNT"/srv "$MNT"/storage "$MNT"/var/lib/docker
install -d -m 1777 "$MNT"/tmp "$MNT"/var/tmp
mkdir -p "$MNT"/var/log/journal   # systemd-tmpfiles sets owner/mode at first boot
install -d "$MNT"/var/lib/apt/lists/partial "$MNT"/var/cache/apt/archives/partial
for mp in $(awk '!/^#/ && $2 ~ /^\/mnt\// {print $2}' "$MNT/etc/fstab"); do mkdir -p "$MNT$mp"; done
if grep -qE '^/swap\.img\s' "$MNT/etc/fstab" && [[ ! -f $MNT/swap.img ]]; then
  fallocate -l 8G "$MNT/swap.img" && chmod 600 "$MNT/swap.img" && mkswap "$MNT/swap.img" >/dev/null
fi

# ── 8. bootloader + initramfs, inside the restored system ────────────
log "chroot: initramfs + GRUB"
for d in dev dev/pts proc sys run; do mount --bind "/$d" "$MNT/$d"; done
[[ -d /sys/firmware/efi/efivars ]] && mount --bind /sys/firmware/efi/efivars "$MNT/sys/firmware/efi/efivars" 2>/dev/null || true
ARCH=$(sed -n 's/^# host=.* arch=\([^ ]*\).*/\1/p' "$M/system.txt")
TARGET=$([[ $ARCH == aarch64 ]] && echo arm64-efi || echo x86_64-efi)
if [[ $REHEARSAL == yes ]]; then
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 console=tty0 console=ttyS0,115200n8"/' "$MNT/etc/default/grub"
fi
NV=$([[ $REHEARSAL == yes ]] && echo --no-nvram || echo "")
chroot "$MNT" update-initramfs -u -k all 2>&1 | tail -2
chroot "$MNT" grub-install --target="$TARGET" --efi-directory=/boot/efi --bootloader-id=ubuntu $NV 2>&1 | tail -1
chroot "$MNT" grub-install --target="$TARGET" --efi-directory=/boot/efi --removable $NV 2>&1 | tail -1
chroot "$MNT" update-grub 2>&1 | tail -1

# ── 9. done ───────────────────────────────────────────────────────────
sync
umount -R "$MNT"
vgchange -an >/dev/null 2>&1 || true
log "restored $SNAPID ($SNAPTIME) in $(( ($(date +%s) - T0) / 60 )) min"
echo "CM-RESTORE-HEAD: DONE snapshot=$SNAPID time=$SNAPTIME minutes=$(( ($(date +%s) - T0) / 60 ))"
