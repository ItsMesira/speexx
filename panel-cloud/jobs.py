"""Job supervision.

Owns everything about a running bot: which device profile it holds, its process
group, its log, and its state machine. Four decisions worth stating.

* Credentials are decrypted only in memory, only for the moment of spawning, and
  are used a second time to scrub whatever gets persisted. They are never
  written to the database, a log, `argv` or a URL. The encrypted copy lives in
  `speexx_accounts.secret` and comes from `vault.py`.

* Children are spawned with `start_new_session=True`, so each job owns a
  process group. Stop signals that group and nothing else. A `pkill -f` would be
  simpler and wrong: the sibling projects' stop scripts match exactly those
  patterns and would take out other bots.

* **Logins are staggered 15 s apart**, taken from the sibling projects'
  `multi_run.sh`. Six browsers authenticating in the same second look like an
  attack, and the cost of an IP ban lands on the whole service. This serialises
  the moment a job may start, not the whole run.

* **Concurrency is capped twice**: globally by device-profile leases, and per
  user. The profile pool is six; without the per-user cap one person could hold
  every slot and lock everyone else out.
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

import accounts
import db
import keys

HERE = Path(__file__).resolve().parent
# Overridable so the integration test can drive the whole supervision path with
# a stub instead of a real browser. A live login attempt against a real account
# must never happen by accident.
RUNNER = Path(os.environ.get("SPEEXX_PANEL_RUNNER") or (HERE / "runner.py"))
# From db, not from HERE: SPEEXX_PANEL_DATA_DIR has to win, or a run with
# a relocated data dir writes its log into the default one instead.
LOG_DIR = db.DATA_DIR / "logs"

MAX_LEVELS = 1
STOP_GRACE_SECONDS = 10.0
LOG_TAIL_LINES = 400

#: Seconds a job may run before it is stopped and reported as a time cap.
CYCLE_CAP_SECONDS = int(os.environ.get("SPEEXX_PANEL_CYCLE_CAP", "1800"))
#: Minimum gap between two jobs reaching the login form.
LOGIN_STAGGER_SECONDS = int(os.environ.get("SPEEXX_PANEL_STAGGER", "15"))
#: How many jobs one person may have in flight.
MAX_PER_USER = int(os.environ.get("SPEEXX_PANEL_MAX_PER_USER", "2"))

#: Preferred first, 0 last. Speexx allows one account per device; profile 0 is
#: "change nothing", so it is a valid last resort rather than a first choice.
PROFILE_POOL = [int(p) for p in os.environ.get(
    "SPEEXX_PANEL_PROFILE_POOL", "1,2,3,4,5,0"
).split(",") if p.strip().isdigit()]

ACTIVE = ("queued", "starting", "running")
TERMINAL = ("done", "failed", "stopped")

# job_id -> password, so the reader thread can scrub the log it persists.
_SCRUB: dict[str, str] = {}
_SCRUB_LOCK = threading.Lock()
_login_lock = threading.Lock()
_last_login_at = 0.0

_MARKERS = {
    "LOGIN_OK": ("login_ok", None),
    "LOGIN_FAILED": ("login_failed", re.compile(r"reason=(\w+)")),
    "PACKET_DONE": ("packet_done", re.compile(r"n=(\d+)\s+packet=(\S+)")),
    "LEVEL_DONE": ("level_done", re.compile(r"n=(\d+)\s+packets=(\d+)")),
    "JOB_DONE": ("job_done", re.compile(r"levels=(\d+)\s+packets=(\d+)")),
}


class NeedsKey(Exception):
    """No level credit. The person can sign in and save logins, but not run."""

    def __init__(self):
        super().__init__("you need a key from the admin to run")


class NoFreeProfile(Exception):
    pass


class AccountBusy(Exception):
    def __init__(self, email: str):
        super().__init__(email)
        self.email = email


class TooManyJobs(Exception):
    pass


class AccountBlocked(Exception):
    def __init__(self, until: int, reason: str):
        super().__init__(reason)
        self.until, self.reason = until, reason


# ── Leases ────────────────────────────────────────────────────────────────

def active_jobs(user_id: int | None = None):
    if user_id is None:
        return db.query(
            "SELECT id, user_id, email, profile, state, pgid FROM jobs"
            " WHERE state IN ('queued','starting','running')")
    return db.query(
        "SELECT id, user_id, email, profile, state, pgid FROM jobs"
        " WHERE user_id = ? AND state IN ('queued','starting','running')",
        (int(user_id),))


def lease_profile() -> int:
    """A free profile. Read-only; reservation is the INSERT. For tests only."""
    taken = {r["profile"] for r in active_jobs() if r["profile"] is not None}
    for candidate in PROFILE_POOL:
        if candidate not in taken:
            return candidate
    raise NoFreeProfile("every device profile is busy")


def reserve_slot(job_id: str, user_id: int, account_id: int, email: str,
                 log_path: str) -> int:
    """Insert the job row, claiming a device profile atomically.

    The insert is the reservation. A unique-index violation means the slot is
    already gone, so try the next profile. Read-then-write would let two
    simultaneous starts pick the same profile, and two accounts sharing one
    fingerprint is what Speexx punishes.
    """
    live = db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND state IN ('queued','starting','running')",
        (int(user_id),))["n"]
    if live >= MAX_PER_USER:
        raise TooManyJobs("you already have %d runs going" % live)

    last_error = None
    for candidate in PROFILE_POOL:
        try:
            db.execute(
                """INSERT INTO jobs
                       (id, user_id, account_id, email, state, levels, packets,
                        profile, log_path, created_at, started_at)
                   VALUES (?, ?, ?, ?, 'starting', 0, 0, ?, ?, ?, ?)""",
                (job_id, int(user_id), int(account_id), email[:120], candidate,
                 log_path, db.now(), db.now()),
            )
            return candidate
        except sqlite3.IntegrityError as exc:
            last_error = exc
            message = str(exc)
            # SQLite names the column, not the partial index, in this message.
            if "jobs.email" in message or "jobs_one_active_per_account" in message:
                raise AccountBusy(email) from exc
            continue
    raise NoFreeProfile("every device profile is busy") from last_error


def _await_login_slot() -> None:
    """Hold the 15 s gap from multi_run.sh between jobs reaching the portal."""
    global _last_login_at
    with _login_lock:
        wait = _last_login_at + LOGIN_STAGGER_SECONDS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_login_at = time.time()


# ── Log tail ──────────────────────────────────────────────────────────────

def tail(path: str | Path | None, limit: int = LOG_TAIL_LINES) -> list[list[str]]:
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines()[-limit:]:
        kind = "plain"
        if line.startswith(("LOGIN_OK", "PACKET_DONE", "LEVEL_DONE")):
            kind = "ok"
        elif line.startswith("LOGIN_FAILED"):
            kind = "bad"
        elif line.startswith("JOB_DONE"):
            kind = "done"
        out.append([kind, line])
    return out


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


# ── Spawning ──────────────────────────────────────────────────────────────

def start_job(user_id: int, account_id: int, max_levels: int = MAX_LEVELS) -> dict:
    """Start one run, spending one level credit.

    Without a credit the refusal is `needs_key`: explicit, and free of detail,
    because "you may not run" should not also tell a stranger what else they are
    missing.
    """
    # The gate, before anything expensive or observable happens.
    if not keys.has_access(user_id):
        raise NeedsKey()

    owned = accounts.get_owned(user_id, account_id)
    blocked = accounts.cooldown_for(owned["email"] if owned else "")
    if blocked:
        raise AccountBlocked(blocked["until"], blocked["reason"])

    email, password = accounts.credentials(user_id, account_id)

    job_id = uuid.uuid4().hex[:12]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    # Server-generated name: no user input reaches a filesystem path, so there
    # is no traversal surface here.
    log_path = LOG_DIR / ("job-%s.log" % job_id)

    # One key, one level. The credit is spent here, before the browser opens, so
    # a run stopped early has still cost the person their key -- that was the
    # decision, and it is why stopping is not a free way to explore.
    spent_key = keys.consume(user_id)

    try:
        profile = reserve_slot(job_id, user_id, account_id, email, str(log_path))
    except Exception:
        # Nothing started, so nothing should have been charged. This only ever
        # undoes the row this call just moved, in this call.
        keys.refund(user_id, spent_key)
        raise

    _await_login_slot()
    accounts.touch(user_id, account_id, job_started=True)

    cmd = [
        os.environ.get("SPEEXX_PANEL_PYTHON", str(HERE / ".venv" / "bin" / "python")),
        str(RUNNER),
        "--credentials-stdin",
        "--max-levels", str(int(max_levels)),
        "--max-seconds", str(CYCLE_CAP_SECONDS),
        "--device-profile", str(profile),
    ]
    if os.environ.get("SPEEXX_PANEL_FAST") == "1":
        cmd.append("--fast")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(HERE),
            text=True,
            bufsize=1,
            # Own session => own process group => stop can signal the child and
            # its Chromium together, and nothing else.
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception as exc:
        db.execute("UPDATE jobs SET state='failed', reason=?, ended_at=? WHERE id=?",
                   ("spawn failed: %s" % exc, db.now(), job_id))
        raise

    db.execute("UPDATE jobs SET pgid=? WHERE id=?", (proc.pid, job_id))
    with _SCRUB_LOCK:
        _SCRUB[job_id] = password
    _supervise(job_id, proc, user_id, account_id, email, password, str(log_path))
    return get_job(job_id, user_id) or {"id": job_id}


def _supervise(job_id: str, proc: subprocess.Popen, user_id: int, account_id: int,
               email: str, password: str, log_path: str) -> None:
    """Read the child's stdout, update state, keep secrets out of the log."""

    def run() -> None:
        secrets = [password]
        state = "running"
        reason = None
        levels = packets = 0

        try:
            with open(log_path, "a", encoding="utf-8") as sink:
                # stdin closes immediately after the credentials so the child's
                # read() returns; the pipe never lingers holding a secret.
                proc.stdin.write("%s\n%s\n" % (email, password))
                proc.stdin.close()
                for raw in proc.stdout:
                    line = scrub(raw.rstrip("\n"), secrets)
                    sink.write(line + "\n")
                    sink.flush()

                    for marker, (kind, pattern) in _MARKERS.items():
                        if not line.startswith(marker):
                            continue
                        if kind == "login_ok":
                            db.execute("UPDATE jobs SET state='running' WHERE id=?",
                                       (job_id,))
                            accounts.touch(user_id, account_id)
                        elif kind == "login_failed":
                            m = pattern.search(line)
                            state = "failed"
                            reason = m.group(1) if m else "unknown"
                            db.execute(
                                "UPDATE jobs SET state='failed', reason=?, ended_at=? WHERE id=?",
                                (reason, db.now(), job_id))
                        elif kind == "packet_done":
                            m = pattern.search(line)
                            if m:
                                packets = int(m.group(1))
                                db.execute("UPDATE jobs SET packets=? WHERE id=?",
                                           (packets, job_id))
                        elif kind == "level_done":
                            m = pattern.search(line)
                            if m:
                                levels = int(m.group(1))
                            db.execute("UPDATE jobs SET levels=? WHERE id=?", (levels, job_id))
                        elif kind == "job_done":
                            m = pattern.search(line)
                            if m:
                                levels, packets = int(m.group(1)), int(m.group(2))
                            if "reason=time_cap" in line or "reason=no_work" in line:
                                state = "failed"
                                reason = reason or (
                                    "time_cap" if "time_cap" in line else "no_work")
                            else:
                                state = "done"
                            db.execute(
                                "UPDATE jobs SET state=?, levels=?, packets=?, ended_at=? WHERE id=?",
                                (state, levels, packets, db.now(), job_id))
        except Exception:
            pass
        finally:
            code = proc.wait()
            with _SCRUB_LOCK:
                _SCRUB.pop(job_id, None)   # drop the last copy we held
            if state not in TERMINAL:
                if code == 3:
                    state, reason = "failed", reason or "bad_credentials"
                elif code == 0:
                    state = "done"
                else:
                    state, reason = "failed", reason or ("exit %d" % code)
                db.execute(
                    "UPDATE jobs SET state=?, reason=?, levels=?, packets=?, ended_at=? WHERE id=?",
                    (state, reason, levels, packets, db.now(), job_id))
            if state == "failed" and reason in accounts.COOLDOWN_SECONDS:
                accounts.note_cooldown(email, reason)
            db.audit(user_id, "job_%s" % state,
                     "%s levels=%d packets=%d" % (job_id, levels, packets))

    threading.Thread(target=run, name="job-%s" % job_id, daemon=True).start()


# ── Control ───────────────────────────────────────────────────────────────

def stop_job(job_id: str, user_id: int | None = None) -> bool:
    """Scoped to the owner when `user_id` is given; never a bare id lookup."""
    if user_id is None:
        row = db.one("SELECT pgid, state FROM jobs WHERE id = ?", (job_id,))
    else:
        row = db.one("SELECT pgid, state FROM jobs WHERE id = ? AND user_id = ?",
                     (job_id, int(user_id)))
    if not row or row["state"] not in ACTIVE:
        return False
    pgid = row["pgid"]
    if not pgid:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.time() + STOP_GRACE_SECONDS
    while time.time() < deadline:
        current = db.one("SELECT state FROM jobs WHERE id = ?", (job_id,))["state"]
        if current not in ACTIVE:
            return True
        time.sleep(0.4)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    db.execute("UPDATE jobs SET state='stopped', ended_at=? WHERE id=?", (db.now(), job_id))
    return True


def reap_orphans() -> int:
    """Terminate anything still marked running from a previous panel process."""
    reaped = 0
    for row in active_jobs():
        if row["pgid"]:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(row["pgid"], sig)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(1.0)
        db.execute(
            "UPDATE jobs SET state='stopped', reason='panel restarted', ended_at=? WHERE id=?",
            (db.now(), row["id"]))
        reaped += 1
    return reaped


# ── Reads ─────────────────────────────────────────────────────────────────

def _shape(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "state": row["state"],
        "levels": row["levels"],
        "packets": row["packets"],
        "profile": row["profile"],
        "reason": row["reason"],
        "created_at": (row["created_at"] * 1000) if row["created_at"] else None,
        "started_at": (row["started_at"] * 1000) if row["started_at"] else None,
        "ended_at": (row["ended_at"] * 1000) if row["ended_at"] else None,
    }


def get_job(job_id: str, user_id: int | None = None) -> dict | None:
    """Owner-scoped. Pass `user_id` for anything a user can reach."""
    if user_id is None:
        row = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    else:
        row = db.one("SELECT * FROM jobs WHERE id = ? AND user_id = ?",
                     (job_id, int(user_id)))
    if not row:
        return None
    job = _shape(row)
    job["log"] = tail(row["log_path"])
    return job


def jobs_for(user_id: int, limit: int = 30) -> list[dict]:
    rows = db.query(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (int(user_id), int(limit)))
    return [_shape(r) for r in rows]


def jobs_all(limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        job = _shape(r)
        job["user_id"] = r["user_id"]
        # The owner's *name*, not an address: the panel stopped storing one.
        owner = (db.one("SELECT username FROM users WHERE id = ?", (r["user_id"],))
                 if r["user_id"] else None)
        job["owner"] = owner["username"] if owner else "—"
        out.append(job)
    return out


def current_for(user_id: int):
    row = db.one(
        """SELECT * FROM jobs WHERE user_id = ?
             AND state IN ('queued','starting','running')
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (int(user_id),))
    if not row:
        row = db.one(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (int(user_id),))
    return get_job(row["id"], user_id) if row else None


def cooldowns() -> list[dict]:
    rows = db.query(
        "SELECT email, until, reason FROM cooldowns WHERE until > ? ORDER BY until DESC",
        (db.now(),))
    return [{"email": r["email"], "until": r["until"] * 1000, "reason": r["reason"]}
            for r in rows]