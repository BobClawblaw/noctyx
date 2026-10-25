"""SSH logins from a machine's own log: journal and macOS lines, each kind of attempt, the key's name from
authorized_keys, the head's key marked, one row per refused connection."""
from noctyx import sshlog

HEAD = "SHA256:headheadheadheadheadheadheadheadheadheadhea"
OTHER = "SHA256:laptoplaptoplaptoplaptoplaptoplaptoplaptopl"

LOG = f"""@@KEY 256 {HEAD} noctyx@head (ED25519)
@@KEY 256 {OTHER} ops-laptop (ED25519)
1792895571.079109 gpu-01 sshd[1]: Accepted publickey for ops from 192.0.2.10 port 60984 ssh2: ED25519 {HEAD}
1792895572.000000 gpu-01 sshd-session[2]: Accepted publickey for ops from 2001:db8::5 port 51000 ssh2: ED25519 {OTHER}
1792895573.000000 gpu-01 sshd[3]: Accepted password for ops from 192.0.2.20 port 40000 ssh2
1792895574.000000 gpu-01 sshd[4]: Invalid user admin from 198.51.100.7 port 4242
1792895574.100000 gpu-01 sshd[4]: Failed password for invalid user admin from 198.51.100.7 port 4242 ssh2
1792895575.000000 gpu-01 sshd[5]: Invalid user  from 198.51.100.8 port 1111
1792895576.000000 gpu-01 sshd[6]: Failed password for root from 198.51.100.7 port 4243 ssh2
1792895577.000000 gpu-01 sshd[7]: Connection closed by authenticating user ops 192.0.2.30 port 2222 [preauth]
1792895578.000000 gpu-01 sshd[8]: error: maximum authentication attempts exceeded for invalid user test from 198.51.100.9 port 3333 ssh2 [preauth]
1792895579.000000 gpu-01 sshd[9]: Disconnected from user ops 192.0.2.10 port 60984
"""


def test_each_kind_of_attempt_is_one_row():
    ev = sshlog.parse(LOG, HEAD)
    got = [(e["result"], e["user"], e["addr"]) for e in ev]
    assert got == [("accepted", "ops", "192.0.2.10"), ("accepted", "ops", "2001:db8::5"), ("accepted", "ops", "192.0.2.20"),
                   ("failed", "admin", "198.51.100.7"),           # its "Invalid user" twin dropped
                   ("invalid", "", "198.51.100.8"),
                   ("failed", "root", "198.51.100.7"),
                   ("gave up", "ops", "192.0.2.30"),
                   ("too many", "test", "198.51.100.9")]
    assert ev[3]["invalid_user"] and ev[7]["invalid_user"] and not ev[5].get("invalid_user")


def test_keys_are_named_and_the_heads_marked():
    ev = sshlog.parse(LOG, HEAD)
    assert ev[0]["head_key"] and ev[0]["key_name"] == "noctyx@head"
    assert not ev[1].get("head_key") and ev[1]["key_name"] == "ops-laptop" and ev[1]["key"] == f"ED25519 {OTHER}"
    assert "key" not in ev[2] and ev[2]["method"] == "password"


def test_a_macs_log_lines():
    mac = ("2026-10-24 21:32:47.972279-0500  localhost sshd-session[8638]: Accepted publickey for ops from 192.0.2.10 "
           f"port 45624 ssh2: ED25519 {OTHER}\n")
    (e,) = sshlog.parse(mac)
    assert e["result"] == "accepted" and e["port"] == 45624
    assert abs(e["ts"] - 1792895567.972) < 0.01                         # 21:32:47.97 -05:00 = 02:32:47.97Z


def test_the_summary_counts_by_address():
    s = sshlog.summary(sshlog.parse(LOG, HEAD))
    assert (s["accepted"], s["refused"], s["head_key"]) == (3, 5, 1)
    top = s["sources"][0]
    assert top["addr"] == "198.51.100.7" and top["refused"] == 2 and top["users"] == ["admin", "root"]


def test_the_remote_command_reads_the_right_log():
    assert "journalctl" in sshlog.remote_cmd(7) and "--since -7d" in sshlog.remote_cmd(7)
    mac = sshlog.remote_cmd(30, macos=True)
    assert "log show" in mac and "--last 30d" in mac and "/Users/*/.ssh" in mac
