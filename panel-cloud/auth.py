"""Sessions, CSRF, rate limiting, and request-level defences.

Binding to 127.0.0.1 does NOT make a browser-served app safe: any page open in
the browser can send a request to 127.0.0.1, and a hostile page can point its
own hostname at loopback. So this module enforces:

  * Host validation against an allowlist (DNS-rebinding defence).
  * A CSRF token a cross-origin page cannot read, plus an Origin check on every
    state-changing request. SameSite=Strict alone is not sufficient.
  * Client IP taken from CF-Connecting-IP only when the immediate peer is
    loopback. Behind a tunnel every request otherwise looks like 127.0.0.1,
    which would collapse every visitor into a single rate-limit bucket.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from urllib.parse import urlparse

import db

SESSION_TTL = 8 * 3600
COOKIE_NAME = "panel_session"

# Fixed windows. Generous enough for a human, useless for a search loop.
LIMITS = {
    # Sign-up is the cheapest thing to abuse on a public service: each accepted
    # signup is a potential account plus a Speexx credential submission.
    "signup": (5, 3600),
    # Per IP and per address, so neither a rotating address nor spraying one
    # address from many gets more than a handful of tries.
    "login": (20, 900),
    "login_user": (10, 900),
    "job_start": (20, 3600),
    # Saving a portal account: a scrypt hash and an AES-GCM seal each time.
    "account_add": (10, 3600),
    "account_add_ip": (30, 3600),
    # The six-digit space is a million codes, so this endpoint is the most
    # tightly limited thing in the panel.
    "key_redeem": (5, 3600),
    "key_redeem_ip": (20, 3600),
    "job_start_ip": (30, 3600),
}

DEFAULT_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limited")
        self.retry_after = retry_after


class Denied(Exception):
    """Request refused before it reached a handler."""


# ── Admin password ────────────────────────────────────────────────────────

# n=2**14 with r=8 needs ~16 MiB, inside OpenSSL's default scrypt ceiling.
# 2**15 needs ~32 MiB and fails outright with "memory limit exceeded" unless
# maxmem is raised, which is why this is not 32768.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expect = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(expect),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expect, actual)


# ── Sessions ──────────────────────────────────────────────────────────────






# ── Rate limiting ─────────────────────────────────────────────────────────

def rate_limit(bucket: str, ident: str) -> None:
    """Fixed-window counter. Raises RateLimited carrying a Retry-After value."""
    if bucket not in LIMITS:
        return
    limit, window = LIMITS[bucket]
    start = db.now()
    window_start = start - (start % window)
    db.execute(
        """INSERT INTO rate_limits (bucket, ident, hits, window_start)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(bucket, ident) DO UPDATE SET
             hits = CASE WHEN rate_limits.window_start = excluded.window_start
                         THEN rate_limits.hits + 1 ELSE 1 END,
             window_start = excluded.window_start""",
        (bucket, ident, window_start),
    )
    row = db.one(
        "SELECT hits, window_start FROM rate_limits WHERE bucket = ? AND ident = ?",
        (bucket, ident),
    )
    if row and row["hits"] > limit:
        raise RateLimited(max(1, row["window_start"] + window - start))


def reset_rate_limits() -> None:
    db.execute("DELETE FROM rate_limits")


# ── Host, origin, client address ──────────────────────────────────────────

def allowed_hosts() -> set[str]:
    hosts = set(DEFAULT_HOSTS)
    for item in os.environ.get("SPEEXX_PANEL_ALLOWED_HOSTS", "").split(","):
        item = item.strip().lower()
        if item:
            hosts.add(item)
    return hosts


def host_matches(host: str) -> bool:
    """True if `host` is allowed, honouring `*.suffix` entries.

    A quick tunnel or ngrok hands out a hostname you do not choose, so the
    allowlist has to accept a whole suffix rather than one exact name. Both the
    Host check and the Origin check go through here — when only the Host check
    knew about wildcards, the page loaded and every login POST was refused with
    403 before the password was even compared.
    """
    host = (host or "").strip().lower().rstrip(".")
    allowed = allowed_hosts()
    if host in allowed:
        return True
    return any(
        pattern.startswith("*.") and host.endswith(pattern[1:])
        for pattern in allowed
    )


def host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 literal, keep the brackets
        host = host.split("]", 1)[0] + "]"
    elif ":" in host:
        host = host.split(":", 1)[0]  # drop the port
    return host_matches(host)


def is_loopback(address: str | None) -> bool:
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def client_ip(request) -> str:
    """Real client IP, trusting proxy headers only when the peer is loopback."""
    peer = request.client.host if request.client else None
    if is_loopback(peer):
        for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
            raw = request.headers.get(header)
            if not raw:
                continue
            candidate = raw.split(",")[0].strip()
            if candidate and not is_loopback(candidate):
                return candidate
    return peer or "unknown"


def origin_allowed(request) -> bool:
    """Accept a missing Origin (same-origin navigation, curl) or a listed host.

    Compares the Origin's hostname only. An Origin carries a port, so matching
    the full origin against a port-less allowlist would reject every request on
    a non-default port — including the panel's own. Wildcard entries are
    honoured via host_matches, exactly as the Host check does.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin.strip())
    if parsed.scheme not in ("http", "https"):
        return False
    return host_matches(parsed.hostname or "")


def is_https(request) -> bool:
    """The browser's scheme, so Secure cookies work behind a TLS-terminating proxy."""
    if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https":
        return True
    return request.url.scheme == "https"
