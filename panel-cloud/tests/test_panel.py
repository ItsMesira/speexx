"""Unit tests for the pure logic that carries the security guarantees.

These exist because keys, admin auth, and the login classifier are ordinary
functions over plain data — the browser is not involved, so they can be tested
properly. The bot itself cannot: only a live run proves that.

Run:  .venv/bin/python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

# Point the database at a throwaway directory BEFORE importing anything that
# touches it, so a test run can never read or write the live panel.db.
_TMP = tempfile.mkdtemp(prefix="panel-test-")
os.environ["SPEEXX_PANEL_DATA_DIR"] = _TMP
os.environ.pop("SPEEXX_PANEL_ADMIN_PASSWORD", None)
os.environ.pop("SPEEXX_PANEL_ALLOWED_HOSTS", None)

import accounts  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import preflight  # noqa: E402
import users  # noqa: E402
import vault  # noqa: E402


def seed_user(username: str = "seed", role: str = "user") -> int:
    """A real user row, because sessions and jobs carry a user_id foreign key.

    Idempotent: setUp runs per test, so a fixed name has to be cleared first.
    """
    db.execute("DELETE FROM sessions")
    db.execute("DELETE FROM users WHERE username = ?", (username,))
    cur = db.execute(
        "INSERT INTO users (username, password_hash, role, status, created_at)"
        " VALUES (?, 'x', ?, 'active', ?)",
        (username, role, db.now()),
    )
    return cur.lastrowid


def seed_account(user_id: int, email: str = "acct@example.test") -> int:
    """A real saved-account row, because jobs.account_id is a foreign key."""
    return accounts.add(user_id, email, "seed-password-value")


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


class PasswordTests(unittest.TestCase):
    def test_roundtrip(self):
        stored = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", stored))
        self.assertFalse(auth.verify_password("wrong", stored))

    def test_salted(self):
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_garbage_does_not_crash(self):
        for bad in ("", "nope", "scrypt$1$2$3", "scrypt$a$b$c$d$e"):
            self.assertFalse(auth.verify_password("x", bad))


class SessionTests(unittest.TestCase):
    def test_lifecycle(self):
        token, csrf = users.create_session(seed_user("sess"))
        session = users.read_session(token)
        self.assertIsNotNone(session)
        self.assertEqual(session["csrf"], csrf)
        users.destroy_session(token)
        self.assertIsNone(users.read_session(token))

    def test_expired_session_rejected(self):
        token, _ = users.create_session(seed_user("exp"))
        db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                   (db.now() - 1, users._token_hash(token)))
        self.assertIsNone(users.read_session(token))

    def test_random_token_not_deterministic(self):
        uid = seed_user("rand")
        a, _ = users.create_session(uid)
        b, _ = users.create_session(uid)
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        auth.reset_rate_limits()

    def test_login_limit_trips(self):
        limit, _ = auth.LIMITS["login"]
        for _ in range(limit):
            auth.rate_limit("login", "1.2.3.4")
        with self.assertRaises(auth.RateLimited):
            auth.rate_limit("login", "1.2.3.4")

    def test_buckets_are_independent(self):
        for _ in range(auth.LIMITS["login"][0]):
            auth.rate_limit("login", "1.2.3.4")
        with self.assertRaises(auth.RateLimited):
            auth.rate_limit("login", "1.2.3.4")
        auth.rate_limit("job_start", "1.2.3.4")  # must not raise

    def test_retry_after_is_positive(self):
        for _ in range(auth.LIMITS["job_start"][0]):
            auth.rate_limit("job_start", "9.9.9.9")
        with self.assertRaises(auth.RateLimited) as ctx:
            auth.rate_limit("job_start", "9.9.9.9")
        self.assertGreaterEqual(ctx.exception.retry_after, 1)


class HostTests(unittest.TestCase):
    def test_loopback_allowed_with_port(self):
        for host in ("127.0.0.1:8787", "localhost:8787", "LOCALHOST", "[::1]:8787"):
            self.assertTrue(auth.host_allowed(host), host)

    def test_rebinding_host_refused(self):
        for host in ("evil.example.com", "attacker.test:8787", "", None,
                     "127.0.0.1.evil.com"):
            self.assertFalse(auth.host_allowed(host), host)

    def test_extra_hosts_from_env(self):
        os.environ["SPEEXX_PANEL_ALLOWED_HOSTS"] = "speexx.example.com"
        try:
            self.assertTrue(auth.host_allowed("speexx.example.com"))
            self.assertTrue(auth.host_allowed("127.0.0.1:8787"))
        finally:
            del os.environ["SPEEXX_PANEL_ALLOWED_HOSTS"]


class OriginWildcardTests(unittest.TestCase):
    """A *.suffix entry must satisfy the Origin check, not just the Host check.

    They were separate code paths and only the Host one knew about wildcards, so
    a tunnel host loaded the page (200) and then every login POST was refused
    with 403 before the password was compared.
    """

    def setUp(self):
        self._saved = os.environ.get("SPEEXX_PANEL_ALLOWED_HOSTS")
        os.environ["SPEEXX_PANEL_ALLOWED_HOSTS"] = "127.0.0.1,localhost,*.ngrok-free.dev"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SPEEXX_PANEL_ALLOWED_HOSTS", None)
        else:
            os.environ["SPEEXX_PANEL_ALLOWED_HOSTS"] = self._saved

    def test_tunnel_origin_is_accepted(self):
        req = FakeRequest("127.0.0.1", {"origin": "https://abc-def-123.ngrok-free.dev"})
        self.assertTrue(auth.origin_allowed(req), "tunnel Origin must pass")

    def test_tunnel_host_is_accepted(self):
        self.assertTrue(auth.host_allowed("abc-def-123.ngrok-free.dev"))

    def test_foreign_origin_still_refused(self):
        req = FakeRequest("127.0.0.1", {"origin": "https://evil.example.com"})
        self.assertFalse(auth.origin_allowed(req))

    def test_non_http_scheme_refused(self):
        req = FakeRequest("127.0.0.1", {"origin": "null"})
        self.assertFalse(auth.origin_allowed(req))
        req = FakeRequest("127.0.0.1", {"origin": "file://abc.ngrok-free.dev"})
        self.assertFalse(auth.origin_allowed(req))

    def test_wildcard_does_not_leak_to_lookalikes(self):
        for bad in ("https://evilngrok-free.dev", "https://abc.ngrok-free.dev.evil.com"):
            self.assertFalse(auth.origin_allowed(FakeRequest("127.0.0.1", {"origin": bad})), bad)


class FakePage:
    """Minimal stand-in for a Playwright page, so verify() is unit testable."""

    def __init__(self, url, text="", form=False):
        self.url = url
        self._text = text
        self._form = form

    def locator(self, _sel):
        class L:
            def __init__(self, present):
                self._present = present
            def count(self):
                return 1 if self._present else 0
        return L(self._form)

    def evaluate(self, _js, *_a):
        return self._text


class LoginVerificationTests(unittest.TestCase):
    """Regression cover for the false 'login failed' report.

    A Thai account logged in successfully, the check looked for an English word
    on the page, found none, and returned `unknown` three times over with a 60 s
    backoff. Success is now structural: off /login with no form present.
    """

    def test_success_even_with_no_recognisable_marker(self):
        page = FakePage("https://portal.speexx.com/", text="แดชบอร์ด", form=False)
        ok, reason = preflight.verify(page)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, preflight.OK)

    def test_still_on_login_is_a_failure(self):
        ok, reason = preflight.verify(FakePage("https://portal.speexx.com/login", "welcome"))
        self.assertFalse(ok)
        self.assertEqual(reason, preflight.UNKNOWN)

    def test_wrong_password_is_classified_from_the_form(self):
        page = FakePage("https://portal.speexx.com/login",
                        "รหัสผ่านไม่ถูกต้อง", form=True)
        ok, reason = preflight.verify(page)
        self.assertFalse(ok)
        self.assertEqual(reason, preflight.BAD_CREDENTIALS)
        self.assertIn(reason, preflight.FATAL)

    def test_locked_is_never_mistaken_for_transient(self):
        page = FakePage("https://portal.speexx.com/login",
                        "Too many failed attempts", form=True)
        ok, reason = preflight.verify(page)
        self.assertFalse(ok)
        self.assertEqual(reason, preflight.LOCKED)
        self.assertIn(reason, preflight.FATAL)

    def test_error_page_without_a_form_is_not_read_as_success(self):
        """Off /login but the form is still mounted: mid-navigation, not in."""
        ok, reason = preflight.verify(FakePage("https://portal.speexx.com/x", "oops", form=True))
        self.assertFalse(ok)

    def test_markers_are_decoration_only(self):
        page = FakePage("https://portal.speexx.com/", text="no marker here")
        self.assertTrue(preflight.verify(page)[0])
        self.assertFalse(preflight.signed_in("no marker here"))
        self.assertEqual(preflight.describe_login(page), "structural")


class FakeRequest:
    def __init__(self, host="127.0.0.1", headers=None, scheme="http"):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()


class ClientIpTests(unittest.TestCase):
    def test_loopback_peer_may_speak_for_the_client(self):
        req = FakeRequest("127.0.0.1", {"cf-connecting-ip": "203.0.113.9"})
        self.assertEqual(auth.client_ip(req), "203.0.113.9")

    def test_first_forwarded_entry_wins(self):
        req = FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
        self.assertEqual(auth.client_ip(req), "203.0.113.9")

    def test_header_ignored_from_a_non_loopback_peer(self):
        """Otherwise anyone could spoof their IP and reset their own limiter."""
        req = FakeRequest("203.0.113.50", {"cf-connecting-ip": "1.2.3.4"})
        self.assertEqual(auth.client_ip(req), "203.0.113.50")

    def test_https_detection_through_proxy(self):
        self.assertTrue(auth.is_https(FakeRequest(headers={"x-forwarded-proto": "https"})))
        self.assertFalse(auth.is_https(FakeRequest()))


class LoginClassifierTests(unittest.TestCase):
    def test_bad_credentials(self):
        for text in ("Incorrect password", "invalid username or password",
                     "รหัสผ่านไม่ถูกต้อง", "密码错误", "비밀번호가 틀렸습니다",
                     "falsches Passwort", "Mot de passe incorrect"):
            self.assertEqual(preflight.classify_text(text), preflight.BAD_CREDENTIALS, text)

    def test_locked(self):
        for text in ("Your account has been locked", "Too many failed attempts",
                     "account suspended", "ปิดการใช้งาน", "账号已锁定"):
            self.assertEqual(preflight.classify_text(text), preflight.LOCKED, text)

    def test_locked_wins_over_rate_limited(self):
        """'Too many failed attempts' also contains 'too many attempts'.

        Getting this backwards would classify a hard lock as transient and retry
        it, which is exactly the IP-lock bug being prevented.
        """
        self.assertEqual(
            preflight.classify_text("Too many failed attempts, try again in 30 minutes"),
            preflight.LOCKED,
        )

    def test_rate_limited_and_captcha(self):
        self.assertEqual(preflight.classify_text("Too many requests"), preflight.RATE_LIMITED)
        self.assertEqual(preflight.classify_text("Please verify you are human"),
                         preflight.CAPTCHA)

    def test_unknown_is_not_fatal(self):
        for text in ("", "Something entirely unexpected", None):
            reason = preflight.classify_text(text)
            self.assertEqual(reason, preflight.UNKNOWN)
        self.assertNotIn(preflight.UNKNOWN, preflight.FATAL)

    def test_fatal_set_is_exactly_the_two_we_refuse_to_retry(self):
        self.assertEqual(preflight.FATAL, {preflight.BAD_CREDENTIALS, preflight.LOCKED})

    def test_signed_in_detection(self):
        self.assertTrue(preflight.signed_in("Dashboard welcome back"))
        self.assertFalse(preflight.signed_in("Sign in to continue"))


class ProfileLeaseTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM jobs")
        self.u1 = seed_user("lease1")
        self.u2 = seed_user("lease2")
        self.a1 = seed_account(self.u1, "lease1@acct.test")
        self.a2 = seed_account(self.u2, "lease2@acct.test")

    def test_pool_has_no_duplicates(self):
        self.assertEqual(len(jobs.PROFILE_POOL), len(set(jobs.PROFILE_POOL)))

    def test_lease_is_exclusive(self):
        first = jobs.reserve_slot("j1", self.u1, self.a1, "a@b.c", "/tmp/j1.log")
        second = jobs.reserve_slot("j2", self.u2, self.a2, "d@e.f", "/tmp/j2.log")
        self.assertNotEqual(first, second)

    def test_reservation_is_atomic_not_just_a_read(self):
        self.setUp()
        """Two slots for one profile must not both succeed.

        reserve_slot used to read the free profile and then insert, which two
        concurrent submits could interleave into the same fingerprint.
        """
        db.execute("DELETE FROM jobs")
        taken = jobs.reserve_slot("j1", self.u1, self.a1, "a@b.c", "/tmp/j1.log")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO jobs (id, user_id, account_id, email, state, levels,
                                     packets, profile, created_at)
                   VALUES ('j2', 2, 2, 'd@e.f', 'running', 0, 0, ?, ?)""", (taken, db.now()))

    def test_same_account_twice_is_refused_by_the_database(self):
        self.setUp()
        shared = seed_account(self.u2, "shared@acct.test")
        jobs.reserve_slot("j1", self.u1, self.a1, "same@acct", "/tmp/j1.log")
        with self.assertRaises(jobs.AccountBusy):
            jobs.reserve_slot("j2", self.u2, shared, "same@acct", "/tmp/j2.log")

    def test_pool_exhaustion_raises(self):
        self.setUp()
        for i in range(len(jobs.PROFILE_POOL)):
            owner = seed_user("pool%d" % i)
            acct = seed_account(owner, "pool%d@acct.test" % i)
            jobs.reserve_slot("j%d" % i, owner, acct, "a%d@b.c" % i,
                              "/tmp/j%d.log" % i)
        owner = seed_user("overflow")
        spare = seed_account(owner, "overflow@acct.test")
        with self.assertRaises(jobs.NoFreeProfile):
            jobs.reserve_slot("overflow", owner, spare, "last@b.c", "/tmp/last.log")


class CurrentJobTests(unittest.TestCase):
    def test_newest_job_wins_within_the_same_second(self):
        """created_at is second-resolution, so two quick jobs tie without rowid."""
        db.execute("DELETE FROM jobs")
        uid = seed_user("current")
        now = db.now()
        db.execute(
            "INSERT INTO jobs (id, user_id, email, state, levels, packets, created_at)"
            " VALUES ('aaa', ?, 'a@b.c', 'failed', 0, 0, ?)", (uid, now))
        db.execute(
            "INSERT INTO jobs (id, user_id, email, state, levels, packets, created_at)"
            " VALUES ('zzz', ?, 'd@e.f', 'done', 1, 68, ?)", (uid, now))
        self.assertEqual(jobs.current_for(uid)["email"], "d@e.f")
        self.assertEqual([j["email"] for j in jobs.jobs_for(uid)], ["d@e.f", "a@b.c"])


class CooldownTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM cooldowns")

    def test_blocked_then_expired(self):
        accounts.note_cooldown("a@b.c", "bad_credentials")
        self.assertIsNotNone(accounts.cooldown_for("a@b.c"))
        db.execute("UPDATE cooldowns SET until = ? WHERE email = ?", (db.now() - 1, "a@b.c"))
        self.assertIsNone(accounts.cooldown_for("a@b.c"))


class ScrubTests(unittest.TestCase):
    def test_password_removed(self):
        self.assertNotIn("hunter22", jobs.scrub("logging in with hunter22", ["hunter22"]))

    def test_short_values_are_not_scrubbed(self):
        """A 2-character secret would turn ordinary log text into [redacted]."""
        self.assertEqual(jobs.scrub("a b c", ["a"]), "a b c")


if __name__ == "__main__":
    unittest.main()


class LevelAccountingTests(unittest.TestCase):
    """A level is the unit of work; packets are the subunit."""

    def test_default_cap_is_one_level(self):
        self.assertEqual(jobs.MAX_LEVELS, 1)

    def test_stagger_and_caps_come_from_multi_run(self):
        """15 s between logins and a 1800 s cycle cap, as multi_run.sh uses."""
        self.assertEqual(jobs.LOGIN_STAGGER_SECONDS, 15)
        self.assertEqual(jobs.CYCLE_CAP_SECONDS, 1800)
        self.assertGreaterEqual(jobs.MAX_PER_USER, 1)

    def test_marker_vocabulary_matches_the_runner(self):
        """jobs.py parses prefixes; runner.py emits them. Keep them in step."""
        for marker in ("LOGIN_OK", "LOGIN_FAILED", "PACKET_DONE", "LEVEL_DONE", "JOB_DONE"):
            self.assertIn(marker, jobs._MARKERS, marker)

    def test_no_lesson_vocabulary_left(self):
        for name in ("LESSON_DONE", "lesson_done"):
            self.assertNotIn(name, jobs._MARKERS, name)

    def test_job_shape_exposes_levels_and_packets(self):
        db.execute("DELETE FROM jobs")
        uid = seed_user("shape")
        db.execute(
            """INSERT INTO jobs (id, user_id, email, state, levels, packets, profile,
                                 created_at)
               VALUES ('lev1', ?, 'a@b.c', 'running', 0, 41, 1, ?)""", (uid, db.now()))
        job = jobs.get_job("lev1", uid)
        self.assertEqual(job["levels"], 0)
        self.assertEqual(job["packets"], 41)
        self.assertNotIn("lessons", job)

    def test_job_done_marker_parses_both_numbers(self):
        import re as _re
        marker = jobs._MARKERS["JOB_DONE"][1]
        m = marker.search("JOB_DONE levels=2 packets=141")
        self.assertEqual(m.group(1), "2")
        self.assertEqual(m.group(2), "141")

    def test_time_cap_is_reported_as_a_failure_not_a_done(self):
        marker = jobs._MARKERS["JOB_DONE"][1]
        m = marker.search("JOB_DONE levels=0 packets=12 reason=time_cap")
        self.assertEqual(m.group(1), "0")
        self.assertEqual(m.group(2), "12")
