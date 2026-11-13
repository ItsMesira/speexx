"""SQLite storage for the panel.

One file, stdlib only. WAL is on so polling never blocks a write.

Multi-user note: every table that holds user data is reachable only through an
explicit `user_id` filter in the query. That is the single most important
invariant in this file — with more than one person using the panel, a missing
`WHERE user_id = ?` is a cross-account data leak, and it looks exactly like
correct code. The partial unique indexes below are the other half of the
integrity story: the database refuses invalid states that application checks
would race past.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

# Overridable so tests get an isolated database instead of the live one.
DATA_DIR = Path(
    os.environ.get("SPEEXX_PANEL_DATA_DIR")
    or (Path(__file__).resolve().parent / "data")
)
DB_PATH = DATA_DIR / "panel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL,               -- the only identifier
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',      -- user | admin
    status        TEXT    NOT NULL DEFAULT 'pending',   -- pending | active | disabled
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    locked_until  INTEGER
);

-- Case-insensitive uniqueness: Foo@x.com and foo@x.com are one account.

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT,
    agent      TEXT
);

-- A user's saved Speexx login. The password is never stored in the clear;
-- `secret` is an AES-256-GCM blob (nonce || ciphertext) from vault.py.
CREATE TABLE IF NOT EXISTS speexx_accounts (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label          TEXT    NOT NULL,
    email          TEXT    NOT NULL,
    secret         BLOB    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'ok',   -- ok | bad_credentials | locked
    cooldown_until INTEGER,
    created_at     INTEGER NOT NULL,
    last_used_at   INTEGER,
    last_job_at    INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT    PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    account_id INTEGER REFERENCES speexx_accounts(id) ON DELETE SET NULL,
    email      TEXT    NOT NULL,
    state      TEXT    NOT NULL,
    levels     INTEGER NOT NULL DEFAULT 0,
    packets    INTEGER NOT NULL DEFAULT 0,
    profile    INTEGER,
    pgid       INTEGER,
    log_path   TEXT,
    reason     TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at   INTEGER
);

-- Two live sessions on one Speexx account invalidate each other.

-- Concurrent jobs must hold distinct device fingerprints: every session on this
-- machine shares one public IP, and Speexx allows one account per device.

-- A 6-digit run key, minted by an admin for one person. The code is never
-- stored: only a scrypt hash, so a leaked database cannot be walked.
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'unused',   -- unused | redeemed | revoked
    note        TEXT,
    created_at  INTEGER NOT NULL,
    redeemed_at INTEGER
);

CREATE TABLE IF NOT EXISTS cooldowns (
    email  TEXT    PRIMARY KEY,
    until  INTEGER NOT NULL,
    reason TEXT    NOT NULL,
    at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       TEXT    NOT NULL,
    ident        TEXT    NOT NULL,
    hits         INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    PRIMARY KEY (bucket, ident)
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER,
    action  TEXT    NOT NULL,
    detail  TEXT,
    at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

# Indexes are applied *after* _migrate(), never as part of SCHEMA: several of
# them name columns (jobs.user_id, jobs.account_id) that an older database does
# not have yet, and an index created before the column exists aborts the whole
# bootstrap.
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS users_username_nocase ON users(username COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE INDEX IF NOT EXISTS speexx_accounts_user ON speexx_accounts(user_id);
CREATE INDEX IF NOT EXISTS keys_user ON keys(user_id, status);

-- Globally unique, not per user: one Speexx login may only be saved once across
-- the whole panel, so two people cannot both queue the same portal account.
CREATE UNIQUE INDEX IF NOT EXISTS speexx_accounts_email
    ON speexx_accounts(email COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at DESC);

-- Two live sessions on one Speexx account invalidate each other.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_account
    ON jobs(email) WHERE state IN ('queued', 'starting', 'running');

-- Concurrent jobs must hold distinct device fingerprints: every session on this
-- machine shares one public IP, and Speexx allows one account per device.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_profile
    ON jobs(profile) WHERE state IN ('queued', 'starting', 'running')
                         AND profile IS NOT NULL;

CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id, at DESC);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

def now() -> int:
    return int(time.time())

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.OperationalError:
        return set()

def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column in _columns(conn, table):
        return
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))

def _drop_legacy_sessions(conn: sqlite3.Connection) -> None:
    """Retire single-user sessions before the schema runs.

    The old `sessions` table had no owner: those cookies belonged to the retired
    one-admin panel and would otherwise authenticate a stranger into the new
    multi-user panel. They cannot be attributed to a person, so they are dropped
    and everyone logs in again. Runs *before* SCHEMA so the table is recreated
    in its current shape.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "sessions" in tables and "user_id" not in _columns(conn, "sessions"):
        conn.execute("DROP TABLE sessions")


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Bring `users` to username-only, in place.

    Went through two shapes: first email-only, then username + email, now
    username only. Rather than chain a third ALTER, the table is rebuilt once
    with the columns that exist. SQLite cannot drop a column that an index
    covers, so the index goes first.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "users" not in tables:
        return
    cols = _columns(conn, "users")
    wanted = ["id", "username", "password_hash", "role", "status", "created_at",
              "last_login_at", "fail_count", "locked_until"]
    if set(wanted) <= cols and "email" not in cols:
        return                      # already current

    conn.execute("DROP INDEX IF EXISTS users_email_nocase")
    has = cols
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    names = list(rows[0].keys()) if rows else list(has)

    # Username from the address local part where one exists, so an older account
    # keeps working under a name its owner can guess. Two addresses can share a
    # local part, so collisions get a suffix rather than failing the upgrade.
    seen = {}
    for row in rows:
        name = (row["username"] if "username" in names else "") or ""
        if not name and "email" in names and row["email"]:
            name = str(row["email"]).split("@")[0].lower()
        name = (name or "user%d" % row["id"]).lower()
        n = seen.get(name, 0) + 1
        seen[name] = n
        if n > 1:
            name = "%s%d" % (name, n)
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (name, row["id"]))

    target = [c for c in wanted if c in cols] or ["id", "password_hash", "role",
                                                  "status", "created_at"]

    # legacy_alter_table keeps RENAME FROM rewriting the REFERENCES clauses in
    # every other table. Without it, SQLite repoints sessions.user_id and
    # speexx_accounts.user_id at the temporary name, and dropping that table
    # leaves them referencing nothing -- so the next DELETE FROM users fails
    # with "no such table". This bit once; do not turn it off.
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        _rebuild_users(conn, target)
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")


def _rebuild_users(conn: sqlite3.Connection, target: list[str]) -> None:
    """Swap `users` for the username-only shape. Caller holds legacy_alter_table."""
    conn.execute("ALTER TABLE users RENAME TO users_old_email")
    conn.execute(
        """CREATE TABLE users (
               id            INTEGER PRIMARY KEY,
               username      TEXT    NOT NULL,
               password_hash TEXT    NOT NULL,
               role          TEXT    NOT NULL DEFAULT 'user',
               status        TEXT    NOT NULL DEFAULT 'pending',
               created_at    INTEGER NOT NULL,
               last_login_at INTEGER,
               fail_count    INTEGER NOT NULL DEFAULT 0,
               locked_until  INTEGER
           )""")
    conn.execute(
        "INSERT INTO users (%s) SELECT %s FROM users_old_email"
        % (", ".join(target), ", ".join(target)))
    conn.execute("DROP TABLE users_old_email")


def _repair_stale_user_references(conn: sqlite3.Connection) -> None:
    """Point any surviving REFERENCES back at `users`.

    Only needed for a database already migrated by the version that renamed
    `users` without the pragma in `_migrate_users`: SQLite rewrote
    `sessions.user_id` and `speexx_accounts.user_id` to name the temporary
    table, and after it was dropped those references dangled, so every
    `DELETE FROM users` failed. Each affected table is recreated from its own
    definition with the name corrected. Its secondary indexes are dropped
    first and rebuilt by INDEXES, which runs immediately after this.
    """
    stale = "users_old_email"
    for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        table = row["name"]
        sql = row["sql"] or ""
        if table == "users" or stale not in sql:
            continue
        fixed = (sql.replace('REFERENCES "%s"' % stale, "REFERENCES users")
                    .replace("REFERENCES %s" % stale, "REFERENCES users"))
        for idx in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name = ? AND sql IS NOT NULL", (table,)).fetchall():
            conn.execute("DROP INDEX IF EXISTS %s" % idx["name"])
        holder = "%s_repair" % table
        conn.execute("ALTER TABLE %s RENAME TO %s" % (table, holder))
        conn.execute(fixed)
        columns = ", ".join(sorted(_columns(conn, holder)))
        conn.execute("INSERT INTO %s (%s) SELECT %s FROM %s"
                     % (table, columns, columns, holder))
        conn.execute("DROP TABLE %s" % holder)


def _drop_legacy_keys(conn: sqlite3.Connection) -> None:
    """Retire the first version's key table before SCHEMA runs.

    That one minted anonymous keys against a browser fingerprint
    (`key_hash`, `fingerprint`, `used_by_email`). Keys are now minted by an
    admin for a named person, so the columns differ and `CREATE TABLE IF NOT
    EXISTS` would keep the old shape and break the new index. Rows are dropped
    rather than translated: nobody should be holding a live code from a design
    that no longer exists.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "keys" in tables and "user_id" not in _columns(conn, "keys"):
        conn.execute("DROP INDEX IF EXISTS keys_user")
        conn.execute("DROP TABLE keys")


def _migrate_keys(conn: sqlite3.Connection) -> None:
    """Replace the first version's key table with the per-user one.

    The original panel minted anonymous keys against a browser fingerprint. They
    are now minted by an admin for a named person, so the columns are different
    and the old rows mean nothing. A `keys` table without `user_id` is dropped
    outright rather than translated: nobody should be holding a live code from a
    design that no longer exists.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "keys" in tables and "user_id" not in _columns(conn, "keys"):
        conn.execute("DROP INDEX IF EXISTS keys_user")
        conn.execute("DROP TABLE keys")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older panel.db up to the current schema.

    Two generations of change are folded in here:
      * `lessons` became `levels` + `packets` when the unit of work changed.
      * jobs gained `user_id` / `account_id` when the panel became multi-user,
        and the account uniqueness index moved from `user_id` to `email`.
    """
    _migrate_users(conn)
    _repair_stale_user_references(conn)

    if "jobs" in {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        _add_column(conn, "jobs", "levels", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "jobs", "packets", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "jobs", "user_id", "INTEGER")
        _add_column(conn, "jobs", "account_id", "INTEGER")
        cols = _columns(conn, "jobs")
        if "lessons" in cols:
            conn.execute("UPDATE jobs SET levels = lessons WHERE levels = 0 AND lessons > 0")
            conn.execute("UPDATE jobs SET packets = lessons WHERE packets = 0 AND levels > 0")
    # The old single-user index keyed on user_id, which no longer means anything.
    conn.execute("DROP INDEX IF EXISTS jobs_one_active_per_email")

def connect() -> sqlite3.Connection:
    """One connection per thread; FastAPI runs handlers in a worker pool."""
    global _initialised
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    if not _initialised:
        with _init_lock:
            if not _initialised:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                os.chmod(DATA_DIR, 0o700)
                boot = sqlite3.connect(DB_PATH)
                boot.row_factory = sqlite3.Row
                _drop_legacy_sessions(boot)
                _drop_legacy_keys(boot)
                boot.executescript(SCHEMA)
                _migrate(boot)
                boot.executescript(INDEXES)
                boot.commit()
                boot.close()
                os.chmod(DB_PATH, 0o600)
                _initialised = True

    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    _local.conn = conn
    return conn

def get_meta(key: str) -> str | None:
    row = connect().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None

def set_meta(key: str, value: str) -> None:
    connect().execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )

def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, args).fetchall()

def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, args).fetchone()

def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    return connect().execute(sql, args)

def audit(user_id: int | None, action: str, detail: str = "") -> None:
    """Append-only trail. `detail` must never contain a credential."""
    execute(
        "INSERT INTO audit (user_id, action, detail, at) VALUES (?, ?, ?, ?)",
        (user_id, action, detail[:300], now()),
    )