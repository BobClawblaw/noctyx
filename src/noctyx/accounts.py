"""Console accounts: local users with scrypt-hashed passwords, three roles, signed session cookies,
and each user's own display settings.

  viewer    sees everything, changes nothing
  operator  also runs jobs: backups, verifies, restores to staging, hygiene fixes, recipe deploys
  admin     also changes configuration: backup policy, schedules, the assistant, console defaults,
            the recipe catalog, reboots

Users live in <etc>/users.json (0600, root); the session key in <etc>/session.key (0600, made on first
use). A session cookie is "<user>.<expiry>.<hmac>": nothing secret in it, and it stops working the
moment its user is deleted or the key is replaced. Passwords never touch a command line or a log.
"""
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import time

from noctyx.paths import P

ROLES = ("viewer", "operator", "admin")
_NAME = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 10
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


def _users_file():
    return pathlib.Path(P["etc"]) / "users.json"


def _key_file():
    return pathlib.Path(P["etc"]) / "session.key"


def _settings_dir():
    return pathlib.Path(P["console_state"]) / "user-settings"


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def users():
    try:
        return json.loads(_users_file().read_text()).get("users") or {}
    except (OSError, ValueError):
        return {}


def _save(us):
    _write_private(_users_file(), json.dumps({"users": us}, indent=1) + "\n")


def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, maxmem=2 ** 26, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${h.hex()}"


def _check_hash(stored, pw):
    try:
        _, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want) // 2, maxmem=2 ** 26)
        return hmac.compare_digest(got.hex(), want)
    except (ValueError, TypeError):
        return False


_DUMMY = None


def verify(name, pw):
    """-> the user's role, or None. An unknown name costs the same scrypt as a known one."""
    global _DUMMY
    u = users().get(name)
    if not u:
        _DUMMY = _DUMMY or hash_password(secrets.token_hex(8))
        _check_hash(_DUMMY, pw)
        return None
    return u["role"] if _check_hash(u["hash"], pw) else None


def check_new(name, role, pw=None):
    errs = []
    if not _NAME.match(name or ""):
        errs.append("name: 2-32 characters, lowercase letters, digits, . _ - (starting with a letter)")
    if role not in ROLES:
        errs.append(f"role: one of {', '.join(ROLES)}")
    if pw is not None and len(pw) < MIN_PASSWORD:
        errs.append(f"password: at least {MIN_PASSWORD} characters")
    return errs


def add(name, role, pw):
    errs = check_new(name, role, pw)
    if name in users():
        errs.append(f"{name} exists (passwd / role change it)")
    if errs:
        raise ValueError("; ".join(errs))
    us = users()
    us[name] = {"hash": hash_password(pw), "role": role, "created": int(time.time())}
    _save(us)


def set_password(name, pw):
    us = users()
    if name not in us:
        raise ValueError(f"no user {name}")
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"password: at least {MIN_PASSWORD} characters")
    us[name]["hash"] = hash_password(pw)
    us[name]["changed"] = int(time.time())
    _save(us)


def set_role(name, role):
    us = users()
    if name not in us or role not in ROLES:
        raise ValueError(f"no user {name}" if name not in us else f"role: one of {', '.join(ROLES)}")
    us[name]["role"] = role
    _save(us)


def delete(name):
    us = users()
    if us.pop(name, None) is None:
        raise ValueError(f"no user {name}")
    _save(us)
    try:
        (_settings_dir() / f"{name}.json").unlink()
    except OSError:
        pass


def _key():
    f = _key_file()
    try:
        k = f.read_text().strip()
        if len(k) >= 64:
            return bytes.fromhex(k)
    except (OSError, ValueError):
        pass
    k = secrets.token_hex(32)
    _write_private(f, k + "\n")
    return bytes.fromhex(k)


def make_session(name, ttl=12 * 3600):
    payload = f"{name}.{int(time.time()) + int(ttl)}"
    return payload + "." + hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def check_session(value):
    """-> (name, role) for a valid, unexpired session of an existing user, else None."""
    try:
        name, exp, sig = (value or "").rsplit(".", 2)
        want = hmac.new(_key(), f"{name}.{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want) or int(exp) < time.time():
            return None
    except (ValueError, TypeError):
        return None
    u = users().get(name)
    return (name, u["role"]) if u else None


def rank(role):
    return ROLES.index(role) if role in ROLES else -1


def get_settings(name):
    try:
        return json.loads((_settings_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return {}


def put_settings(name, s):
    _write_private(_settings_dir() / f"{name}.json", json.dumps(s))
