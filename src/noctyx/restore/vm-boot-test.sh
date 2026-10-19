#!/usr/bin/env bash
# vm-boot-test.sh — boot recovery media in a UEFI VM on the head node and say
# whether it reaches a live login. Proves a stick (or a staged image) BOOTS
# without touching a real node. Reads the media snapshot-only: nothing is written.
#
#   sudo noctyx tool vm-boot-test --arch arm64 --media /dev/sde            # the built stick
#   sudo noctyx tool vm-boot-test --arch arm64 --media @VMTEST_DIR@/stick.img
#   sudo noctyx tool vm-boot-test --arch amd64 --media /dev/sdX --minutes 6
#
# arm64 on the x86 head is TCG emulation: ~6–8 min to the login target (KVM for
# amd64: ~1 min). Serial console is logged; the guest framebuffer can be
# screenshotted while it runs:  python3 <tool dir>/vm-screenshot.py <workdir>/mon.sock out.png
# PASS = serial shows the getty/login target or the Subiquity console.
# FAIL = casper cannot find a medium, initramfs shell, or the timeout.
# Needs: qemu-system-arm + qemu-efi-aarch64 (arm64) / qemu-system-x86 + ovmf (amd64).
set -euo pipefail
ARCH="" MEDIA="" MIN=12 KEEP=""
while [[ $# -gt 0 ]]; do case $1 in
  --arch) ARCH=$2; shift 2;; --media) MEDIA=$2; shift 2;; --minutes) MIN=$2; shift 2;; --keep) KEEP=1; shift;;
  *) echo "usage: $0 --arch arm64|amd64 --media DEV|IMG [--minutes N] [--keep]" >&2; exit 2;;
esac; done
[[ -n $ARCH && -e $MEDIA ]] || { echo "need --arch and an existing --media" >&2; exit 2; }
W=${NX_VMTEST_DIR:-@VMTEST_DIR@}/run-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$W"
case $ARCH in
  arm64) QEMU=qemu-system-aarch64; CODE=/usr/share/AAVMF/AAVMF_CODE.fd; VARS=/usr/share/AAVMF/AAVMF_VARS.fd
         MACH=(-M virt -cpu max -accel tcg,thread=multi); [[ -e /dev/kvm && $(uname -m) == aarch64 ]] && MACH=(-M virt -cpu host -accel kvm);;
  amd64) QEMU=qemu-system-x86_64; CODE=/usr/share/OVMF/OVMF_CODE_4M.fd; VARS=/usr/share/OVMF/OVMF_VARS_4M.fd
         MACH=(-M q35 -cpu max); [[ -e /dev/kvm ]] && MACH=(-M q35 -cpu host -enable-kvm);;
  *) echo "--arch arm64|amd64" >&2; exit 2;;
esac
command -v $QEMU >/dev/null || { echo "$QEMU not installed" >&2; exit 2; }
cp "$VARS" "$W/vars.fd"
echo "== VM ($ARCH) booting $MEDIA — serial: $W/serial.log — monitor: $W/mon.sock"
$QEMU -name nx-boot-test "${MACH[@]}" -smp 6 -m 6G \
  -drive if=pflash,format=raw,readonly=on,file="$CODE" -drive if=pflash,format=raw,file="$W/vars.fd" \
  -device qemu-xhci -drive file="$MEDIA",format=raw,if=none,id=usb0,snapshot=on -device usb-storage,drive=usb0,removable=on \
  -device virtio-gpu-pci -display none -device usb-kbd -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
  -monitor unix:"$W/mon.sock",server,nowait -serial file:"$W/serial.log" -rtc base=utc >"$W/qemu.log" 2>&1 &
QPID=$!
verdict=""
for ((i = 0; i < MIN * 6; i++)); do
  sleep 10
  kill -0 $QPID 2>/dev/null || { verdict="FAIL: qemu exited — $(tail -2 "$W/qemu.log")"; break; }
  # strip ANSI escapes first: the live ISO's kernel keeps systemd on the display, and what
  # reaches serial is Subiquity's text UI (its welcome banner is the PASS marker there)
  txt=$(sed -E 's/\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]//g' "$W/serial.log" 2>/dev/null | tr -c '[:print:]\n' '.' || true)
  # markers seen for real: systemd's getty.target (dual image, console on serial); the getty
  # issue banner "Ubuntu 24.04.5 LTS ubuntu-server ttyAMA0", "waiting for cloud-init" and the
  # "[ Help ]" TUI header (the stick: Subiquity on the serial getty)
  if grep -qE "getty.target|Login Prompts|Subiquity, the installer|login:|Ubuntu [0-9.]+ LTS [A-Za-z0-9-]+ tty|waiting for cloud-init|\[ Help \]" <<<"$txt"; then verdict="PASS: live login reached"; break; fi
  if grep -qE "Unable to find a medium|\(initramfs\)|Kernel panic|you need to load the kernel first" <<<"$txt"; then
    verdict="FAIL: $(grep -oE 'Unable to find a medium[^.]*|\(initramfs\)|Kernel panic[^.]*|you need to load the kernel first' <<<"$txt" | head -1)"; break; fi
done
[[ -n $verdict ]] || verdict="FAIL: no login within $MIN min (last: $(sed -E 's/\x1b\[[0-9;?]*[A-Za-z]//g' "$W/serial.log" | tr -c '[:print:]\n' '.' | grep -v '^[. ]*$' | tail -1 | cut -c1-120))"
echo "== $verdict"
[[ -n $KEEP ]] || kill $QPID 2>/dev/null || true
[[ $verdict == PASS* ]]
