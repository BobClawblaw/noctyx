"""Console accounts: local users with scrypt-hashed passwords, three roles, signed session cookies,
and each user's own display settings.

  viewer    sees everything, changes nothing
  operator  also runs jobs: backups, verifies, restores to staging, hygiene fixes, recipe deploys
  admin     also changes configuration: backup policy, schedules, the assistant, console defaults,
            the recipe catalog, reboots

API tokens: `noctyx user token --name N` prints "nx_<name>_<secret>" once; only its sha256 is stored. A
script sends it as "Authorization: Bearer ..." and acts with that user's role, read live.

Users live in <etc>/users.json (0600, root); the session key in <etc>/session.key (0600, made on first
use). A session cookie is "<user>.<expiry>.<hmac>": nothing secret in it, and it stops working the
moment its user is deleted or the key is replaced. Passwords never touch a command line or a log.
"""
import contextlib
import fcntl
import functools
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import tempfile
import threading
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
    """0600, atomically, through a temp file of its own (two writers never share one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked():
    """One change to users.json at a time: across the console's threads and the CLI's processes."""
    f = _users_file().with_name(_users_file().name + ".lock")
    f.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(f, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _mutates(fn):
    """Read, change and save users.json as one step."""
    @functools.wraps(fn)
    def run(*a, **k):
        with _locked():
            return fn(*a, **k)
    return run


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


@_mutates
def add(name, role, pw):
    errs = check_new(name, role, pw)
    if name in users():
        errs.append(f"{name} exists (passwd / role change it)")
    if errs:
        raise ValueError("; ".join(errs))
    us = users()
    us[name] = {"hash": hash_password(pw), "role": role, "created": int(time.time())}
    _save(us)


@_mutates
def set_password(name, pw):
    us = users()
    if name not in us:
        raise ValueError(f"no user {name}")
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"password: at least {MIN_PASSWORD} characters")
    us[name]["hash"] = hash_password(pw)
    us[name]["changed"] = int(time.time())
    _save(us)


@_mutates
def set_role(name, role):
    us = users()
    if name not in us or role not in ROLES:
        raise ValueError(f"no user {name}" if name not in us else f"role: one of {', '.join(ROLES)}")
    us[name]["role"] = role
    _save(us)


@_mutates
def delete(name):
    us = users()
    if us.pop(name, None) is None:
        raise ValueError(f"no user {name}")
    _save(us)
    try:
        (_settings_dir() / f"{name}.json").unlink()
    except OSError:
        pass


@_mutates
def make_token(name):
    """A new API token for name (replacing any earlier one); the caller shows it once."""
    us = users()
    if name not in us:
        raise ValueError(f"no user {name}")
    tok = f"nx_{name}_{secrets.token_hex(24)}"
    us[name]["token_sha256"] = hashlib.sha256(tok.encode()).hexdigest()
    us[name]["token_created"] = int(time.time())
    _save(us)
    return tok


@_mutates
def revoke_token(name):
    us = users()
    if name not in us:
        raise ValueError(f"no user {name}")
    had = us[name].pop("token_sha256", None) is not None
    us[name].pop("token_created", None)
    _save(us)
    return had


def check_token(tok):
    """-> (name, role) for a valid API token, else None."""
    m = re.match(r"^nx_([a-z][a-z0-9._-]{1,31})_([0-9a-f]{48})$", tok or "")
    if not m:
        return None
    u = users().get(m.group(1))
    want = (u or {}).get("token_sha256")
    if not want or not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), want):
        return None
    return (m.group(1), u["role"])


def sign(text, ttl):
    """A short-lived signed ticket for text (download links): "<expiry>.<hmac>"."""
    exp = int(time.time()) + int(ttl)
    return f"{exp}." + hmac.new(_key(), f"{exp}|{text}".encode(), hashlib.sha256).hexdigest()


def check_signed(text, ticket):
    try:
        exp, sig = (ticket or "").split(".", 1)
        want = hmac.new(_key(), f"{exp}|{text}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want) and int(exp) >= time.time()
    except (ValueError, TypeError):
        return False


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
