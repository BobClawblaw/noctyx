#!/usr/bin/env python3
"""dual_gpt.py — write a protective 4-partition GPT to a raw device whose
LBA 0 currently holds an ext4 superblock (the staged p1 image was dd'd there).

Same pure-stdlib GPT writer technique as stick_gpt.py (proven 2026-09-26).

  p1 @LIVE_LABEL@  (linux  0FC63DAF...)  2048 .. 2048+p1_sectors-1
  p2 ESP          (esp    C12A7328...)  2048-aligned after p1, esp_mb size
  p3 @STICK_LABEL@   (msdn   EBD0A0A2...)  rest, minus p4 and the 33-LBA backup reserve
  p4 CIDATA       (msdn   EBD0A0A2...)  8 MiB at the end: cloud-init NoCloud seed that
                  starts the recovery menu at boot (numbered 4, placed last, so p1-p3
                  keep their numbers and offsets)

Usage: dual_gpt.py /dev/sdX <p1_sectors> [esp_mb=64]
"""
import binascii, os, struct, sys, uuid

DEV, P1_SECTORS = sys.argv[1], int(sys.argv[2])
ESP_MB = int(sys.argv[3]) if len(sys.argv) > 3 else 64
LBA = 512
START_P1 = 2048
P1_END = START_P1 + P1_SECTORS                       # exclusive
P2_START = (P1_END + 2047) // 2048 * 2048
P2_SECTORS = ESP_MB * 1024 * 1024 // LBA
P2_END = P2_START + P2_SECTORS
P3_START = (P2_END + 2047) // 2048 * 2048

fd = os.open(DEV, os.O_RDWR)
disk_sectors = os.lseek(fd, 0, 2) // LBA
P4_END = disk_sectors - 34                           # backup hdr + 16KiB arr + slack
P4_START = (P4_END - 16384) // 2048 * 2048           # 8 MiB, 1 MiB-aligned
P3_END = P4_START
if P3_END <= P3_START + 10240:
    sys.exit(f"FATAL: no room for p3 (p3_start={P3_START} p3_end={P3_END})")

ENT_SZ, ENT_CNT = 128, 128
ARR_BYTES = ENT_SZ * ENT_CNT
ARR_SECTORS = ARR_BYTES // LBA

TYPES = {
    "linux": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
    "esp":   "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
    "msdn":  "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7",
}
NS = uuid.uuid5(uuid.NAMESPACE_DNS, "noctyx-dual-stick")

def entry(ptype, label, start, end_excl, idx):
    e = bytearray(128)
    e[0:16] = uuid.UUID(TYPES[ptype]).bytes_le
    e[16:32] = uuid.uuid5(NS, label).bytes_le
    struct.pack_into("<QQ", e, 32, start, end_excl - 1)
    name = label.encode("utf-16-le")[:72]
    e[56:56 + len(name)] = name                       # name field = bytes 56..127
    return bytes(e)

guid_disk = str(uuid.uuid5(NS, "disk"))

def header(my_lba, alt_lba, arr_lba, arr_crc):
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    struct.pack_into("<I", hdr, 8, 0x00010000)
    struct.pack_into("<I", hdr, 12, 92)
    struct.pack_into("<Q", hdr, 24, my_lba)
    struct.pack_into("<Q", hdr, 32, alt_lba)
    struct.pack_into("<Q", hdr, 40, 2 + ARR_SECTORS)          # first usable (after the primary array)
    struct.pack_into("<Q", hdr, 48, disk_sectors - 2 - ARR_SECTORS)  # last usable (before the backup array)
    hdr[56:72] = uuid.UUID(guid_disk).bytes_le
    struct.pack_into("<Q", hdr, 72, arr_lba)
    struct.pack_into("<I", hdr, 80, ENT_CNT)
    struct.pack_into("<I", hdr, 84, ENT_SZ)
    struct.pack_into("<I", hdr, 88, arr_crc)
    hdr[16:20] = struct.pack("<I", binascii.crc32(bytes(hdr)) & 0xFFFFFFFF)
    return bytes(hdr).ljust(LBA, b"\0")

entries = b"".join([
    entry("linux", "@LIVE_LABEL@", START_P1, P1_END, 1),
    entry("esp",   "ESP",         P2_START, P2_END, 2),
    entry("msdn",  "@STICK_LABEL@",  P3_START, P3_END, 3),
    entry("msdn",  "CIDATA",      P4_START, P4_END, 4),
]).ljust(ARR_BYTES, b"\0")
arr_crc = binascii.crc32(entries) & 0xFFFFFFFF
last = disk_sectors - 1
backup_arr = last - ARR_SECTORS
# protective MBR: one 0xEE partition covering the disk + boot signature, else nothing
# (sfdisk, the kernel, most firmware) recognises the GPT at all
pmbr = bytearray(LBA)
struct.pack_into("<BBBBBBBBII", pmbr, 446, 0x00, 0x00, 0x02, 0x00, 0xEE, 0xFF, 0xFF, 0xFF, 1, min(last, 0xFFFFFFFF))
pmbr[510:512] = b"\x55\xAA"
os.pwrite(fd, bytes(pmbr), 0)
os.pwrite(fd, header(1, last, 2, arr_crc), LBA * 1)             # primary header @LBA1
os.pwrite(fd, entries, LBA * 2)                                  # primary array  @LBA2..33
os.pwrite(fd, entries, LBA * backup_arr)                         # backup array   @last-32..last-1
os.pwrite(fd, header(last, 1, backup_arr, arr_crc), LBA * last)  # backup header  @last
os.fsync(fd)
os.close(fd)
print(f"GPT: p1 2048-{P1_END-1}  p2 {P2_START}-{P2_END-1}  p3 {P3_START}-{P3_END-1}  p4 {P4_START}-{P4_END-1}  disk {disk_sectors} LBAs")
