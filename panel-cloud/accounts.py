"""A person's saved Speexx logins.

Every read is scoped by `user_id` in the SQL, not by a check afterwards. That is
the whole IDOR story: `WHERE id = ?` looks fine and leaks another person's
account, while `WHERE id = ? AND user_id = ?` cannot. `get_owned()` is the only
way to fetch a single row and it always takes the owner.
"""

from __future__ import annotations

import db
import vault

MAX_LABEL = 60
MAX_ACCOUNTS_PER_USER = 20

#: How long a saved account is refused after a fatal login, per reason.
COOLDOWN_SECONDS = {"bad_credentials": 15 * 60, "locked": 3600}


class AccountError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def cooldown_for(email: str):
    """Global cooldown keyed on the Speexx email, not the owner.

    Shared deliberately: one bad password on an address should stop any job for
    that address, whoever queued it.
    """
    row = db.one("SELECT until, reason FROM cooldowns WHERE email = ?", (email,))
    if row and row["until"] > db.now():
        return row
    return None


def note_cooldown(email: str, reason: str) -> None:
    seconds = COOLDOWN_SECONDS.get(reason, 900)
    db.execute(
        """INSERT INTO cooldowns (email, until, reason, at) VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET until = excluded.until,
             reason = excluded.reason, at = excluded.at""",
        (email, db.now() + seconds, reason, db.now()),
    )
    db.execute(
        """UPDATE speexx_accounts SET status = ?, cooldown_until = ?
            WHERE email = ? COLLATE NOCASE""",
        (reason, db.now() + seconds, email),
    )


def add(user_id: int, email: str, password: str, label: str = "") -> int:
    email = (email or "").strip().lower()
    label = (label or "").strip()[:MAX_LABEL] or email.split("@")[0]
    if not email or "@" not in email or len(email) > 254:
        raise AccountError("That does not look like an email address.", "bad_email")
    if not password:
        raise AccountError("A password is required.", "no_password")

    count = db.one("SELECT COUNT(*) AS n FROM speexx_accounts WHERE user_id = ?",
                   (int(user_id),))["n"]
    if count >= MAX_ACCOUNTS_PER_USER:
        raise AccountError("You have reached %d accounts." % MAX_ACCOUNTS_PER_USER, "limit")

    # One Speexx login belongs to one person. Allowing it to be saved twice
    # would let two accounts on this panel fight over the same portal session,
    # which invalidates both -- exactly what jobs_one_active_per_account
    # refuses at run time, checked here before it ever gets that far.
    if db.one("SELECT 1 AS x FROM speexx_accounts WHERE email = ?", (email,)):
        raise AccountError("That account has already been added here.", "duplicate")

    blob = vault.encrypt(password, int(user_id), email)
    try:
        cur = db.execute(
            """INSERT INTO speexx_accounts (user_id, label, email, secret, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (int(user_id), label, email, blob, db.now()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise AccountError("You have already added that account.", "duplicate")
        raise
    # Only the address is logged. Never the secret, never the ciphertext.
    db.audit(user_id, "account_added", email)
    return cur.lastrowid


def list_for(user_id: int) -> list[dict]:
    rows = db.query(
        """SELECT id, label, email, status, cooldown_until, created_at,
                  last_used_at, last_job_at
             FROM speexx_accounts WHERE user_id = ? ORDER BY id""",
        (int(user_id),),
    )
    out = []
    for r in rows:
        blocked = r["cooldown_until"] and r["cooldown_until"] > db.now()
        out.append({
            "id": r["id"],
            "label": r["label"],
            "email": r["email"],
            "status": "blocked" if blocked else r["status"],
            "blocked": bool(blocked),
            "created_at": r["created_at"] * 1000,
            "last_used_at": r["last_used_at"] * 1000 if r["last_used_at"] else None,
            "last_job_at": r["last_job_at"] * 1000 if r["last_job_at"] else None,
        })
    return out


def get_owned(user_id: int, account_id: int):
    """The only single-row fetch. Scoped by owner — never widen this."""
    return db.one(
        "SELECT * FROM speexx_accounts WHERE id = ? AND user_id = ?",
        (int(account_id), int(user_id)),
    )


def remove(user_id: int, account_id: int) -> bool:
    if not get_owned(user_id, account_id):
        return False
    db.execute("DELETE FROM speexx_accounts WHERE id = ? AND user_id = ?",
               (int(account_id), int(user_id)))
    db.audit(user_id, "account_removed", str(account_id))
    return True


def touch(user_id: int, account_id: int, job_started: bool = False) -> None:
    if job_started:
        db.execute(
            "UPDATE speexx_accounts SET last_job_at = ? WHERE id = ? AND user_id = ?",
            (db.now(), int(account_id), int(user_id)))
    else:
        db.execute(
            "UPDATE speexx_accounts SET last_used_at = ? WHERE id = ? AND user_id = ?",
            (db.now(), int(account_id), int(user_id)))


def clear_block(user_id: int, account_id: int) -> bool:
    row = get_owned(user_id, account_id)
    if not row:
        return False
    db.execute(
        "UPDATE speexx_accounts SET status = 'ok', cooldown_until = NULL"
        " WHERE id = ? AND user_id = ?", (int(account_id), int(user_id)))
    db.execute("DELETE FROM cooldowns WHERE email = ?", (row["email"],))
    db.audit(user_id, "account_unblocked", row["email"])
    return True


def credentials(user_id: int, account_id: int) -> tuple[str, str]:
    """Decrypt one account the caller owns. Raises if it is not theirs."""
    row = get_owned(user_id, account_id)
    if not row:
        raise AccountError("No such account.", "not_found")
    try:
        return row["email"], vault.decrypt(row["secret"], int(user_id), row["email"])
    except vault.VaultError:
        # Almost always a lost or rotated vault key. The user must re-enter it.
        raise AccountError(
            "Stored credentials cannot be decrypted any more. Remove and re-add the account.",
            "undecryptable")