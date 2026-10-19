#!/bin/bash
# Noctyx restore engine — run from the bootable restore volume on a bare machine.
#
# STATUS: written, syntax-checked, and honest about what has NOT happened yet:
#         it has never restored a real machine. The exit test is a wiped
#         GB10 node rebuilt with this script. Everything below "UNVERIFIED" needs
#         that run before it can be trusted. Do not point it at a machine you
#         care about until then.
#
# Flow:
#   discover server → trust CA → get inventory → pick host → get key → pick
#   snapshot → rebuild partition table from the captured sfdisk dump → mkfs →
#   restic restore → fix fstab UUIDs → chroot: grub-install + initramfs +
#   efibootmgr → unmount → reboot.
#
# Usage:
#   noctyx-restore.sh --host node1 [--snapshot latest|ID] [--disk /dev/nvme0n1]
#                 [--key-file PATH] [--dry-run] [--yes] [--reboot]
set -uo pipefail

SERVER="${NX_SERVER:-}"            # empty → discovery
DIR_URL="${NX_DIR_URL:-}"
MNT=/mnt/restore
WORK=/tmp/nx-restore
DISK="" HOST="" SNAP="latest" KEY_FILE="${NX_KEY_FILE:-}" DRY="" ASSUME=no REBOOT=no LIST=""

# --list hosts|snapshots: machine-readable answers for the recovery menu
# (noctyx-menu.sh). Data on stdout, everything human on stderr, nothing written.
log(){ if [[ -n $LIST ]]; then printf '[noctyx-restore] %s\n' "$*" >&2
       else printf '\n\033[1m[noctyx-restore]\033[0m %s\n' "$*"; fi; }
die(){ printf '\n\033[31m[noctyx-restore FATAL]\033[0m %s\n' "$*" >&2; exit 1; }
run(){ if [[ -n $DRY ]]; then echo "DRY: $*"; else "$@"; fi; }

while [[ $# -gt 0 ]]; do case $1 in
  --host) HOST=$2; shift 2;;
  --snapshot) SNAP=$2; shift 2;; --disk) DISK=$2; shift 2;;
  --key-file) KEY_FILE=$2; shift 2;; --server) SERVER=$2; shift 2;;
  --dry-run) DRY=1; shift;; --yes) ASSUME=yes; shift;; --reboot) REBOOT=yes; shift;;
  --list) LIST=$2; shift 2;;
  *) die "unknown argument: $1";; esac; done

# restic is NOT in the Ubuntu live image: the stick carries the pinned binary per
# arch next to this script. grub-install and efibootmgr are NOT needed here —
# they run inside the restored system's chroot (step 10), which has its own.
HERE=$(cd "$(dirname "$0")" && pwd)
RARCH=$(uname -m | sed 's/aarch64/arm64/;s/x86_64/amd64/')
# Into the live root's overlay, NOT /run: /run is mounted noexec on 24.04
# ("Permission denied", VM test 2026-10-11).
if [[ -r $HERE/bin/restic-$RARCH ]] && ! command -v restic >/dev/null; then
  install -m 0755 "$HERE/bin/restic-$RARCH" /usr/local/bin/restic \
    || die "could not install restic from the stick into /usr/local/bin"
fi
# started by systemd (the recovery menu) there is no $HOME, and restic refuses to
# run without a cache dir ("neither $XDG_CACHE_HOME nor $HOME are defined")
export HOME=${HOME:-/root}
for t in restic sfdisk mkfs.ext4 mkfs.vfat blkid curl python3 nc; do
  command -v "$t" >/dev/null || die "restore volume is missing '$t' — this image is incomplete"
done

# ── 1. find the server ────────────────────────────────────────────────
# Broadcast probe first (this LAN is flat L2), then a static fallback. The
# restore volume must work on the worst day, which includes "the DHCP server
# you were relying on is on the machine that died".
if [[ -z $SERVER ]]; then
  log "discovering Noctyx server (UDP broadcast :20997)…"
  for i in 1 2 3; do
    REPLY=$(printf 'NX_DISCOVER?' | timeout 4 nc -u -b -w2 -p 40000 255.255.255.255 20997 2>/dev/null)
    [[ -n $REPLY ]] && break
    sleep 1
  done
  [[ -n $REPLY ]] || die "no Noctyx server found. Bring the network up (ip link set <if> up; dhclient) and/or pass --server <ip>. Discovery is UDP/20997 broadcast."
  # Take the URLs from the reply. Hardcoding a port here is how a port migration
  # silently breaks every existing restore volume.
  SERVER=$(printf '%s' "$REPLY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["address"])' 2>/dev/null)
  DIR_URL=$(printf '%s' "$REPLY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("directory_url",""))' 2>/dev/null)
  REST_URL_DISC=$(printf '%s' "$REPLY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("rest_url",""))' 2>/dev/null)
fi
: "${DIR_URL:=https://$SERVER:20999}"   # static fallback, last resort
: "${REST_URL_DISC:=https://$SERVER:20998}"
log "server: $DIR_URL"

# ── 2. trust ──────────────────────────────────────────────────────────
mkdir -p "$WORK"
curl -fsSk "$DIR_URL/api/v1/ca" -o "$WORK/ca.crt" || die "could not fetch CA from $DIR_URL"
CURL=( curl --fail --silent --show-error --cacert "$WORK/ca.crt" )

# ── 3. inventory ─────────────────────────────────────────────────────
"${CURL[@]}" "$DIR_URL/api/v1/hosts" -o "$WORK/hosts.json" || die "could not fetch host inventory"
get_host_field(){ python3 - "$WORK/hosts.json" "$HOST" "$1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); h=[x for x in d["hosts"] if x["name"]==sys.argv[2]]
if not h: sys.exit("host not in inventory")
print(h[0].get(sys.argv[3]) or "")
PY
}

if [[ $LIST == hosts ]]; then
  # name arch repo root_disk template macs(comma)
  python3 - "$WORK/hosts.json" <<'PY'
import json,sys
for h in json.load(open(sys.argv[1]))["hosts"]:
    print("\t".join([h["name"], h.get("arch",""), h.get("repo",""), h.get("root_disk") or "",
                     h.get("restore_template") or "", ",".join(h.get("macs") or [])]))
PY
  exit 0
fi
[[ -n $HOST ]] || { log "known hosts:"; python3 -c 'import json;print("  "+"  ".join(h["name"] for h in json.load(open("'"$WORK"'/hosts.json"))["hosts"]))'; die "--host is required"; }

REPO=$(get_host_field repo); ARCH=$(get_host_field arch); BOOT=$(get_host_field boot)
TPL=$(get_host_field restore_template); ROOTDISK_GUESS=$(get_host_field root_disk)
LVMV=$(get_host_field lvm); VG=$(get_host_field vg); LV=$(get_host_field lv)
[[ -n $REPO ]] || die "host '$HOST' not found in inventory"
log "restoring $HOST: repo=$REPO arch=$ARCH boot=$BOOT template=$TPL lvm=$LVMV"

if [[ -z $LIST ]] && [[ $TPL == gpt-esp-boot-lvm || $LVMV == True ]]; then
  die "LVM restore (template=$TPL) is Phase 5 and NOT implemented here. This engine
     currently rebuilds the plain GPT+ESP+ext4 layout used by the four aarch64
     clients. Refusing to guess at LVM on a machine you are about to wipe."
fi
[[ $BOOT == uefi-* ]] || die "boot mode '$BOOT' unsupported by this image"

DISK=${DISK:-$ROOTDISK_GUESS}
if [[ -z $LIST ]]; then
[[ -n $DISK ]] || die "--disk required (inventory had no root_disk)"
[[ -b $DISK ]] || die "$DISK is not a block device on this machine"
fi

# ── 4. the key ────────────────────────────────────────────────────────
# Never served by the directory service. Either on the restore volume (so the
# operator can rebuild a node without typing 44 characters) or typed in.
if [[ -z $KEY_FILE ]]; then
  # this stick's layout first, then an older stick's (cm-keys/)
  for c in /keys/$REPO.key /run/nx-keys/$REPO.key /mnt/nx-keys/$REPO.key "$HERE/keys/$REPO.key" \
           /cm-keys/$REPO.key /run/cm-keys/$REPO.key /mnt/cm-keys/$REPO.key "$HERE/cm-keys/$REPO.key"; do
    [[ -r $c ]] && KEY_FILE=$c && break
  done
fi
if [[ -n $KEY_FILE && -r $KEY_FILE ]]; then
  log "using repo key from $KEY_FILE"
else
  log "repo key for '$REPO' is required and is NOT downloadable from the server."
  read -rs -p "  paste key (input hidden): " k; echo
  printf '%s' "$k" > "$WORK/key"; KEY_FILE="$WORK/key"; unset k
fi

# The scheme STAYS. `rest:<head>:20998/...` fails with "first path segment
# in URL cannot contain colon" — the rest: backend wants rest:https://host:port/
# and a rehearsal dry-run caught it before a restore attempt ever did.
# Two DIFFERENT secrets, routinely confused:
#   repo encryption key  -> --password-file  (what decrypts the data)
#   REST basic-auth pass -> what the HTTP endpoint accepts
# AND, verified against 0.19.1 the hard way (401 on a rehearsal dry-run):
# the rest: backend does NOT read REST_USER/REST_PASSWORD env vars — the
# endpoint credential must be embedded in the location URL. Clients get this
# right via --repository-file; the restore engine previously set env vars and
# silently 401'd. Location file written 0600 under $WORK (0700 dir), not -r.
export REST_USER="@REPO_DIR_PREFIX@$REPO"
REST_PASSWORD=""
for c in "/creds/@REPO_DIR_PREFIX@$REPO.pass" "/run/nx-creds/@REPO_DIR_PREFIX@$REPO.pass" "$HERE/creds/@REPO_DIR_PREFIX@$REPO.pass" \
         "/cm-creds/@REPO_DIR_PREFIX@$REPO.pass" "/run/cm-creds/@REPO_DIR_PREFIX@$REPO.pass" "$HERE/cm-creds/@REPO_DIR_PREFIX@$REPO.pass"; do
  [[ -r $c ]] && REST_PASSWORD=$(cat "$c") && break
done
if [[ -z $REST_PASSWORD ]]; then
  echo "  REST basic-auth password for user '@REPO_DIR_PREFIX@$REPO' (separate from the repo key):"
  read -rs -p "  > " REST_PASSWORD; echo
  [[ -n $REST_PASSWORD ]] || die "no REST password; cannot talk to the repository endpoint"
fi
umask 077
printf 'rest:https://@REPO_DIR_PREFIX@%s:%s@%s/@REPO_DIR_PREFIX@%s/%s' "$REPO" "$REST_PASSWORD" \
  "${REST_URL_DISC#https://}" "$REPO" "$REPO" > "$WORK/repo-location"
RESTIC=( restic --cacert "$WORK/ca.crt" --password-file "$KEY_FILE"
         --repository-file "$WORK/repo-location" )

# ── 5. pick a snapshot ────────────────────────────────────────────────
log "reading snapshots for host $HOST…"
"${RESTIC[@]}" snapshots --host "$HOST" --json > "$WORK/snaps.json" 2>"$WORK/snaps.err" \
  || die "could not list snapshots: $(tail -2 "$WORK/snaps.err" | tr '\n' ' ')"
if [[ $LIST == snapshots ]]; then
  # short_id time(UTC, to the minute) profile size_processed
  python3 - "$WORK/snaps.json" <<'PY'
import json,sys
for x in reversed(json.load(open(sys.argv[1]))):
    prof = next((t[8:] for t in x.get("tags") or [] if t.startswith("profile-")), "?")
    gb = (x.get("summary") or {}).get("total_bytes_processed", 0) / 1e9
    print(f'{x["short_id"]}\t{x["time"][:16].replace("T", " ")}\t{prof}\t{gb:.0f} GB')
PY
  exit 0
fi
if [[ $SNAP == latest ]]; then
  SNAP=$(python3 -c 'import json;s=[x for x in json.load(open("'"$WORK"'/snaps.json")) if "profile-system" in (x.get("tags") or [])];print(s[-1]["short_id"] if s else "")')
  [[ -n $SNAP ]] || die "no snapshots exist for host '$HOST'"
fi
DESC=$(python3 -c 'import json;s=json.load(open("'"$WORK"'/snaps.json"));m=[x for x in s if x["short_id"].startswith("'"$SNAP"'")];print(m[-1]["time"][:19] if m else "?")')
log "snapshot $SNAP  taken $DESC"

# ── 6. confirm the destructive part ───────────────────────────────────
echo
echo "  ═══════════════════════════════════════════════════════════"
echo "  About to DESTROY and rebuild:  $DISK"
echo "  Host being restored:           $HOST"
echo "  Snapshot:                      $SNAP ($DESC)"
echo "  Partitioning:                  replay of the captured sfdisk dump"
[[ -n $DRY ]] && echo "  MODE: DRY RUN — nothing will be written"
echo "  ═══════════════════════════════════════════════════════════"
lsblk "$DISK" 2>/dev/null | sed 's/^/    /'
if [[ $ASSUME != yes && -z $DRY ]]; then
  printf "\n  Type the device name to continue (e.g. %s): " "$DISK"
  read -r ack; [[ $ack == "$DISK" ]] || die "confirmation mismatch, nothing was written"
fi

# ── 7. rebuild the disk ───────────────────────────────────────────────
# The partition table comes from the snapshot's captured fsmeta, not from a
# template. A machine restored to a subtly different layout than it was backed
# up with is the difference between "it came back" and "it came back wrong".
log "extracting captured filesystem metadata…"
SNAP_FULL=$(python3 -c 'import json;s=json.load(open("'"$WORK"'/snaps.json"));m=[x for x in s if x["short_id"].startswith("'"$SNAP"'")];print(m[-1]["id"])')
"${RESTIC[@]}" dump "$SNAP_FULL" >/dev/null 2>&1 || true
run mkdir -p "$WORK/meta"
"${RESTIC[@]}" restore "$SNAP_FULL" --target "$WORK/meta" \
  --include '@NODE_LIB@/fsmeta' 2>/dev/null || true
# The agent backs up the TIMESTAMPED capture dir; the `current` symlink is not a
# backup target and is NOT in the snapshot. Matching '*fsmeta/current' made a
# real rehearsal fail here: the metadata was in the snapshot all along under its
# timestamp, the engine just couldn't see it. Match any capture dir, prefer the
# newest, and require it to actually contain the partition dump.
META=""
for d in $(find "$WORK/meta" -path '*fsmeta/*' -type d 2>/dev/null | sort -r); do
  compgen -G "$d/partitions/*.sfdisk" > /dev/null && META=$d && break
done
[[ -n $META || -n $DRY ]] || die "no captured fsmeta with a partition dump in this snapshot — cannot rebuild the partition table faithfully"

# UNVERIFIED: sfdisk replay + esp selection
if [[ -n $META ]]; then
  SD=$(ls "$META"/partitions/*.sfdisk 2>/dev/null | head -1)
  [[ -n $SD ]] || die "no .sfdisk capture found under $META/partitions"
  log "replaying partition table from $(basename "$SD") onto $DISK"
  run sfdisk -f -Wnever "$DISK" < "$SD"
  run partprobe "$DISK" ; run udevadm settle
fi

# Derive partitions rather than assuming p1/p2 naming.
if [[ -n $DRY ]]; then
  # A dry run wrote no partition table, so derivation has nothing to read. In a
  # REAL run the derivation below catches "sfdisk replay produced a layout the
  # engine can't handle" — skipping it only in dry-run keeps that guard where it
  # matters and lets the rest of the chain (fstab rewrite, grub target choice)
  # be exercised before ever touching hardware.
  log "DRY: would derive root/ESP from the fresh table; simulating gpt-esp-ext4 naming"
  ROOTP="${DISK}p2"; ESPP="${DISK}p1"
else
ROOTP=$(lsblk -npo NAME,PARTLABEL,FSTYPE "$DISK" | awk '$3=="ext4"{print $1; exit}')
ESPP=$(lsblk -npo NAME,FSTYPE "$DISK" | awk '$2=="vfat"{print $1; exit}')
[[ -n $ROOTP && -n $ESPP ]] || die "after partitioning, expected an ext4 and a vfat partition on $DISK; found: $(lsblk -npo NAME,FSTYPE "$DISK" | tr '\n' ' ')"
fi
log "root=$ROOTP esp=$ESPP"

run mkfs.ext4 -q -F "$ROOTP"
run mkfs.vfat -F 32 "$ESPP"
run mkdir -p "$MNT"
run mount "$ROOTP" "$MNT"
run mkdir -p "$MNT/boot/efi"
run mount "$ESPP" "$MNT/boot/efi"

# ── 8. restore the data ───────────────────────────────────────────────
log "restoring snapshot into $MNT (this is the long part)…"
run "${RESTIC[@]}" restore "$SNAP_FULL" --target "$MNT" --delete 2>&1 | tail -5
[[ -n $DRY ]] || {
  # --delete above is scoped to $MNT only, and $MNT is a filesystem we just made.
  # Anything already there would have been destroyed by the mkfs before it.
  :
}

# ── 9. fstab: rewrite UUIDs, because we just re-made every filesystem ──
log "rewriting fstab with the new UUIDs…"
NEWROOT=$(blkid -s UUID -o value "$ROOTP"); NEWESP=$(blkid -s UUID -o value "$ESPP")
if [[ -z $DRY ]]; then
  cp "$MNT/etc/fstab" "$WORK/fstab.orig"
  python3 - "$MNT/etc/fstab" "$NEWROOT" "$NEWESP" <<'PY'
import re,sys
path,root,esp=sys.argv[1],sys.argv[2],sys.argv[3]
lines=open(path).read().splitlines()
out=[]
for l in lines:
    if not l.strip() or l.lstrip().startswith("#"): out.append(l); continue
    f=l.split()
    if len(f)<3: out.append(l); continue
    if f[1]=="/":           f[0]=f"UUID={root}"
    elif f[1]=="/boot/efi": f[0]=f"UUID={esp}"
    elif f[1]=="/boot" and f[2]=="ext4": f[0]=f"UUID={root}"
    out.append("\t".join(f))
open(path,"w").write("\n".join(out)+"\n")
print("  fstab rewritten:")
for l in out:
    if l.strip() and not l.lstrip().startswith("#"): print("   ",l)
PY
fi

# ── 10. bootloader (UNVERIFIED until Phase 4 hardware run) ───────────
log "reinstalling bootloader into the chroot…"
if [[ -z $DRY ]]; then
  for d in dev dev/pts sys proc run; do run mkdir -p "$MNT/$d"; run mount --bind "/$d" "$MNT/$d"; done

  TARGET=$( [[ $ARCH == aarch64 ]] && echo arm64-efi || echo x86_64-efi )
  [[ -d $MNT/usr/lib/grub/$TARGET ]] || die "grub modules for $TARGET absent from the restored system"
  chroot "$MNT" grub-install --target="$TARGET" --efi-directory=/boot/efi \
        --bootloader-directory=/boot/grub --no-nvram || die "grub-install failed"
  chroot "$MNT" update-initramfs -u -k all || log "WARNING: initramfs rebuild failed — node may not boot"
  chroot "$MNT" update-grub || log "WARNING: update-grub failed"

  # Write an EFI boot entry. Names differ per distro; try the common ones.
  DISKP="$DISK" ESPP="$ESPP" chroot "$MNT" sh -c '
    for f in EFI/ubuntu/shimaa64.efi EFI/ubuntu/shimx64.efi EFI/boot/bootaa64.efi EFI/boot/bootx64.efi; do
      if [ -f "/boot/efi/$f" ]; then
        efibootmgr -c -d "$DISKP" -p 1 -L Ubuntu -l "\\\\$f" && { echo "  efibootmgr entry created for $f"; exit 0; }
      fi
    done
    echo "  WARNING: no shim/loader found on the ESP — the firmware may need a manual boot entry"'
else
  log "DRY: would run grub-install --target=$( [[ $ARCH == aarch64 ]] && echo arm64-efi || echo x86_64-efi )"
fi

# ── 11. clean shutdown of the mount tree ──────────────────────────────
log "syncing and unmounting…"
run sync
for d in proc sys dev/pts dev run boot/efi; do run umount -l "$MNT/$d" 2>/dev/null; done
run umount -l "$MNT" 2>/dev/null
run sync

log "RESTORE COMPLETE for $HOST (snapshot $SNAP, taken $DESC)"
echo "  Remove the restore volume, then reboot."
echo "  If it does not come up, the two things to check first are the EFI boot"
echo "  order and whether update-initramfs actually succeeded above."
[[ $REBOOT == yes ]] && { echo "  rebooting in 10s (Ctrl-C to stop)"; sleep 10; reboot; }
