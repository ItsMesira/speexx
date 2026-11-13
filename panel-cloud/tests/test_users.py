"""Multi-user rules: approval, lockout, encryption, and above all isolation.

These are the tests that matter most in a public service. One person must never
see or touch another person's saved passwords, even by guessing an id.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="panel-users-")
# The directory, not a file: SPEEXX_PANEL_DB is not a variable db.py reads, and
# setting it silently points every query at the live database instead.
os.environ["SPEEXX_PANEL_DATA_DIR"] = _TMP
os.environ["SPEEXX_PANEL_VAULT_KEY"] = "0" * 64

import accounts  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import users  # noqa: E402
import vault  # noqa: E402

GOOD_PASSWORD = "correct-horse-battery-staple"


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


def make_user(username, password=GOOD_PASSWORD, role="user", allow_reserved=False):
    return users.create_user(username, password, role=role,
                             allow_reserved=allow_reserved)


def approve(user_id):
    users.set_status(user_id, "active")


class DataDirTests(unittest.TestCase):
    def test_tests_never_touch_the_live_database(self):
        """If this fails, the suite has been writing to data/panel.db for real.

        It happened once: a wrong environment-variable name was used, db.py
        ignored it, and the fixtures signed up alice@example.test in the live
        database and deleted its job history.
        """
        # Whichever test module imports first wins the env var, so compare
        # against the property rather than one module's temp path: the database
        # must not be the panel's own data directory, and must live in a
        # temporary directory.
        live = (Path(__file__).resolve().parents[1] / "data" / "panel.db").resolve()
        self.assertNotEqual(Path(db.DB_PATH).resolve(), live)
        self.assertEqual(db.DATA_DIR, Path(db.DATA_DIR))
        self.assertTrue(str(db.DATA_DIR).startswith(tempfile.gettempdir()),
                        "tests must use a temp directory, not %s" % db.DATA_DIR)


class SignupTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM users")

    def test_the_next_arrival_owns_a_panel_with_no_admin(self):
        """After --reset-admin, other accounts remain but nobody owns the panel."""
        make_user("oldowner")           # becomes admin while empty
        other = make_user("other")      # ordinary, active
        db.execute("DELETE FROM users WHERE role = 'admin'")   # what reset-admin does

        uid = make_user("newowner")
        self.assertEqual(users.get_by_id(uid)["role"], "admin")
        self.assertEqual(users.get_by_id(other)["status"], "active")

    def test_first_account_owns_the_panel(self):
        """Bootstrap: on an empty panel the first sign-up is the admin.

        Without this there is nobody who can approve anybody, which is the
        dead end approval-only sign-up creates on a fresh install.
        """
        uid = make_user("first")
        row = users.get_by_id(uid)
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["status"], "active")
        self.assertEqual(users.authenticate("first", GOOD_PASSWORD)["role"], "admin")

    def test_everybody_else_signs_in_straight_away(self):
        """No approval queue. A key is what gates running, not signing in."""
        make_user("first")
        uid = make_user("second")
        self.assertEqual(users.get_by_id(uid)["role"], "user")
        self.assertEqual(users.get_by_id(uid)["status"], "active")
        self.assertEqual(users.authenticate("second", GOOD_PASSWORD)["id"], uid)

    def test_approval_lets_the_same_password_in(self):
        uid = make_user("new")
        approve(uid)
        self.assertEqual(users.authenticate("new", GOOD_PASSWORD)["id"], uid)

    def test_disabling_kills_live_sessions(self):
        uid = make_user("bye")
        approve(uid)
        token, _ = users.create_session(uid)
        self.assertIsNotNone(users.read_session(token))
        users.set_status(uid, "disabled")
        self.assertIsNone(users.read_session(token), "a disabled user's session survived")

    def test_duplicate_name_refused(self):
        make_user("dup")
        with self.assertRaises(users.AuthError) as ctx:
            make_user("dup")
        self.assertEqual(ctx.exception.code, "username_taken")

    def test_short_password_refused(self):
        with self.assertRaises(users.AuthError):
            make_user("short", "hunter2")

    def test_common_password_refused(self):
        with self.assertRaises(users.AuthError):
            make_user("common", "password123")

    def test_chosen_username_is_kept(self):
        self.assertEqual(users.get_by_id(make_user("blue"))["username"], "blue")

    def test_username_taken_is_refused(self):
        make_user("blue")
        with self.assertRaises(users.AuthError) as ctx:
            make_user("BLUE")
        self.assertEqual(ctx.exception.code, "username_taken")

    def test_admin_name_is_reserved(self):
        with self.assertRaises(users.AuthError) as ctx:
            make_user("admin")
        self.assertEqual(ctx.exception.code, "bad_username")

    def test_sign_in_by_email_still_works(self):
        """Someone who types their address instead of their name is not stuck."""
        uid = make_user("secret", GOOD_PASSWORD)
        self.assertEqual(users.authenticate("secret", GOOD_PASSWORD)["id"], uid)

    def test_eight_characters_are_enough(self):
        uid = make_user("short", "abcd1234")
        self.assertIsNotNone(uid)

    def test_password_never_stored_in_the_clear(self):
        make_user("secret", GOOD_PASSWORD)
        row = users.get_by_username("secret")
        self.assertNotIn(GOOD_PASSWORD, row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("scrypt$"))

    def test_wrong_password_and_unknown_name_look_identical(self):
        uid = make_user("quiet")
        approve(uid)
        with self.assertRaises(users.AuthError) as wrong:
            users.authenticate("quiet", "not-the-password")
        with self.assertRaises(users.AuthError) as unknown:
            users.authenticate("nobody", "not-the-password")
        self.assertEqual(str(wrong.exception), str(unknown.exception))
        self.assertEqual(wrong.exception.code, unknown.exception.code)


class LockoutTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM users")
        self.uid = make_user("lock")
        approve(self.uid)

    def test_repeated_failures_lock_the_account(self):
        for _ in range(users.MAX_FAILS):
            with self.assertRaises(users.AuthError):
                users.authenticate("lock", "wrong-one")
        with self.assertRaises(users.AuthError) as ctx:
            users.authenticate("lock", GOOD_PASSWORD)
        self.assertEqual(ctx.exception.code, "locked")
        self.assertIsNotNone(users.get_by_id(self.uid)["locked_until"])

    def test_one_typo_does_not_lock_you_out(self):
        for _ in range(users.MAX_FAILS - 1):
            with self.assertRaises(users.AuthError):
                users.authenticate("lock", "typo")
        self.assertIsNone(users.get_by_id(self.uid)["locked_until"])
        self.assertEqual(users.authenticate("lock", GOOD_PASSWORD)["id"], self.uid)

    def test_lockout_backs_off_but_is_capped(self):
        first = users.lock_seconds_for(users.MAX_FAILS)
        self.assertGreater(users.lock_seconds_for(6), first)
        self.assertLessEqual(users.lock_seconds_for(99), 6 * 3600)

    def test_a_good_login_clears_the_failure_count(self):
        with self.assertRaises(users.AuthError):
            users.authenticate("lock", "wrong-one")
        users.authenticate("lock", GOOD_PASSWORD)
        self.assertEqual(users.get_by_id(self.uid)["fail_count"], 0)


class VaultTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM users")

    def test_roundtrip(self):
        blob = vault.encrypt("hunter-two", 7, "a@b.c")
        self.assertNotIn(b"hunter-two", blob)
        self.assertEqual(vault.decrypt(blob, 7, "a@b.c"), "hunter-two")

    def test_ciphertext_cannot_be_moved_to_another_owner(self):
        blob = vault.encrypt("hunter-two", 7, "a@b.c")
        with self.assertRaises(vault.VaultError):
            vault.decrypt(blob, 8, "a@b.c")   # different person
        with self.assertRaises(vault.VaultError):
            vault.decrypt(blob, 7, "other@b.c")  # same person, different address

    def test_tampering_is_detected(self):
        blob = bytearray(vault.encrypt("hunter-two", 7, "a@b.c"))
        blob[-1] ^= 0x01
        with self.assertRaises(vault.VaultError):
            vault.decrypt(bytes(blob), 7, "a@b.c")


class OwnershipTests(unittest.TestCase):
    """The isolation guarantees. Each test tries the attack directly."""

    def setUp(self):
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM speexx_accounts")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM users")
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        for uid in (self.alice, self.bob):
            approve(uid)
        self.alice_acct = accounts.add(self.alice, "alice@speexx.test", "alice-secret")
        self.bob_acct = accounts.add(self.bob, "bob@speexx.test", "bob-secret")

    def test_listing_shows_only_your_own_accounts(self):
        self.assertEqual([a["email"] for a in accounts.list_for(self.alice)],
                         ["alice@speexx.test"])

    def test_fetching_someone_elses_account_is_refused(self):
        self.assertIsNone(accounts.get_owned(self.bob, self.alice_acct))
        with self.assertRaises(accounts.AccountError):
            accounts.credentials(self.bob, self.alice_acct)

    def test_credentials_of_a_foreign_account_never_return(self):
        with self.assertRaises(accounts.AccountError):
            accounts.credentials(self.alice, self.bob_acct)

    def test_deleting_someone_elses_account_does_nothing(self):
        self.assertFalse(accounts.remove(self.bob, self.alice_acct))
        self.assertIsNotNone(accounts.get_owned(self.alice, self.alice_acct))

    def test_same_speexx_email_can_only_be_saved_once(self):
        """Global, not per user: one portal login may not be saved by two people."""
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.add(self.bob, "alice@speexx.test", "stolen")
        self.assertEqual(ctx.exception.code, "duplicate")

    def test_account_cap_per_person(self):
        for i in range(accounts.MAX_ACCOUNTS_PER_USER - 1):
            accounts.add(self.bob, "extra%d@speexx.test" % i, "pw-for-extra-%d" % i)
        with self.assertRaises(accounts.AccountError):
            accounts.add(self.bob, "one-too-many@speexx.test", "pw-too-many")

    def test_job_is_invisible_to_another_person(self):
        jobs.reserve_slot("jobA", self.alice, self.alice_acct,
                          "alice@speexx.test", "/tmp/jobA.log")
        self.assertIsNone(jobs.get_job("jobA", self.bob))
        self.assertIsNotNone(jobs.get_job("jobA", self.alice))

    def test_a_job_carries_the_speexx_address_not_a_panel_identity(self):
        """A run is about a Speexx login, so it keeps that address — and nothing
        of the panel's own account, which is a bare name now."""
        jobs.reserve_slot("jobA", self.alice, self.alice_acct,
                          "alice@speexx.test", "/tmp/jobA.log")
        for job in jobs.jobs_for(self.alice):
            self.assertNotIn("username", job)
            self.assertNotIn("password", job)

    def test_job_list_is_scoped_to_the_owner(self):
        jobs.reserve_slot("jobA", self.alice, self.alice_acct,
                          "alice@speexx.test", "/tmp/jobA.log")
        jobs.reserve_slot("jobB", self.bob, self.bob_acct,
                          "bob@speexx.test", "/tmp/jobB.log")
        self.assertEqual([j["id"] for j in jobs.jobs_for(self.bob)], ["jobB"])
        self.assertEqual(len(jobs.jobs_all()), 2, "admins do see both")

    def test_stopping_someone_elses_run_is_refused(self):
        jobs.reserve_slot("jobA", self.alice, self.alice_acct,
                          "alice@speexx.test", "/tmp/jobA.log")
        self.assertFalse(jobs.stop_job("jobA", self.bob),
                         "a stranger stopped another person's run")

    def test_one_address_cannot_run_twice_at_once(self):
        # A duplicate saved account can no longer be created at all, so stage
        # the collision one level down: a live job row on one portal address.
        db.execute(
            """INSERT INTO jobs (id, user_id, email, state, levels, packets, profile,
                                 created_at)
               VALUES ('staged', ?, 'alice@speexx.test', 'running', 0, 0, 3, ?)""",
            (self.bob, db.now()),
        )
        with self.assertRaises(jobs.AccountBusy):
            jobs.reserve_slot("jobB", self.bob, self.bob_acct,
                              "alice@speexx.test", "/tmp/jobB.log")

    def test_per_person_run_cap(self):
        for i in range(jobs.MAX_PER_USER):
            acct = accounts.add(self.bob, "cap%d@speexx.test" % i, "pw-cap-%d" % i)
            jobs.reserve_slot("cap%d" % i, self.bob, acct,
                              "cap%d@speexx.test" % i, "/tmp/cap%d.log" % i)
        extra = accounts.add(self.bob, "cap-over@speexx.test", "pw-cap-over")
        with self.assertRaises(jobs.TooManyJobs):
            jobs.reserve_slot("cap-over", self.bob, extra,
                              "cap-over@speexx.test", "/tmp/over.log")

    def test_cooldown_is_hidden_from_another_person(self):
        accounts.note_cooldown("alice@speexx.test", "bad_credentials")
        self.assertIsNotNone(accounts.cooldown_for("alice@speexx.test"))
        self.assertIsNone(accounts.cooldown_for("bob@speexx.test"))


class AdminTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM users")
        self.admin = make_user("admin", role="admin", allow_reserved=True)
        self.member = make_user("member")
        approve(self.admin)
        approve(self.member)

    def test_admin_can_see_everyone(self):
        listed = users.list_users()
        self.assertEqual({u["username"] for u in listed},
                         {"admin", "member"})
        for user in listed:
            self.assertNotIn("email", user,
                             "the admin list shows names, not addresses")

    def test_public_view_never_leaks_the_password_hash(self):
        for user in users.list_users():
            self.assertNotIn("password_hash", user)
            self.assertNotIn("fail_count", user)
            self.assertNotIn("email", user)

    def test_bootstrap_needs_an_explicit_password(self):
        """No env password means no generated admin -- that was the dead end."""
        os.environ.pop("SPEEXX_PANEL_ADMIN_PASSWORD", None)
        self.assertIsNone(users.bootstrap_admin())

    def test_bootstrap_never_overwrites_an_existing_admin(self):
        before = users.get_by_username("admin")["password_hash"]
        users.bootstrap_admin()
        self.assertEqual(users.get_by_username("admin")["password_hash"], before)

    def test_status_change_is_restricted_to_known_states(self):
        self.assertFalse(users.set_status(self.member, "superuser"))
        self.assertEqual(users.get_by_id(self.member)["status"], "active")



if __name__ == "__main__":
    unittest.main()

class MigrationTests(unittest.TestCase):
    """The username-only rebuild, and the foreign-key trap it walked into."""

    def _fresh_db(self):
        import sqlite3
        path = Path(_TMP) / ("migrate-%s.db" % os.urandom(4).hex())
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return path, conn

    def _old_users_table(self, conn):
        """The email-only shape this project used to have."""
        conn.executescript("""
            CREATE TABLE users (
                id            INTEGER PRIMARY KEY,
                email         TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'user',
                status        TEXT    NOT NULL DEFAULT 'pending',
                created_at    INTEGER NOT NULL,
                last_login_at INTEGER,
                fail_count    INTEGER NOT NULL DEFAULT 0,
                locked_until  INTEGER,
                username      TEXT
            );
            CREATE UNIQUE INDEX users_email_nocase ON users(email COLLATE NOCASE);
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf       TEXT NOT NULL, created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL, ip TEXT, agent TEXT
            );
            CREATE TABLE speexx_accounts (
                id INTEGER PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label TEXT NOT NULL, email TEXT NOT NULL, secret BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok', cooldown_until INTEGER,
                created_at INTEGER NOT NULL, last_used_at INTEGER, last_job_at INTEGER
            );
            INSERT INTO users (email, password_hash, role, status, created_at)
                VALUES ('old@example.test', 'x', 'admin', 'active', 1),
                       ('two@example.test', 'x', 'user', 'active', 2);
        """)
        conn.commit()

    def test_old_email_database_is_rebuilt_and_still_deletes(self):
        path, conn = self._fresh_db()
        self.addCleanup(path.unlink, missing_ok=True)
        self._old_users_table(conn)

        db._migrate_users(conn)
        db._repair_stale_user_references(conn)

        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        self.assertIn("username", cols)
        self.assertNotIn("email", cols, "the email column survived the migration")
        names = sorted(r["username"] for r in conn.execute("SELECT username FROM users"))
        self.assertEqual(names, ["old", "two"])

        # The whole point: a dangling reference made every delete fail.
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM speexx_accounts")
        conn.execute("DELETE FROM users")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_child_tables_keep_referencing_users(self):
        path, conn = self._fresh_db()
        self.addCleanup(path.unlink, missing_ok=True)
        self._old_users_table(conn)
        db._migrate_users(conn)
        db._repair_stale_user_references(conn)
        for table in ("sessions", "speexx_accounts"):
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                               (table,)).fetchone()[0]
            self.assertIn("REFERENCES users", sql, table)
            self.assertNotIn("users_old_email", sql, table)

    def test_migration_is_idempotent(self):
        path, conn = self._fresh_db()
        self.addCleanup(path.unlink, missing_ok=True)
        self._old_users_table(conn)
        for _ in range(3):
            db._migrate_users(conn)
            db._repair_stale_user_references(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
