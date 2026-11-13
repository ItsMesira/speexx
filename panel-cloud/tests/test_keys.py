"""Run keys: minting, redeeming, revoking, and the gate they enforce.

A six digit code is a million guesses, so most of what matters here is that the
code is never stored, is bound to one person, and is spent exactly once.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="panel-keys-")
# The directory, not a file: SPEEXX_PANEL_DB is not a variable db.py reads, and
# setting it silently points every query at the live database instead.
os.environ["SPEEXX_PANEL_DATA_DIR"] = _TMP
os.environ["SPEEXX_PANEL_VAULT_KEY"] = "0" * 64

import accounts  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import keys  # noqa: E402
import users  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


def make_user(username="someone", role="user"):
    return users.create_user(username, "abcd1234", role=role,
                             allow_reserved=True)


def save_account(user_id, email="student@speexx.test"):
    return accounts.add(user_id, email, "speexx-password")


class KeyTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM keys")
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM speexx_accounts")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM users")
        self.owner = make_user("owner")
        self.account = save_account(self.owner)

    def test_a_minted_key_is_six_digits(self):
        code = keys.mint(self.owner, "for the demo")
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_the_code_is_never_stored_in_the_clear(self):
        code = keys.mint(self.owner)
        rows = db.query("SELECT code_hash FROM keys")
        self.assertEqual(len(rows), 1)
        self.assertNotIn(code, rows[0]["code_hash"])
        self.assertTrue(rows[0]["code_hash"].startswith("scrypt$"))
        # And nowhere else in the file.
        with open(Path(_TMP) / "panel.db", "rb") as handle:
            self.assertNotIn(code.encode(), handle.read())

    def test_a_key_does_nothing_until_it_is_redeemed(self):
        keys.mint(self.owner)
        self.assertFalse(keys.has_access(self.owner))
        self.assertEqual(keys.for_user(self.owner)["unused"], 1)

    def test_redeeming_buys_exactly_one_level(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        self.assertEqual(keys.credits(self.owner), 1)
        keys.consume(self.owner)
        self.assertEqual(keys.credits(self.owner), 0)
        self.assertFalse(keys.has_access(self.owner))

    def test_redeeming_makes_running_possible(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        self.assertTrue(keys.has_access(self.owner))

    def test_a_spent_key_stays_spent(self):
        code = keys.mint(self.owner)
        keys.redeem(self.owner, code)
        keys.consume(self.owner)
        with self.assertRaises(keys.KeyError_):
            keys.redeem(self.owner, code)

    def test_consuming_with_nothing_left_is_refused(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        keys.consume(self.owner)
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.consume(self.owner)
        self.assertEqual(ctx.exception.code, "no_credit")

    def test_two_keys_buy_two_levels(self):
        for _ in range(2):
            keys.redeem(self.owner, keys.mint(self.owner))
        self.assertEqual(keys.credits(self.owner), 2)
        keys.consume(self.owner)
        self.assertEqual(keys.credits(self.owner), 1)
        self.assertTrue(keys.has_access(self.owner))

    def test_a_key_only_works_for_the_person_it_was_minted_for(self):
        other = make_user("other")
        code = keys.mint(self.owner)
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.redeem(other, code)
        self.assertEqual(ctx.exception.code, "bad_key")
        self.assertFalse(keys.has_access(other), "a stolen code unlocked somebody else")

    def test_a_wrong_code_is_refused_without_hints(self):
        wrong = keys.mint(self.owner, "note")
        # Right length, wrong digits.
        code = "000000" if wrong != "000000" else "111111"
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.redeem(self.owner, code)
        self.assertEqual(str(ctx.exception), "That key is not right.")

    def test_a_malformed_code_gets_the_same_refusal(self):
        for bad in ("", "abcdef", "12345", "1234567", "12 34 5"):
            with self.assertRaises(keys.KeyError_) as ctx:
                keys.redeem(self.owner, bad)
            self.assertEqual(str(ctx.exception), "That key is not right.",
                             "a different shape must not read differently")

    def test_revoke_voids_an_unused_key(self):
        code = keys.mint(self.owner)
        row = db.one("SELECT id FROM keys WHERE user_id = ?", (self.owner,))
        self.assertTrue(keys.revoke(self.owner, row["id"]))
        with self.assertRaises(keys.KeyError_):
            keys.redeem(self.owner, code)

    def test_revoke_takes_back_a_credit_not_yet_spent(self):
        """The admin needs a real kill switch, and this is it."""
        keys.redeem(self.owner, keys.mint(self.owner))
        row = db.one("SELECT id FROM keys WHERE user_id = ?", (self.owner,))
        self.assertTrue(keys.revoke(self.owner, row["id"]))
        self.assertFalse(keys.has_access(self.owner))

    def test_revoke_will_not_undo_a_spent_key(self):
        """No refund path: that would make a key a way to run unearned."""
        keys.redeem(self.owner, keys.mint(self.owner))
        keys.consume(self.owner)
        row = db.one("SELECT id FROM keys WHERE user_id = ?", (self.owner,))
        self.assertFalse(keys.revoke(self.owner, row["id"]))
        self.assertFalse(keys.has_access(self.owner))
        self.assertEqual(keys.for_user(self.owner)["spent"], 1)

    def test_keys_held_per_person_are_capped(self):
        for _ in range(keys.MAX_HELD_PER_USER):
            keys.mint(self.owner)
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.mint(self.owner)
        self.assertEqual(ctx.exception.code, "too_many")

    def test_a_credit_counts_towards_the_cap(self):
        """Redeemed levels are held too, so nobody hoards twenty of them."""
        for _ in range(keys.MAX_HELD_PER_USER):
            keys.redeem(self.owner, keys.mint(self.owner))
        self.assertEqual(keys.held(self.owner), keys.MAX_HELD_PER_USER)
        with self.assertRaises(keys.KeyError_):
            keys.mint(self.owner)

    def test_spending_frees_room_for_another_key(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        keys.consume(self.owner)
        self.assertEqual(len(keys.mint(self.owner)), 6)

    def test_minting_for_nobody_is_refused(self):
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.mint(9999)
        self.assertEqual(ctx.exception.code, "not_found")


class RunGateTests(unittest.TestCase):
    """Without a key, a person can sign in and save logins but cannot run."""

    def setUp(self):
        db.execute("DELETE FROM keys")
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM speexx_accounts")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM users")
        self.owner = make_user("owner")
        self.account = save_account(self.owner)

    def test_saving_a_login_needs_no_key(self):
        self.assertIsNotNone(accounts.list_for(self.owner))

    def test_running_needs_a_key(self):
        with self.assertRaises(jobs.NeedsKey):
            jobs.start_job(self.owner, self.account)

    def test_running_works_once_a_key_is_redeemed(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        job = jobs.start_job(self.owner, self.account)
        try:
            self.assertIn("id", job)
            self.assertEqual(job["state"], "starting")
        finally:
            jobs.stop_job(job["id"], self.owner)

    def test_starting_a_run_spends_the_key(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        job = jobs.start_job(self.owner, self.account)
        try:
            self.assertFalse(keys.has_access(self.owner),
                             "starting a run did not spend the credit")
        finally:
            jobs.stop_job(job["id"], self.owner)

    def test_stopping_a_run_does_not_give_the_key_back(self):
        """The chosen rule: any run spends it, stopped or not."""
        keys.redeem(self.owner, keys.mint(self.owner))
        job = jobs.start_job(self.owner, self.account)
        jobs.stop_job(job["id"], self.owner)
        self.assertEqual(db.one("SELECT state FROM jobs WHERE id = ?",
                                (job["id"],))["state"], "failed")
        self.assertFalse(keys.has_access(self.owner))

    def test_a_second_run_needs_a_second_key(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        first = jobs.start_job(self.owner, self.account)
        jobs.stop_job(first["id"], self.owner)
        with self.assertRaises(jobs.NeedsKey):
            jobs.start_job(self.owner, self.account)

    def test_a_run_that_never_started_is_refunded(self):
        """No free slot means nothing ran, so nothing should have been charged.

        The filler profiles belong to other people: one person is capped at
        MAX_PER_USER concurrent runs, so filling six slots from one account
        would trip the per-person limit instead of the pool limit.
        """
        for index in range(len(jobs.PROFILE_POOL)):
            filler = make_user("filler%d" % index)
            acct = save_account(filler, "filler%d@speexx.test" % index)
            jobs.reserve_slot("fill%d" % index, filler, acct,
                              "filler%d@speexx.test" % index, "/tmp/fill%d.log" % index)
        keys.redeem(self.owner, keys.mint(self.owner))
        with self.assertRaises(jobs.NoFreeProfile):
            jobs.start_job(self.owner, self.account)
        self.assertTrue(keys.has_access(self.owner),
                        "a refused run still spent the key")

    def test_one_persons_key_does_not_unlock_another(self):
        keys.redeem(self.owner, keys.mint(self.owner))
        other = make_user("other")
        their_account = save_account(other, "other@speexx.test")
        with self.assertRaises(jobs.NeedsKey):
            jobs.start_job(other, their_account)


class RateLimitShapeTests(unittest.TestCase):
    def test_redeeming_is_the_tightest_limit_in_the_panel(self):
        """A million codes is a small space; this endpoint is the one that
        walks it, so it must be the strictest bucket."""
        per_user, window = auth.LIMITS["key_redeem"]
        self.assertEqual(window, 3600)
        self.assertLessEqual(per_user, 10)
        for bucket, (count, _) in auth.LIMITS.items():
            if bucket == "key_redeem":
                continue
            self.assertGreaterEqual(count, per_user,
                                    "%s is stricter than key_redeem" % bucket)


class DataDirTests(unittest.TestCase):
    def test_keys_tests_never_touch_the_live_database(self):
        """See the note at the top: the wrong env var points at data/."""
        live = (Path(__file__).resolve().parents[1] / "data" / "panel.db").resolve()
        self.assertNotEqual(Path(db.DB_PATH).resolve(), live)
        self.assertTrue(str(db.DATA_DIR).startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()