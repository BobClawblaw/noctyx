#!/usr/bin/env python3
"""vm-screenshot.py <monitor.sock> <out.png> [key ...]
Send optional `sendkey` presses (e.g. ret, down, e) to a QEMU monitor, then
screendump the guest framebuffer and convert it to PNG (needs PIL)."""
import socket, sys, time, os
from PIL import Image

sock, out, keys = sys.argv[1], sys.argv[2], sys.argv[3:]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(20)
s.connect(sock)

def cmd(c):
    s.sendall((c + "\n").encode())
    time.sleep(0.5)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if buf.rstrip().endswith(b"(qemu)"):
                break
    except socket.timeout:
        pass
    return buf.decode(errors="replace")

cmd("")  # banner
for k in keys:
    cmd(f"sendkey {k}")
    time.sleep(0.4)
if keys:
    time.sleep(1.5)
ppm = out + ".ppm"
cmd(f"screendump {ppm}")
time.sleep(1)
Image.open(ppm).save(out)
os.remove(ppm)
print(out, Image.open(out).size)
