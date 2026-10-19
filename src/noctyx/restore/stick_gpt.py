#!/usr/bin/env python3
"""stick_gpt.py — rewrite the isohybrid GPT of a dd'd Ubuntu live ISO into a
stick-sized table: p1 (ISO9660) and p2 (ESP) kept BYTE-IDENTICAL (labels,
PARTUUIDs, contents unchanged -> firmware and casper see the media they
expect), p3 @STICK_LABEL@ appended in the tail space. Backups relocated to the
real disk end, all CRCs recomputed, protective MBR fixed up.

Usage: stick_gpt.py <iso> <device-or-image>
Run AFTER dd of the ISO to the device start. Writes directly to the device
(run under sudo).
"""
import os, struct, subprocess, sys, uuid, zlib

iso, dev = sys.argv[1:3]

def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF

def guid_bytes_le(s):
    return uuid.UUID(s).bytes_le

def build_header(my_lba, alt_lba, first_u, last_u, disk_guid, ent_lba, ent_cnt, ent_sz,
                 ent_bytes):
    h = bytearray(512)
    h[0:8] = b"EFI PART"
    struct.pack_into("<II", h, 8, 0x00010000, 92)      # revision, header size
    struct.pack_into("<I", h, 16, 0)                    # header CRC (computed below)
    struct.pack_into("<I", h, 20, 0)                    # reserved
    struct.pack_into("<QQ", h, 24, my_lba, alt_lba)
    struct.pack_into("<QQ", h, 40, first_u, last_u)
    h[56:72] = disk_guid
    struct.pack_into("<QII", h, 72, ent_lba, ent_cnt, ent_sz)
    ecrc = crc32(ent_bytes)
    struct.pack_into("<I", h, 88, ecrc)
    crc = crc32(bytes(h[:92]))
    struct.pack_into("<I", h, 16, crc)
    return bytes(h), ecrc

# ── read the ISO's primary GPT ──────────────────────────────────────────
with open(iso, "rb") as f:
    f.seek(512); h = f.read(512)
    sig = h[0:8]
    assert sig == b"EFI PART", f"ISO has no GPT header (sig {sig!r}) — unsupported ISO"
    hsz = struct.unpack("<I", h[12:16])[0]
    my_lba, alt_lba = struct.unpack("<QQ", h[24:40])
    first_u, last_u = struct.unpack("<QQ", h[40:56])
    disk_guid = h[56:72]
    ent_lba, ent_cnt, ent_sz = struct.unpack("<QII", h[72:88])
    f.seek(ent_lba * 512)
    entries = f.read(ent_cnt * ent_sz)
assert ent_sz == 128, f"unexpected entry size {ent_sz}"
print(f"ISO GPT: {ent_cnt} entries @{ent_lba}, usable {first_u}..{last_u}")

# ── stick geometry ───────────────────────────────────────────────────────
try:
    dev_bytes = int(subprocess.run(["blockdev", "--getsize64", dev],
                                   capture_output=True, text=True).stdout)
except (ValueError, FileNotFoundError):
    dev_bytes = os.path.getsize(dev)     # regular file (test image)
if os.path.isdir(dev):
    sys.exit(f"{dev} is a directory")
iso_bytes = os.path.getsize(iso)
if os.path.exists(dev) and dev_bytes < iso_bytes + 34 * 512:
    sys.exit(f"device {dev} ({dev_bytes} B) too small for ISO ({iso_bytes} B)")
dev_sectors = dev_bytes // 512
assert dev_sectors > last_u + 34, f"stick too small (need > {last_u+34} sectors)"
print(f"stick: {dev_bytes} B ({dev_sectors} sectors)")

# ── new entries: ISO's 248-entry array with p3 swapped in at slot 3 ──────
# (ISO ships entries[0]=ISO9660, [1]=Appended2(ESP), [2]=Gap1; Gap1 never
#  holds data — casper skips it — so replacing slot 3 is safe and keeps the
#  entry-array CRC region the same length.)
DATA_GUID = guid_bytes_le("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7")  # MS basic data
p3_start = last_u + 1
if p3_start % 8:
    p3_start += 8 - (p3_start % 8)
p3_end = dev_sectors - 34 - 1               # leave 33 LBAs for backup header+entries
assert p3_end - p3_start > 8192, "no tail space for @STICK_LABEL@"
name = "@STICK_LABEL@".encode("utf-16-le")
e3 = bytearray(128)
e3[0:16] = DATA_GUID
e3[16:32] = uuid.uuid4().bytes_le
# entry array is large (ISO ships 248 slots = 31744 sectors); the BACKUP array
# at the disk end must not be overlapped by p3 -> p3 ends before it.
ent_sectors_pre = (ent_cnt * ent_sz + 511) // 512
alt_hdr_lba = dev_sectors - 1
alt_ent_lba = alt_hdr_lba - ent_sectors_pre
p3_start = last_u + 1
if p3_start % 8:
    p3_start += 8 - (p3_start % 8)
p3_end = alt_ent_lba - 1                      # strictly before the backup array
assert p3_end - p3_start > 8192, "no tail space for @STICK_LABEL@"
struct.pack_into("<QQ", e3, 32, p3_start, p3_end)
e3[56:56 + len(name)] = name
entries_new = bytearray(entries)
entries_new[2 * 128:3 * 128] = bytes(e3)

hdr_backup, ecrc = build_header(alt_hdr_lba, 1, first_u, last_u, disk_guid,
                                alt_ent_lba, ent_cnt, ent_sz, bytes(entries_new))
hdr_primary, _ = build_header(1, alt_hdr_lba, first_u, last_u, disk_guid,
                              ent_lba, ent_cnt, ent_sz, bytes(entries_new))

with open(dev, "r+b") as f:
    # zero the ISO's stale backup-table band (its alt sat at ISO end)
    old_alt = struct.unpack("<Q", h[32:40])[0]          # ISO backup header LBA
    old_be = max(old_alt - ent_sectors_pre, last_u)
    f.seek(old_be * 512)
    f.write(b"\x00" * min((alt_ent_lba - old_be) * 512, 8 << 20))
    f.seek(alt_ent_lba * 512); f.write(bytes(entries_new))
    f.seek(alt_hdr_lba * 512);  f.write(hdr_backup)
    f.seek(ent_lba * 512);      f.write(bytes(entries_new))
    f.seek(512);                f.write(hdr_primary)
    # protective MBR: single 0xEE partition spanning the whole device
    f.seek(446)
    pmbr = bytearray(16)
    pmbr[0] = 0x00; pmbr[4] = 0xEE
    pmbr[8:12] = (1).to_bytes(4, "little")
    pmbr[12:16] = (dev_sectors - 1).to_bytes(4, "little")  # capped at 0xFFFFFFFF by kernel convention
    f.write(bytes(pmbr))
    f.seek(510); f.write(b"\x55\xAA")
    f.flush()

print(f"wrote stick GPT: p1/p2 verbatim, p3 @STICK_LABEL@ {p3_start}..{p3_end} "
      f"({(p3_end-p3_start)*512//1000//1000} MB)")
print("next: partprobe, then mkfs.vfat -F 32 -n @STICK_LABEL@ <dev>3")
