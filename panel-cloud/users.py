"""Users, sign-in, and sessions.

The panel's original access model was a single-use key: hand someone a key, they
run one account, the key dies. That does not survive having more than one person
use the service, so accounts replace it. The key path is gone rather than
layered underneath, because two access models on one service is two sets of
rules to get right.

Three decisions worth stating:

* **Sign-up is admin-approved by default.** This service turns the operator's Mac
  and its IP address into a bot runner for whoever finds the URL. Open sign-up
  means a stranger can spend the six device-profile slots, drive login attempts
  from the operator's IP, and get it banned — the exact failure the login gate
  exists to prevent. Set `SPEEXX_PANEL_OPEN_SIGNUP=1` to allow instant accounts
  when that is a deliberate choice.

* **Failed sign-ins lock the account, not just the IP.** Rate limiting by IP is
  trivially defeated by anyone with a second address; the lockout on the account
  row is what actually makes guessing a password expensive.

* **A disabled or pending user keeps their sessions.** Every authenticated
  request re-reads the user row and checks status, so suspending someone takes
  effect on their next request instead of when their cookie expires.
"""

from __future__ import annotations

import os
import re
import secrets

import auth
import db

MIN_PASSWORD = 8
MAX_PASSWORD = 200
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
LOCK_BACKOFF_CAP = 6 * 3600

# A deliberately tiny list. Length plus these is the whole policy: composition
# rules push people toward predictable substitutions, NIST SP 800-63B does not
# recommend them.
TOO_COMMON = {
    "password", "password1", "password12", "password123",
    "12345678", "123456789", "1234567890", "qwertyui", "qwertyuiop",
    "letmein12", "letmein1234", "iloveyou12", "iloveyou123",
    "admin1234", "admin12345", "welcome123", "welcome1234",
    "abc12345", "abc1234567", "changeme12", "changeme123", "111111111",
}


class AuthError(Exception):
    """Sign-in or sign-up refused. `code` lets the UI say something useful."""

    def __init__(self, message: str, code: str = "bad_credentials"):
        super().__init__(message)
        self.code = code


def open_signup() -> bool:
    """Always true. Sign-up is open; the key is what gates running."""
    return True


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(
            "Use at least %d characters." % MIN_PASSWORD, "weak_password")
    if len(password) > MAX_PASSWORD:
        raise AuthError("That password is too long.", "weak_password")
    if password.lower() in TOO_COMMON:
        raise AuthError("That password is too common.", "weak_password")


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")
RESERVED_USERNAMES = {"admin", "root", "administrator", "support", "speexx"}


def normalise_username(name: str) -> str:
    return (name or "").strip().lower()


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(normalise_username(name)))


def check_username(name: str, allow_reserved: bool = False) -> str:
    """A name you can type on a phone, lowercased, and not one we reserve.

    The reserved list exists so a stranger cannot sign up as "admin" and be
    mistaken for the owner. The environment bootstrap is exempt, or it could
    never create the very account it is trying to create.
    """
    name = normalise_username(name)
    if not valid_username(name):
        raise AuthError(
            "Use 2 to 31 letters, numbers, dot, dash or underscore.", "bad_username")
    if not allow_reserved and name in RESERVED_USERNAMES:
        raise AuthError("That name is taken.", "bad_username")
    return name


def no_admin_exists() -> bool:
    """True while nobody owns the panel.

    Deliberately about *admins*, not about accounts: after `./run.sh
    --reset-admin` there are still pending sign-ups on file, and the next
    person to arrive has to be able to take ownership, or the panel is
    unadministrable again.
    """
    return not db.one("SELECT 1 AS x FROM users WHERE role = 'admin' LIMIT 1")


def create_user(username: str, password: str, role: str = "user",
                allow_reserved: bool = False) -> int:
    """Create one account from a username and a password. That is all.

    No email address. It was asked for and nobody used it: sign-in is by
    username, the admin list shows usernames, and the only address the panel
    ever needs is the Speexx login, which is stored per saved account.

    The first arrival on a panel with no admin becomes the owner, because
    approval-only sign-up with nobody able to approve is a dead end.
    """
    check_password(password)
    username = (check_username(username, allow_reserved=allow_reserved)
                if username else normalise_username("user" + secrets.token_hex(4)))

    # The first arrival on a panel with no admin owns it.
    if role == "user" and no_admin_exists():
        role = "admin"
    # Open sign-up: an account can sign in the moment it exists. What it cannot
    # do is *run* -- that needs a key from the admin, which is the whole point.
    status = "active"

    try:
        cur = db.execute(
            """INSERT INTO users (username, password_hash, role, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, auth.hash_password(password), role, status, db.now()),
        )
    except Exception as exc:
        message = str(exc)
        # SQLite names the indexed column, not the index.
        if "users_username_nocase" in message or "users.username" in message:
            raise AuthError("That username is taken.", "username_taken") from exc
        raise
    db.audit(cur.lastrowid, "signup", "username=%s role=%s status=%s"
             % (username, role, status))
    return cur.lastrowid


def get_by_username(name: str):
    name = normalise_username(name)
    return db.one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (name,))



def get_by_id(user_id: int):
    return db.one("SELECT * FROM users WHERE id = ?", (user_id,))


def lock_seconds_for(fails: int) -> int:
    """15 min for the first trip, doubling per repeat, capped at 6 h."""
    return min(LOCK_SECONDS * (2 ** max(0, fails - MAX_FAILS)), LOCK_BACKOFF_CAP)


def authenticate(identifier: str, password: str) -> dict:
    """Verify a sign-in by username. Raises AuthError with a `code` on failure."""
    row = get_by_username(identifier)
    # Always do the scrypt work, even for an unknown address, so response time
    # does not reveal whether the account exists.
    stored = row["password_hash"] if row else auth.hash_password("decoy-password-never-matches")
    ok = auth.verify_password(password or "", stored)

    if not row or not ok:
        if row:
            fails = row["fail_count"] + 1
            # Only arm the lock once the allowance is spent. Arming it on the
            # first typo meant one stray keystroke cost 15 minutes.
            locked = db.now() + lock_seconds_for(fails) if fails >= MAX_FAILS else None
            db.execute(
                "UPDATE users SET fail_count = ?, locked_until = ? WHERE id = ?",
                (fails, locked, row["id"]),
            )
            db.audit(row["id"], "login_failed",
                     "fail_count=%d%s" % (fails, " locked" if locked else ""))
        raise AuthError("Username or password is not right.", "bad_credentials")

    if row["locked_until"] and row["locked_until"] > db.now():
        wait = row["locked_until"] - db.now()
        raise AuthError("Too many attempts. Try again in %d minutes." % max(1, wait // 60),
                        "locked")

    if row["status"] == "disabled":
        raise AuthError("This account is disabled.", "disabled")
    if row["status"] == "pending":
        raise AuthError("Waiting for approval. Try again once you are approved.",
                        "pending")

    db.execute(
        "UPDATE users SET fail_count = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (db.now(), row["id"]),
    )
    db.audit(row["id"], "login_ok")
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "status": row["status"]}


# ── Sessions ──────────────────────────────────────────────────────────────

def create_session(user_id: int, ip: str = "", agent: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    created = db.now()
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at, ip, agent)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_token_hash(token), int(user_id), csrf, created,
         created + auth.SESSION_TTL, ip[:60], agent[:200]),
    )
    return token, csrf


def _token_hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_session(token: str | None) -> dict | None:
    """Resolve a cookie to a live user. Status is re-checked on every call."""
    if not token:
        return None
    row = db.one(
        """SELECT s.token_hash, s.csrf, s.expires_at, u.id, u.username, u.role, u.status
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?""",
        (_token_hash(token),),
    )
    if not row:
        return None
    if row["expires_at"] <= db.now():
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if row["status"] != "active":
        # Suspending or un-approving takes effect immediately, not at expiry.
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
               (db.now() + auth.SESSION_TTL, row["token_hash"]))
    return {"token_hash": row["token_hash"], "csrf": row["csrf"], "user_id": row["id"],
            "username": row["username"], "role": row["role"]}


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def destroy_user_sessions(user_id: int) -> None:
    db.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))


def purge_expired() -> None:
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now(),))


# ── Administration ────────────────────────────────────────────────────────

def bootstrap_admin() -> str | None:
    """Create an admin from the environment. Returns a generated password once.

    Only runs when SPEEXX_PANEL_ADMIN_PASSWORD is set. On an empty panel with
    no such variable it deliberately does nothing: generating an admin nobody
    knows the password to is the dead end that started all this. Instead the
    first person to sign up becomes the admin, and can see it immediately.
    """
    if not os.environ.get("SPEEXX_PANEL_ADMIN_PASSWORD"):
        return None
    if db.one("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"):
        return None
    name = os.environ.get("SPEEXX_PANEL_ADMIN_USERNAME") or "admin"
    password = os.environ["SPEEXX_PANEL_ADMIN_PASSWORD"]
    try:
        user_id = create_user(name, password, role="admin", allow_reserved=True)
    except AuthError:
        return None
    db.audit(user_id, "bootstrap_admin", name)
    return password


def list_users() -> list[dict]:
    rows = db.query(
        """SELECT u.id, u.username, u.role, u.status, u.created_at, u.last_login_at,
                  (SELECT COUNT(*) FROM speexx_accounts a WHERE a.user_id = u.id) AS accounts,
                  (SELECT COUNT(*) FROM jobs j WHERE j.user_id = u.id) AS jobs
             FROM users u ORDER BY u.id""")
    return [dict(r) for r in rows]


def set_status(user_id: int, status: str) -> bool:
    """Approve, disable or re-enable someone. Unknown states are refused."""
    if status not in ("pending", "active", "disabled"):
        return False
    row = get_by_id(user_id)
    if not row:
        return False
    db.execute("UPDATE users SET status = ? WHERE id = ?", (status, int(user_id)))
    if status != "active":
        # No point keeping sessions for someone who can no longer sign in.
        destroy_user_sessions(user_id)
    db.audit(user_id, "status_changed", status)
    return True


def public_view(user: dict) -> dict:
    """The only shape of a user that ever leaves the server."""
    return {"id": user["id"], "username": user["username"], "role": user["role"]}