"""Password hashing, unit keys, session tokens and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
UNIT_ID_RE = re.compile(r"^u[a-z2-7]{10}$")
# 43 characters: a derived 32-byte signing key (current). 32 characters: random keys from before #9.
UNIT_SECRET_RE = re.compile(r"^(?:[A-Za-z0-9_-]{43}|[A-Za-z0-9_-]{32})$")
NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# -- unit keys -------------------------------------------------------------
#
# A unit key looks like "u4fz2kq7mbx.<43 url-safe characters>". The part before the dot is the
# public unit ID, the rest is the unit's signing key, derived from FLEET_SECRET_KEY, the unit ID
# and a key version (#9). The server stores no per-unit secret: it derives the key when needed,
# and rotating a key increments the version.

def new_unit_id() -> str:
    return "u" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:10]


def signing_key(secret_key: str, unit_id: str, version: int) -> bytes:
    return hmac.new(secret_key.encode("utf-8"), f"unit-sign:{unit_id}:{version}".encode(), hashlib.sha256).digest()


def unit_key_for(secret_key: str, unit_id: str, version: int) -> str:
    encoded = base64.urlsafe_b64encode(signing_key(secret_key, unit_id, version)).decode("ascii").rstrip("=")
    return f"{unit_id}.{encoded}"


def split_unit_key(key: str) -> tuple[str, str] | None:
    unit_id, _, secret = key.partition(".")
    if not UNIT_ID_RE.match(unit_id) or not UNIT_SECRET_RE.match(secret):
        return None
    return unit_id, secret


def hash_unit_key(secret_key: str, key: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()


def unit_key_matches(secret_key: str, key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_unit_key(secret_key, key), stored_hash)


# -- signed requests (#9) -------------------------------------------------------

def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_signature(key: bytes, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = "\n".join((method.upper(), path, timestamp, nonce, body_hash(body))).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def response_signature(key: bytes, nonce: str, status: int, body: bytes) -> str:
    message = "\n".join((nonce, str(status), body_hash(body))).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class NonceCache:
    """Remembers (unit, nonce) pairs long enough to cover the accepted timestamp window."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 200000) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_store(self, unit_id: str, nonce: str, now: float) -> bool:
        """True if the nonce is new (and is now remembered), False if it was seen before."""
        with self._lock:
            if len(self._seen) > self.max_entries:
                cutoff = now - self.ttl_s
                self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
            key = (unit_id, nonce)
            seen_at = self._seen.get(key)
            if seen_at is not None and now - seen_at < self.ttl_s:
                return False
            self._seen[key] = now
            return True


# -- admin sessions ----------------------------------------------------------

def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginThrottle:
    """Slows down password guessing: after `free` failures from one client, each further
    failure locks that client out for an exponentially growing time (capped)."""

    def __init__(self, free: int = 5, base_s: float = 2.0, max_s: float = 300.0,
                 clock=time.monotonic) -> None:
        self.free = free
        self.base_s = base_s
        self.max_s = max_s
        self.clock = clock
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> float:
        with self._lock:
            _, until = self._state.get(client, (0, 0.0))
        return max(0.0, until - self.clock())

    def failed(self, client: str) -> None:
        with self._lock:
            fails, _ = self._state.get(client, (0, 0.0))
            fails += 1
            until = 0.0
            if fails > self.free:
                until = self.clock() + min(self.max_s, self.base_s * 2 ** (fails - self.free - 1))
            self._state[client] = (fails, until)
            if len(self._state) > 10000:  # forget the oldest clients rather than grow without bound
                for key in list(self._state)[:5000]:
                    del self._state[key]

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._state.pop(client, None)
