"""Run keys: six digits, minted by an admin for one named person.

Anyone may sign up and sign in. A key is what lets somebody *run* a level, and
only the admin can mint one. **One key buys one level.**

Redeeming a code does not open a door; it buys a single level. The credit is spent
the instant a run starts, so stopping one early does not give it back -- that was
an explicit decision, and the alternative (only a completed level counts) lets
somebody explore the portal for free by stopping repeatedly.

    unused  --redeem-->  redeemed  --run-->  spent
       ^                    |                    |
       +----- revoke -------+---- revoke --------+

`revoked` is the admin's kill switch, and it works on a credit somebody is already
holding as well as on a code they have not yet spent.

**Six digits is a million combinations, and that is not many.** Three things make
it survivable, and all three are load-bearing:

- The code is **never stored**. Only a scrypt hash goes in the database, so a
  copied `panel.db` yields no codes to try.
- Redeeming is **rate limited** hard, per person and per address
  (`key_redeem`, `key_redeem_ip`). Walking a million hashes is not the risk;
  walking them through an endpoint is.
- A key is **bound to one username** when minted, so a redeemed key is not a
  transferable licence and the admin can see exactly who holds what.

A spent key stays spent, including when the account it was for turns out to be
blocked. Refunding it would make the key an unlimited password-testing oracle.
"""

from __future__ import annotations

import secrets

import auth
import db

DIGITS = 6
CODE_LENGTH = DIGITS
# Per person, per hour. Someone fat-fingering gets a few tries; someone
# enumerating gets stopped long before a million.
REDEEM_LIMIT_PER_USER = (5, 3600)
REDEEM_LIMIT_PER_IP = (20, 3600)

# How many unspent keys a person may hold, counting both unclaimed codes and
# redeemed credits. Stops someone hoarding twenty levels with twenty codes.
MAX_HELD_PER_USER = 5


class KeyError_(Exception):
    """A key request refused. `code` lets the UI say something useful."""

    def __init__(self, message: str, code: str = "bad_key"):
        super().__init__(message)
        self.code = code


def _random_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(DIGITS))


def _hash(code: str) -> str:
    return auth.hash_password(code.strip())


def mint(user_id: int, note: str = "") -> str:
    """Mint an unused key for one person and return it, once, in the clear.

    The caller is an admin. Only the hash is persisted; this return value is the
    one and only time the code exists in readable form.
    """
    row = db.one("SELECT id FROM users WHERE id = ?", (int(user_id),))
    if not row:
        raise KeyError_("No such person.", "not_found")

    if held(user_id) >= MAX_HELD_PER_USER:
        raise KeyError_("They already hold %d keys." % MAX_HELD_PER_USER, "too_many")

    for _ in range(24):
        code = _random_code()
        # Check first so the common case never burns an scrypt hash, then let the
        # unique-ish check below be belt and braces.
        if not _find_unused(code):
            break
    else:                                   # pragma: no cover - 24 collisions
        raise KeyError_("Could not find a free code, try again.", "collision")

    db.execute(
        "INSERT INTO keys (user_id, code_hash, status, note, created_at)"
        " VALUES (?, ?, 'unused', ?, ?)",
        (int(user_id), _hash(code), (note or "").strip()[:80], db.now()),
    )
    db.audit(user_id, "key_minted", "note=%s" % ((note or "").strip()[:80]))
    return code


def _find_unused(code: str) -> bool:
    """True when some unused key already hashes from this code."""
    digest = _hash(code)
    for row in db.query(
            "SELECT code_hash FROM keys WHERE status = 'unused'"):
        if auth.verify_password(code, row["code_hash"]):
            return True
    return False


def held(user_id: int) -> int:
    """Keys this person could spend right now: unclaimed codes plus credits."""
    return db.one(
        "SELECT COUNT(*) AS n FROM keys WHERE user_id = ?"
        " AND status IN ('unused', 'redeemed')", (int(user_id),))["n"]


def redeem(user_id: int, code: str) -> dict:
    """Turn a code into one level's worth of credit for one person.

    Atomic: the update that claims the code carries `WHERE status = 'unused'`, so
    two simultaneous attempts cannot both win.
    """
    code = (code or "").strip()
    if len(code) != CODE_LENGTH or not code.isdigit():
        # Uniform refusal: do not confirm which shape was wrong.
        raise KeyError_("That key is not right.", "bad_key")

    # No cap check here on purpose. Redeeming converts an unclaimed code into a
    # credit and so does not change `held`, so a cap here would refuse somebody
    # the last key they were legitimately given. `mint` is where the cap lives,
    # because that is the only thing that increases `held`.

    rows = db.query(
        "SELECT id, code_hash, user_id FROM keys WHERE status = 'unused'")
    for row in rows:
        if auth.verify_password(code, row["code_hash"]):
            if row["user_id"] != int(user_id):
                # The code is real but was minted for someone else. Say nothing
                # more than "not right", or this becomes a way to test whether a
                # guessed code exists.
                raise KeyError_("That key is not right.", "bad_key")
            cur = db.execute(
                "UPDATE keys SET status = 'redeemed', redeemed_at = ?"
                " WHERE id = ? AND status = 'unused'",
                (db.now(), row["id"]))
            if cur.rowcount != 1:
                raise KeyError_("That key is not right.", "bad_key")
            db.audit(user_id, "key_redeemed", "key=%d" % row["id"])
            return {"id": row["id"], "levels": held(user_id)}
    raise KeyError_("That key is not right.", "bad_key")


def revoke(user_id: int, key_id: int) -> bool:
    """Take a key back: an unclaimed code or a credit not yet spent.

    A spent one stays spent. There is no refund path on purpose -- a refund would
    make the key a way to run without having been given one.
    """
    cur = db.execute(
        "UPDATE keys SET status = 'revoked' WHERE id = ? AND user_id = ?"
        " AND status IN ('unused', 'redeemed')", (int(key_id), int(user_id)))
    if cur.rowcount:
        db.audit(user_id, "key_revoked", "key=%d" % key_id)
        return True
    return False


def has_access(user_id: int) -> bool:
    """The gate. True while the person holds at least one unspent level."""
    return credits(user_id) > 0


def credits(user_id: int) -> int:
    """Levels this person can still run."""
    return db.one(
        "SELECT COUNT(*) AS n FROM keys WHERE user_id = ? AND status = 'redeemed'",
        (int(user_id),))["n"]


def consume(user_id: int) -> int:
    """Spend one level. Returns the key row id.

    Atomic: the UPDATE carries `AND status = 'redeemed'`, so of two people (or two
    tabs) pressing Run with one credit, exactly one wins and the other is told they
    need a key rather than double-spending.
    """
    row = db.one(
        "SELECT id FROM keys WHERE user_id = ? AND status = 'redeemed'"
        " ORDER BY created_at LIMIT 1", (int(user_id),))
    if not row:
        raise KeyError_("That key is not right.", "no_credit")
    cur = db.execute(
        "UPDATE keys SET status = 'spent', redeemed_at = ?"
        " WHERE id = ? AND status = 'redeemed'", (db.now(), row["id"]))
    if cur.rowcount != 1:
        raise KeyError_("That key is not right.", "no_credit")
    db.audit(user_id, "key_spent", "key=%d" % row["id"])
    return row["id"]


def refund(user_id: int, key_id: int) -> bool:
    """Undo a consume, for when the run could not actually start.

    Only for a row this panel just moved to `spent` in the same call -- there is no
    user-facing way to refund, on purpose.
    """
    cur = db.execute(
        "UPDATE keys SET status = 'redeemed', redeemed_at = NULL"
        " WHERE id = ? AND user_id = ? AND status = 'spent'", (int(key_id), int(user_id)))
    return cur.rowcount == 1


def for_user(user_id: int) -> dict:
    """What the admin console shows, and what the dashboard needs to say."""
    rows = db.query(
        """SELECT id, status, note, created_at, redeemed_at
             FROM keys WHERE user_id = ? ORDER BY created_at DESC""",
        (int(user_id),))
    return {
        "levels": credits(user_id),
        "unused": sum(1 for r in rows if r["status"] == "unused"),
        "spent": sum(1 for r in rows if r["status"] == "spent"),
        "keys": [{"id": r["id"], "status": r["status"], "note": r["note"],
                  "created_at": r["created_at"],
                  "redeemed_at": r["redeemed_at"]} for r in rows],
    }