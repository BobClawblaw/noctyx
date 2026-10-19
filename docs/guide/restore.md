# Restore

There are three levels of restore. You can pull one path out of a snapshot, rebuild a dead node
bare-metal from the recovery stick, or rebuild the head node itself, whose layout is different (it uses
LVM). Path restores never overwrite anything unless you ask for `--in-place --yes`. Bare-metal restores ask
you to type the device name before they erase it. This page also covers `copy`, which carries history over
from a retired repository tier, and how to prove that a stick boots without touching a real machine.

Paths below are the defaults from `src/noctyx/paths.py`. Run `noctyx paths` to see the values your site
uses.

## Restore a path

`noctyx restore --host NAME --path /ABS/PATH [--snapshot latest|ID] [--to staging|host] [--in-place --yes]`

| Mode | Where the files go | Who runs restic |
|---|---|---|
| `--to staging` (default) | `<parent of repo_root>/restores/<host>/<UTC stamp>/` on the NAS mount, browsable over NFS/SMB; the live host is not touched | the head node, as the repository owner, `--no-lock` |
| `--to host` | `/var/lib/noctyx/restore/<UTC stamp>/` on the host | the host's agent |
| `--in-place --yes` | `/` on the live host, **overwriting** files | the host's agent |

```bash
# /etc/nginx from gpu-01's newest snapshot, to staging on the NAS
sudo noctyx restore --host gpu-01 --path /etc/nginx

# a specific snapshot, into a fresh directory on the host itself
sudo noctyx snapshots --host gpu-01 --limit 10
sudo noctyx restore --host gpu-01 --snapshot 1a2b3c4d --path /srv/app/data --to host

# put it back where it was (CLI only; the console never restores in place)
sudo noctyx restore --host gpu-01 --snapshot 1a2b3c4d --path /etc/nginx --in-place --yes
```

- `--snapshot` takes a hex id of 8 to 64 characters, or `latest`. `latest` means the newest snapshot of
  that host, whatever its profile.
- `--path` must be absolute. restic keeps the full path under the target directory. For example, with
  `repo_root` set to `/mnt/nas/backups`, staging puts `/etc/nginx` at
  `/mnt/nas/restores/gpu-01/20261019T101500Z/etc/nginx`.
- `--in-place` without `--yes` stops with "--in-place overwrites files on the live host; add --yes if you
  mean it". `--in-place` wins over `--to`.
- A restore runs as a detached job. Use `--foreground` or `--no-follow` as needed (see
  [Backups › Detached jobs](backups.md#detached-jobs-and-follow)).
- Every restore is audited (`event: restore`) with its target and exit code. On the host, restic's
  output is also in `/var/lib/noctyx/last-restore.log`.

The console offers only staging and host restores.

## `copy`: carry history over from a retired tier

`copy` exists for one job: moving the snapshots of an older repository tier (`server.hot_root`) into the
primary repositories under `server.repo_root`. Once those are drained it reports "up to date". If the
site config has no `hot_root`, it stops with "no retired hot tier … nothing to copy".

```bash
sudo noctyx copy --dry-run            # per repository: N of M snapshots would copy (touches nothing, not even init)
sudo noctyx copy [--host gpu-01]      # init the destination with the same key if needed, then restic copy
```

- `copy` refuses to run if the NAS mount (the NAS spindle's `mount`) is missing, so that it never fills
  the root disk.
- It holds the destination spindle's lock, waiting for it if needed.
- It runs restic as the repository owner.
- It compares snapshots by their `original` id, so a snapshot that was already copied is not copied
  again.

## Rebuild a node bare-metal: the recovery stick

The recovery stick boots a stock Ubuntu 24.04 live image and carries a payload:

- the repository keys and the endpoint credentials
- the CA
- the restore engine (`noctyx-restore.sh`) and the recovery menu
- pinned restic binaries for amd64 and arm64

The engine reads the inventory from the head node's directory service, picks the snapshot and rebuilds
the disk from the partition table that the agent captured in that snapshot's fsmeta.

> **Status, as stated in `noctyx-restore.sh` itself:** the engine has not yet restored a real machine.
> The sfdisk replay and the bootloader steps are marked UNVERIFIED. Run it against hardware you can
> afford to lose before you rely on it. It rebuilds the plain **GPT + ESP + ext4** layout with
> `boot: uefi-*`, and it **refuses LVM** (`restore_template: gpt-esp-boot-lvm` or `lvm: true`). For an
> LVM layout, see [Rebuild the head node](#rebuild-the-head-node).

### Before you build: the inventory

The menu and the engine read `GET https://<head>:20999/api/v1/hosts`, which the directory service serves
from `/etc/noctyx/state/hosts.json`. Regenerate that file after you change hosts:

```bash
sudo noctyx directory
```

It includes each backed-up host's `arch`, `repo`, `boot`, `root_disk`, `lvm`, `vg`, `lv`,
`restore_template` and `address` from the site config. It also includes the host's NIC MAC addresses,
taken from Prometheus (`node_network_info`), so that the menu can recognise the machine it booted on. If
Prometheus is down, the MACs from the previous run are kept.

### What goes on the stick

`make-restore-usb` writes the payload onto a FAT partition. Every builder below calls it for you.

| On the stick | From the head node |
|---|---|
| `keys/<repo>.key` | `/etc/noctyx/repo-keys/*.key` |
| `creds/nx-<repo>.pass` | `/etc/noctyx/repo-creds/*.pass` |
| `noctyx-restore.sh`, `noctyx-menu.sh` | the package's `restore/` directory |
| `stick.conf` | `DEFAULT_SERVER=<server.address>`, used when discovery gets no answer |
| `bin/restic-amd64`, `bin/restic-arm64` | restic `restic_version` (default 0.19.1), downloaded once to `/srv/noctyx/isos/restic-<ver>/` and checked against the release's SHA256SUMS |
| `restore-ca.crt` | `/etc/noctyx/tls/ca.crt` |

The stick holds keys that decrypt your backups, so keep it locked away when you are not restoring.

### Build the dual-arch stick (amd64 + arm64 on one stick)

The stick has four partitions:

- **p1** `NOCTYXLIVE`: ext4, holding both live ISOs and the arm64 kernel and initrd
- **p2**: a 64 MB ESP with both `bootx64.efi` and `bootaa64.efi`
- **p3** `NOCTYX`: FAT32, the payload
- **p4** `CIDATA`: 8 MiB, a cloud-init seed that starts the recovery menu on tty1 in place of the
  installer

```bash
# 1. cache both live ISOs (sha256-verified against cdimage.ubuntu.com) in /srv/noctyx/isos
sudo noctyx tool build-stick-single --fetch-only --arch amd64
sudo noctyx tool build-stick-single --fetch-only --arch arm64

# 2. stage p1.img + esp.img under /srv/noctyx/isos/stage-NOCTYX (once; about the size of both ISOs)
sudo noctyx tool build-stick - --stage

# 3. write the stick: you must type the device name; then partitions, payload, and a full verify
sudo noctyx tool build-stick /dev/sdX --build
```

- `--build` accepts only `/dev/sd*` and `/dev/nvme*`, or `/dev/loop*` with `NX_ALLOW_LOOP=1` (for a
  test image made with `losetup -fP --show stick.img`).
- No partition on the device may be mounted.
- The device must be larger than p1 plus the ESP plus 512 MB.

When the stick is written, `--build` checks all of the following and ends with "ALL CHECKS PASSED":

- the GPT
- the first 64 MiB of each ISO copy
- the arm64 kernel and initrd
- both loaders on the ESP
- the payload files
- the CIDATA seed
- that the stick's restic version equals the head node's
- that the stick's key and credential open a live repository (`restic cat config`)

If a step fails, the repair modes redo part of the build: `/dev/sdX --boot` rewrites the ESP, and
`/dev/sdX --payload` redeploys the payload and runs the verification again.

Tools the build uses on the head node: `losetup`, `mkfs.ext4`, `mkfs.vfat`, `mformat` and `mcopy`
(mtools), `sgdisk`, `partprobe`, `wipefs`, `curl`.

### Build a single-arch stick

```bash
sudo noctyx tool build-stick-single --arch amd64 --usb /dev/sdX     # or --arch arm64
sudo noctyx tool build-stick-single --arch amd64 --dry-run          # no writes
sudo noctyx tool build-stick-single --arch amd64 --finish /dev/sdX  # resume after a replug (p3 already formatted)
sudo noctyx tool build-stick-single --arch amd64 --iso /path/to.iso --usb /dev/sdX
```

- The build writes the ISO verbatim at offset 0 and then adds p3 `NOCTYX`. For an amd64 ISO it rewrites
  the GPT. For an arm64 ISO, which has only an MBR, it appends p3 to the MBR.
- The device must be `/dev/sd*` and at least 24 000 MB.
- The verification also compares the stick's engine with the one the directory service serves.
- This stick has **no CIDATA seed**, so the menu does not start by itself. Boot the stick, choose the
  live session, mount the `NOCTYX` partition and run the engine by hand:

```bash
lsblk -f | grep NOCTYX
sudo mount /dev/sdb3 /mnt
sudo bash /mnt/noctyx-restore.sh --server 192.0.2.10 --host gpu-01     # finds keys/ and creds/ next to itself
```

### Add the payload to a stick you already have

```bash
sudo noctyx tool make-restore-usb --usb /dev/sdX3              # an existing FAT partition
sudo noctyx tool make-restore-usb --dry-run    # list what would be copied (nothing written or downloaded)
```

The partition must be FAT. Both forms need `--usb`.

### The recovery menu

On the dual-arch stick, the menu comes up on tty1 by itself. It does the following:

1. It waits up to 45 s for a DHCP lease. If none arrives, it offers to fix the network: pick the
   interface, try DHCP, or set a static address and gateway.
2. It finds the head node by UDP broadcast on port 20997 (`NX_DISCOVER?`). If nothing answers, it uses
   the stick's `DEFAULT_SERVER`.
3. It preselects the **host** by matching this machine's MACs against the inventory, or else takes the
   first host of the same arch. It preselects the newest **`system` snapshot** and the inventory's
   `root_disk` as the **disk**.

| Item | What it does |
|---|---|
| Host / Snapshot / Disk / Server | change the target; only `system` snapshots are offered, and the stick itself is never offered as a disk |
| Dry run | runs the engine with `--dry-run`: reads everything, writes nothing |
| RESTORE | runs the engine. The engine still asks you to type the disk name, and offers a reboot when it is done |
| Shell | a login shell (`exit` returns to the menu). The engine log is `/run/nx-menu.log` |
| Reboot / Power off | |

The menu does not rebuild a head node (`restore_template: gpt-esp-boot-lvm`). Use the shell and the
[head-node restore](#rebuild-the-head-node) instead.

### The engine by hand

```bash
sudo bash noctyx-restore.sh --list hosts                                  # name arch repo root_disk template macs
sudo bash noctyx-restore.sh --host gpu-01 --list snapshots                # id time profile size
sudo bash noctyx-restore.sh --host gpu-01 --disk /dev/nvme0n1 --dry-run
sudo bash noctyx-restore.sh --server 192.0.2.10 --host gpu-01 --snapshot 1a2b3c4d --disk /dev/nvme0n1 [--reboot]
```

Other flags are `--key-file PATH` and `--yes` (skips the typed confirmation). The engine works in this
order:

1. It finds the server by discovery, or uses `--server`. If the discovery reply gives no URLs, it falls
   back to `:20999` for the directory and `:20998` for the repositories.
2. It fetches the CA and the inventory.
3. It finds the key and credential on the stick. If they are missing, it prompts for them; they are
   never downloaded.
4. It restores the snapshot's fsmeta, replays the captured `sfdisk` dump onto the disk, and runs mkfs
   for the ext4 root and the vfat ESP.
5. It restores the snapshot, rewrites the fstab UUIDs, and runs `grub-install`, `update-initramfs` and
   `update-grub` in a chroot.
6. It creates an EFI boot entry.

## Rebuild the head node

`noctyx-restore-head.sh` handles the head node's layout: GPT, an ESP, `/boot` and an LVM root. It
recreates the partition table, the LVM PV, VG and LVs, and every filesystem **with their original UUIDs**,
using the snapshot's fsmeta (`sfdisk` dump, `blkid.txt`, LVM metadata backup). fstab, GRUB and `root=` then
work unchanged. It can run from any rescue Linux that reaches a repository endpoint. The endpoint on the
NAS works even with the head node dead.

Paths that the profile excludes are **not** restored: separate mounts under `/mnt`, `/var/lib/docker`,
`/snap` and the apt lists. They come back from their own disks or their own backups. The script creates
empty mount points for them.

The stick carries a copy rendered for this site: `noctyx-restore-head.sh` on its `NOCTYX` partition, next
to `noctyx-restore.sh` (`--help` prints its usage).

In the rescue system, write the repository location (the endpoint credential is in the URL) and run the
script:

```bash
umask 077
printf 'rest:https://nx-head-01:%s@192.0.2.5:20998/nx-head-01/head-01' "$(cat creds/nx-head-01.pass)" > /tmp/repo-location
sudo bash noctyx-restore-head.sh --disk /dev/nvme0n1 --repo-file /tmp/repo-location \
     --key keys/head-01.key --ca restore-ca.crt [--host head-01] [--snapshot latest|ID] [--yes]
```

- Without `--yes`, it asks you to type `ERASE`.
- It refuses a target disk that is smaller than the source layout.
- The rescue system needs `restic`, `sfdisk`, `pvcreate`, `vgcfgrestore`, `vgchange`, `mkfs.ext4`,
  `mkfs.vfat`, `python3`, `chroot` and `blkid`.
- `--rehearsal` adds a serial console and skips the EFI NVRAM write. It is meant for VMs.

### Rehearse it in a VM

`rehearse-head` rebuilds the head node from its newest `system` snapshot in a VM. It never touches the real
head node.

- **Phase 1:** a rescue VM (the Ubuntu 24.04 cloud image) runs `noctyx-restore-head.sh` against the
  NAS endpoint into an empty, sparse disk image that is the size of the real root disk.
- **Phase 2:** it boots only the restored disk, **with no network**, so that the copy cannot reach the
  LAN. The run passes when a login prompt appears on the serial console within 20 minutes.

```bash
# takes hours: run it in its own unit
sudo systemd-run --unit=noctyx-rehearse-head --collect "$(command -v noctyx)" tool rehearse-head
sudo WORK=/var/tmp/noctyx-rehearsal SNAP=latest WAIT_H=8 noctyx tool rehearse-head   # the knobs, with their defaults
```

- `WORK` is the scratch directory, which needs hundreds of GB.
- `WAIT_H` is how long to wait for a snapshot to appear on the endpoint.
- If the restored fstab has other ext4 mounts without `nofail`, a small empty disk with the same UUID
  stands in for each one.
- The repository key and credential copied into the VM are deleted on any exit.
- The verdict is audited as `rehearsal-head`.
- On PASS, the logs and fsmeta are kept in `/var/log/noctyx/rehearsal-<stamp>/` and the scratch
  directory is deleted. On FAIL, everything is kept for inspection.
- It needs root, KVM, `qemu-system-x86_64`, OVMF (`OVMF_CODE_4M.fd`), `qemu-img`, `cloud-localds` and
  `genisoimage`.

## Prove a stick boots: `vm-boot-test`

```bash
sudo noctyx tool vm-boot-test --arch amd64 --media /dev/sdX                 # the built stick
sudo noctyx tool vm-boot-test --arch arm64 --media /srv/noctyx/vmtest/stick.img --minutes 15
```

- The test boots the media in a UEFI VM. The media is opened `snapshot=on`, so nothing is written to it.
- It prints `PASS: live login reached` when the serial console shows a getty or login prompt, or the
  live installer's console.
- It prints `FAIL` when casper cannot find a medium, when it lands in an initramfs shell or a kernel
  panic, or when `--minutes` (default 12) runs out.
- amd64 uses KVM and takes about a minute. arm64 on an x86 head node is emulated and takes about 6 to
  8 minutes.
- It needs `qemu-system-x86_64` with OVMF, or `qemu-system-aarch64` with AAVMF (`qemu-efi-aarch64`).

Each run writes `serial.log`, `qemu.log` and a monitor socket to `/srv/noctyx/vmtest/run-<stamp>/`.
`--keep` leaves the VM running after the verdict (stop it yourself). While the VM runs, you can capture its
screen:

```bash
python3 src/noctyx/restore/vm-screenshot.py /srv/noctyx/vmtest/run-<stamp>/mon.sock shot.png [ret down …]
```

The optional arguments are QEMU `sendkey` names, which are pressed before the screenshot. The script needs
Pillow.

See also: [Backups](backups.md) · [Troubleshooting](troubleshooting.md)
