#!/usr/bin/env bash
# Rehearse a full HEAD-NODE rebuild in a VM, end to end, without touching the
# real head node:
#   phase 1  rescue VM (stock Ubuntu cloud image) + an EMPTY virtual disk the size
#            of the head's root disk; runs noctyx-restore-head.sh against the
#            repository endpoint on the NAS (the path that works with the head dead)
#   phase 2  boots ONLY the restored disk, NO network (a live copy of the head node
#            must never reach the LAN: it would claim the head's address and run the tick against
#            the real nodes); passes when a login prompt appears on the serial console
#
#   sudo noctyx tool rehearse-head                 # run (hours) — use systemd-run
#   WORK=<scratch dir> SNAP=latest ...
#
# Scratch lives on the NVMe (fast, local, NOT a backup target — it is deleted
# after the rehearsal). Loop-mounting the restored LVM on the head itself is
# deliberately avoided: it carries the SAME VG/PV UUIDs as the live root volume.
# Run through `noctyx tool rehearse-head`, which fills in this site's paths.
set -Eeuo pipefail
trap 'echo "REHEARSAL: FAILED at line $LINENO: $BASH_COMMAND"' ERR
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG=${NOCTYX_CONFIG:?run through: noctyx tool rehearse-head}
WORK=${WORK:-@REHEARSAL_DIR@}
SNAP=${SNAP:-latest}
# the head node: the host in the site config whose address is the server's
read -r HOST REPO < <(python3 -c "import json,sys;c=json.load(open(sys.argv[1]));h=next(x for x in c['hosts'] if x.get('address')==c['server']['address']);print(h['name'],h['repo'])" "$CFG")
WAIT_H=${WAIT_H:-8}              # how long to wait for the snapshot to exist on the NAS
IMG_URL=https://cloud-images.ubuntu.com/noble/current
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.fd OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd
log() { printf '[rehearsal %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
install -d -m 0700 "$WORK"; cd "$WORK"
# the payload/seed carry the repo key + endpoint password: gone on ANY exit
trap 'rm -rf "$WORK"/payload "$WORK"/payload.iso "$WORK"/seed.iso "$WORK"/user-data "$WORK"/repo-location' EXIT
T0=$(date +%s)

REST_HOST=$(python3 -c "import json;s=json.load(open('$CFG'))['server'];print(s.get('rest_host') or s['address'])")
PORT=$(python3 -c "import json;print(json.load(open('$CFG'))['server']['rest_port'])")
umask 077
printf 'rest:https://@REPO_DIR_PREFIX@%s:%s@%s:%s/@REPO_DIR_PREFIX@%s/%s' "$REPO" "$(cat @REPO_CREDS@/@REPO_DIR_PREFIX@$REPO.pass)" \
  "$REST_HOST" "$PORT" "$REPO" "$REPO" > repo-location
RESTIC=(restic --cacert @TLS@/ca.crt --password-file @REPO_KEYS@/$REPO.key
        --repository-file "$WORK/repo-location" --no-cache --no-lock)
log "endpoint https://$REST_HOST:$PORT (repo $REPO), scratch $WORK"
[[ $REST_HOST == "$(python3 -c "import json;print(json.load(open('$CFG'))['server']['address'])")" ]] \
  || noctyx nas-rest status >/dev/null || noctyx nas-rest start

# ── 0. wait for a system snapshot of the head to exist THERE ──────────
deadline=$(( $(date +%s) + WAIT_H * 3600 ))
until "${RESTIC[@]}" snapshots --host $HOST --tag profile-system --json 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin) else 1)'; do
  (( $(date +%s) < deadline )) || { log "no system snapshot of $HOST on the endpoint after ${WAIT_H}h"; exit 1; }
  log "no system snapshot of $HOST in the NAS repo yet (migration still copying?) — waiting 5 min"
  sleep 300
done
"${RESTIC[@]}" snapshots --host $HOST --tag profile-system --json > snaps.json
read -r SID META < <(python3 - snaps.json "$SNAP" <<'EOF'
import json, sys
s = sorted(json.load(open(sys.argv[1])), key=lambda x: x["time"])
w = sys.argv[2]
s = [x for x in s if w in ("latest", x["short_id"], x["id"], (x.get("original") or "")[:8])]
x = s[-1]
print(x["short_id"], next(p for p in x["paths"] if p.startswith("@NODE_LIB@/fsmeta/")))
EOF
)
log "rehearsing snapshot $SID"

# ── 1. disks sized from the snapshot's own metadata ───────────────────
rm -rf meta; "${RESTIC[@]}" restore "$SID" --include "$META" --target meta >/dev/null
M=meta$META
ESP=$(sed -n 's/.*PATH="\([^"]*\)".*MOUNTPOINT="\/boot\/efi".*/\1/p' "$M/lsblk.txt" | head -1)
SRCDISK=$(sed -n 's/^device: //p' "$(grep -l "^$ESP " "$M"/partitions/*.sfdisk | head -1)")
SIZE=$(sed -n "s#.*NAME=\"$(basename "$SRCDISK")\" .*SIZE=\"\([0-9]*\)\".*#\1#p" "$M/lsblk.txt" | head -1)
rm -f target.raw; truncate -s "$SIZE" target.raw
log "target.raw: $SIZE bytes, sparse (= $SRCDISK)"
# Other local disks the restored fstab mounts WITHOUT nofail (e.g. /storage):
# a real rebuild has them (they didn't die); here a small empty ext4 carrying
# the same UUID stands in, or boot would stop in emergency mode.
STUBS=()
while read -r uuid mp; do
  f=stub-$(echo "$mp" | tr / _).raw; rm -f "$f"; truncate -s 2G "$f"
  mkfs.ext4 -F -q -U "$uuid" "$f"; STUBS+=("$f"); log "stub disk for $mp (UUID $uuid)"
done < <(awk '!/^#/ && $1 ~ /^UUID=/ && $3 == "ext4" && $2 !~ /^\/(boot|boot\/efi)?$/ && $4 !~ /nofail/ \
              {sub("UUID=","",$1); print $1, $2}' "$M/fstab")

# ── 2. rescue image, payload, cloud-init ──────────────────────────────
if [[ ! -f noble.img ]]; then
  log "downloading Ubuntu 24.04 cloud image"
  curl -fsSL -o noble.img.part "$IMG_URL/noble-server-cloudimg-amd64.img"
  curl -fsSL -o SHA256SUMS "$IMG_URL/SHA256SUMS"
  want=$(awk '/noble-server-cloudimg-amd64.img$/{print $1}' SHA256SUMS)
  [[ $(sha256sum noble.img.part | cut -d' ' -f1) == "$want" ]] || { log "cloud image checksum MISMATCH"; exit 1; }
  mv noble.img.part noble.img
fi
rm -f rescue.qcow2; qemu-img create -q -f qcow2 -b noble.img -F qcow2 rescue.qcow2 20G
rm -rf payload; install -d -m 0700 payload
cp /usr/local/bin/restic "$HERE/noctyx-restore-head.sh" payload/
cp @TLS@/ca.crt payload/ca.crt
cp @REPO_KEYS@/$REPO.key payload/repo.key
cp repo-location payload/repo-location
genisoimage -quiet -V CMPAYLOAD -r -J -o payload.iso payload
cat > user-data <<EOF
#cloud-config
package_update: true
packages: [lvm2, dosfstools, e2fsprogs, gdisk]
runcmd:
  - [bash, -c, "mkdir -p /payload && mount -o ro /dev/disk/by-label/CMPAYLOAD /payload && install -m 0755 /payload/restic /usr/local/bin/restic && bash /payload/noctyx-restore-head.sh --disk /dev/vdb --repo-file /payload/repo-location --key /payload/repo.key --ca /payload/ca.crt --host $HOST --snapshot $SID --yes --rehearsal 2>&1 | tee /dev/ttyS0; echo REHEARSAL-RESCUE-EXIT \${PIPESTATUS[0]} > /dev/ttyS0; poweroff"]
EOF
printf 'instance-id: nx-rehearsal-%s\nlocal-hostname: nx-rescue\n' "$SID" > meta-data
cloud-localds seed.iso user-data meta-data

# ── 3. phase 1: restore inside the rescue VM ──────────────────────────
cp "$OVMF_VARS" vars-rescue.fd
log "phase 1: rescue VM restoring into target.raw (log: $WORK/rescue.log)"
rm -f rescue.log
timeout 12h qemu-system-x86_64 -name nx-rescue -enable-kvm -machine q35 -cpu host -smp 8 -m 16G \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" -drive if=pflash,format=raw,file=vars-rescue.fd \
  -drive file=rescue.qcow2,if=virtio,format=qcow2 \
  -drive file=target.raw,if=virtio,format=raw,cache=none,aio=threads,discard=unmap \
  -drive file=seed.iso,media=cdrom,readonly=on -drive file=payload.iso,media=cdrom,readonly=on \
  -nic user,model=virtio-net-pci -serial file:rescue.log -display none -no-reboot || true
grep -a "CM-RESTORE-HEAD: DONE" rescue.log || { log "phase 1 FAILED — see rescue.log"; grep -a "CM-RESTORE-HEAD" rescue.log | tail -3; exit 1; }

# ── 4. phase 2: boot the restored disk alone, no network ──────────────
cp "$OVMF_VARS" vars-boot.fd
STUBARGS=(); for f in "${STUBS[@]}"; do STUBARGS+=(-drive "file=$f,if=virtio,format=raw"); done
log "phase 2: booting the restored disk (no network; log: $WORK/boot.log)"
rm -f boot.log
qemu-system-x86_64 -name nx-restored-head -enable-kvm -machine q35 -cpu host -smp 8 -m 16G \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" -drive if=pflash,format=raw,file=vars-boot.fd \
  -drive file=target.raw,if=virtio,format=raw,cache=none,aio=threads \
  "${STUBARGS[@]}" -nic none -serial file:boot.log -display none -no-reboot &
QPID=$!
verdict="TIMEOUT (no login prompt within 20 min)"
for _ in $(seq 240); do
  sleep 5
  if grep -aqE "login: *$|login:" boot.log; then verdict="PASS (reached a login prompt)"; break; fi
  if grep -aq "emergency mode" boot.log; then verdict="FAIL (emergency mode)"; break; fi
  kill -0 $QPID 2>/dev/null || { verdict="FAIL (VM exited)"; break; }
done
sleep 20   # let late units log
kill $QPID 2>/dev/null; wait $QPID 2>/dev/null || true
failed=$(grep -a "\[FAILED\]" boot.log | sed 's/.*\[FAILED\] //' | sort -u | head -20 || true)

# ── 5. verdict (disks + logs kept for inspection; secrets removed by the EXIT trap)
mins=$(( ($(date +%s) - T0) / 60 ))
log "RESULT: $verdict · snapshot $SID · $mins min total"
[[ -n $failed ]] && { log "units that failed during the offline boot:"; echo "$failed" | sed 's/^/    /'; }
log "restored-disk usage: $(du -h --apparent-size target.raw | cut -f1) apparent, $(du -h target.raw | cut -f1) allocated"
python3 - "$verdict" "$SID" "$mins" "@AUDIT@" <<'EOF'
import json, sys, datetime, pathlib
rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
       "event": "rehearsal-head", "verdict": sys.argv[1], "snapshot": sys.argv[2], "minutes": int(sys.argv[3])}
with open(sys.argv[4], "a") as f: f.write(json.dumps(rec) + "\n")
EOF
# On PASS the multi-hundred-GB disk images are dead weight — and a scratch dir that
# something else mirrors (no --delete) would copy them away forever, so leaving
# them here copies them to the NAS (it did, 2026-09-27). Keep the logs + verdict
# under /var/log; keep everything on FAIL for inspection.
if [[ $verdict == PASS* ]]; then
  K=$(dirname @JOB_LOGS@)/rehearsal-$(date -u +%Y%m%dT%H%MZ); install -d -m 0750 "$K"
  cp -a rescue.log boot.log snaps.json "$K"/; cp -a meta "$K"/fsmeta
  cd /; rm -rf "$WORK"; log "PASS: scratch removed; logs kept in $K"
fi
[[ $verdict == PASS* ]]
