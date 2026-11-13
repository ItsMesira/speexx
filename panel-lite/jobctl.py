#!/usr/bin/env python3
"""jobctl.py — the lite panel's own small orchestration layer.

Spawns panel-lite/runner.py children (the same runner the full panel uses),
parses the stdout markers contract (LOGIN_OK / LOGIN_FAILED / PACKET_DONE /
LEVEL_DONE / JOB_DONE), keeps one active job per Speexx address, leases device
profiles 1..5 then 0, and enforces the 15 s login stagger.

Deliberately simpler than the full panel's jobs.py: no user ids (single
operator), no key machine, no audit trail. What it keeps from the full panel,
because it prevents real incidents:

  * bad_credentials / locked never retry — a per-address cooldown instead
    (15 min / 60 min), keyed by the Speexx address like the portal's lockout;
  * credentials reach the child on stdin, never argv;
  * one active job per address (the portal allows one session per account).

State lives in lite_data/lite.db; per-job output in lite_data/logs/<id>.log.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPEEXX_LITE_DATA_DIR", HERE / "lite_data"))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "lite.db"
VAULT_KEY_PATH = DATA_DIR / "vault.key"

STAGGER = int(os.environ.get("SPEEXX_LITE_STAGGER", "15"))
CYCLE_CAP = int(os.environ.get("SPEEXX_LITE_CYCLE_CAP", "1800"))
MAX_PER_ADDRESS = 1
COOLDOWN = {"bad_credentials": 15 * 60, "locked": 60 * 60}
PROFILE_POOL = [int(x) for x in
                os.environ.get("SPEEXX_LITE_PROFILE_POOL", "1,2,3,4,5,0").split(",")]

_SCHEMAS = [
    """CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        secret BLOB NOT NULL,
        created INTEGER NOT NULL,
        cooldown_until INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        profile INTEGER,
        state TEXT NOT NULL,
        started INTEGER NOT NULL,
        ended INTEGER,
        levels INTEGER NOT NULL DEFAULT 0,
        packets INTEGER NOT NULL DEFAULT 0,
        pid INTEGER,
        exit_code INTEGER,
        reason TEXT)""",
]

# ── AES-256-GCM seal/open (same approach as the full panel's vault.py) ──────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - deps installed by run.sh
    AESGCM = None


def _vault_key() -> bytes:
    env = os.environ.get("SPEEXX_PANEL_VAULT_KEY", "").strip()
    if env:
        return bytes.fromhex(env)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        VAULT_KEY_PATH.chmod(0o600)
    except OSError:
        pass
    if VAULT_KEY_PATH.exists():
        return VAULT_KEY_PATH.read_bytes()
    key = AESGCM.generate_key(bit_length=256)
    VAULT_KEY_PATH.write_bytes(key)
    VAULT_KEY_PATH.chmod(0o600)
    return key


def seal(email: str, password: str) -> bytes:
    import os as _os
    key = _vault_key()
    nonce = _os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, password.encode("utf-8"),
                                       ("lite:%s" % email).encode("utf-8"))


def unseal(email: str, blob: bytes) -> str:
    key = _vault_key()
    nonce, body = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, body, ("lite:%s" % email).encode("utf-8")).decode("utf-8")


class JobCtl:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            DATA_DIR.chmod(0o700)
        except OSError:
            pass
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        for schema in _SCHEMAS:
            self.db.execute(schema)
        self.db.commit()
        self.lock = threading.Lock()
        self.last_spawn = 0.0
        self._threads = {}
        self.boot()

    # ── lifecycle ───────────────────────────────────────────────────────
    def boot(self):
        """Any job still marked active belongs to a process the previous server
        may have left: TERM its process group (runner + its Chromium share one
        group), then mark it stopped. Rows are marked BEFORE the kill so the
        watcher cannot resurrect them as 'failed exit -15'."""
        rows = self.db.execute(
            "SELECT id, pid FROM jobs WHERE state IN ('starting','running')").fetchall()
        if rows:
            now = int(time.time())
            with self.lock:
                self.db.execute(
                    "UPDATE jobs SET state='stopped', ended=?, reason='reboot' "
                    "WHERE state IN ('starting','running')", (now,))
                self.db.commit()
            for row in rows:
                if row["pid"]:
                    try:
                        os.killpg(row["pid"], 15)
                    except Exception:
                        pass

    def shutdown(self):
        """uvicorn calls this on SIGTERM: TERM every active job's process group
        so the runner and its browser exit cleanly. Marks the rows stopped
        BEFORE signalling — same ordering as stop_job — so the watcher thread
        cannot finalize a killed child as 'failed exit -15'."""
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET state='stopped', ended=?, reason='server shutdown' "
                "WHERE state IN ('starting','running')", (now,))
            self.db.commit()
        rows = self.db.execute(
            "SELECT id, pid FROM jobs WHERE state='stopped' AND reason='server shutdown' "
            "AND ended=?", (now,)).fetchall()
        for row in rows:
            if row["pid"]:
                try:
                    os.killpg(row["pid"], 15)
                except Exception:
                    pass
        deadline = time.time() + 3
        # Wait for the per-job watcher threads to finalize (they record the
        # child's exit code); the rows are already stopped, so this only
        # bounds how long a wedged child can delay the shutdown.
        while time.time() < deadline:
            with self.lock:
                watchers = list(self._threads.values())
            if not any(t.is_alive() for t in watchers):
                break
            time.sleep(0.2)
        for t in list(self._threads.values()):
            t.join(timeout=1)
        for row in self.db.execute(
                "SELECT id, pid FROM jobs WHERE pid IS NOT NULL AND "
                "state IN ('starting','running')").fetchall():
            if row["pid"]:
                try:
                    os.killpg(row["pid"], 9)
                except Exception:
                    pass

    # ── accounts ────────────────────────────────────────────────────────
    def add_account(self, email: str, password: str) -> int:
        with self.lock:
            exists = self.db.execute("SELECT id FROM accounts WHERE email=?",
                                     (email,)).fetchone()
            if exists:
                raise ValueError("That Speexx address is already saved.")
            cur = self.db.execute(
                "INSERT INTO accounts (email, secret, created) VALUES (?,?,?)",
                (email, seal(email, password), int(time.time())))
            self.db.commit()
            return cur.lastrowid

    def delete_account(self, account_id: int) -> bool:
        with self.lock:
            active = self.db.execute(
                "SELECT id FROM jobs WHERE account_id=? AND state IN ('starting','running')",
                (account_id,)).fetchone()
            if active:
                raise ValueError("Stop the run before deleting the account.")
            cur = self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.db.commit()
            return cur.rowcount > 0

    def list_accounts(self):
        now = int(time.time())
        out = []
        for row in self.db.execute(
                "SELECT id, email, created, cooldown_until FROM accounts ORDER BY id"):
            active = self.db.execute(
                "SELECT id, state FROM jobs WHERE account_id=? AND "
                "state IN ('starting','running')", (row["id"],)).fetchone()
            out.append({
                "id": row["id"],
                "email": row["email"],
                "created": row["created"] * 1000,
                "job": {"id": active["id"], "state": active["state"]} if active else None,
                "cooldown": max(0, row["cooldown_until"] - now),
            })
        return out

    def _credentials(self, account_id: int):
        row = self.db.execute("SELECT email, secret FROM accounts WHERE id=?",
                              (account_id,)).fetchone()
        if not row:
            raise LookupError("no such account")
        return row["email"], unseal(row["email"], row["secret"])

    # ── jobs ────────────────────────────────────────────────────────────
    def start_job(self, account_id: int) -> int:
        now = time.time()
        with self.lock:
            acct = self.db.execute("SELECT email, cooldown_until FROM accounts WHERE id=?",
                                   (account_id,)).fetchone()
            if not acct:
                raise LookupError("no such account")
            if acct["cooldown_until"] > now:
                raise ValueError("This address is cooling down (%d s left). Do not retry."
                                 % (acct["cooldown_until"] - now))
            busy = self.db.execute(
                "SELECT id FROM jobs WHERE email=? AND state IN ('starting','running')",
                (acct["email"],)).fetchone()
            if busy:
                raise ValueError("A run for this address is already active.")
            gap = STAGGER - (now - self.last_spawn)
            if gap > 0:
                raise ValueError("Stagger: wait %d s, then run again." % int(gap + 0.5))
            profile = self._lease_profile()
            self.last_spawn = now

        email, password = self._credentials(account_id)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO jobs (account_id, email, profile, state, started) "
                "VALUES (?,?,?,'starting',?)",
                (account_id, email, profile, int(now)))
            job_id = cur.lastrowid
            self.db.commit()
        log_path = LOG_DIR / ("job%d.log" % job_id)

        cmd = [sys.executable, str(os.environ.get(
            "SPEEXX_LITE_RUNNER", str(HERE / "runner.py"))),
            "--credentials-stdin",
               "--max-levels", "1",
               "--max-seconds", str(CYCLE_CAP),
               "--device-profile", str(profile)]
        logf = open(log_path, "ab")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=logf,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            proc.stdin.write(("%s\n%s\n" % (email, password)).encode("utf-8"))
            proc.stdin.close()
        except Exception:
            logf.close()
            with self.lock:
                self.db.execute("UPDATE jobs SET state='failed', ended=?, "
                                "reason='spawn failed' WHERE id=?",
                                (int(time.time()), job_id))
                self.db.commit()
            raise
        logf.close()
        with self.lock:
            self.db.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, job_id))
            self.db.commit()
        t = threading.Thread(target=self._watch, args=(job_id, proc), daemon=True)
        self._threads[job_id] = t
        t.start()
        return job_id

    def _lease_profile(self) -> int:
        active = {r["profile"] for r in self.db.execute(
            "SELECT profile FROM jobs WHERE profile IS NOT NULL AND "
            "state IN ('starting','running')")}
        for p in PROFILE_POOL:
            if p not in active:
                return p
        raise ValueError("All device profiles are busy; wait for a run to finish.")

    def _watch(self, job_id: int, proc: subprocess.Popen):
        """Waits on the child and applies the stdout markers contract.
        The log lines themselves were already written by the child to the log
        file; this thread only tracks state transitions."""
        state = "starting"
        levels = packets = 0
        reason = None
        log_path = LOG_DIR / ("job%d.log" % job_id)
        seen = 0
        while proc.poll() is None:
            time.sleep(0.5)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(seen)
                    new = f.read()
                    seen = f.tell()
            except OSError:
                new = ""
            for line in new.splitlines():
                if line.startswith("LOGIN_OK"):
                    state = "running"
                elif line.startswith("LOGIN_FAILED"):
                    m = re.search(r"reason=(\w+)", line)
                    reason = (m.group(1) if m else "unknown")
                    state = "failed"
                    if reason in COOLDOWN:
                        self._cooldown(job_id, COOLDOWN[reason])
                elif line.startswith("PACKET_DONE"):
                    packets += 1
                elif line.startswith("LEVEL_DONE"):
                    levels += 1
                elif line.startswith("JOB_DONE"):
                    m = re.search(r"levels=(\d+)", line)
                    if m:
                        levels = int(m.group(1))
                    m = re.search(r"packets=(\d+)", line)
                    if m:
                        packets = int(m.group(1))
                    m = re.search(r"reason=(\w+)", line)
                    if m:
                        reason = m.group(1)
                if state in ("running", "failed"):
                    with self.lock:
                        self.db.execute(
                            "UPDATE jobs SET state=?, levels=?, packets=?, reason=? "
                            "WHERE id=? AND state NOT IN ('stopped')",
                            (state, levels, packets, reason, job_id))
                        self.db.commit()
        code = proc.wait()
        with self.lock:
            if state == "failed":
                final, fin_reason = "failed", reason
            elif code == 0:
                final, fin_reason = "done", reason
            else:
                final, fin_reason = "failed", reason or ("exit %d" % code)
            row = self.db.execute("SELECT state, reason FROM jobs WHERE id=?",
                                  (job_id,)).fetchone()
            if row and row["state"] == "stopped":
                final, fin_reason = "stopped", row["reason"]
            self.db.execute(
                "UPDATE jobs SET state=?, ended=?, exit_code=?, levels=?, "
                "packets=?, reason=? WHERE id=?",
                (final, int(time.time()), code, levels, packets, fin_reason, job_id))
            self.db.commit()
        self._threads.pop(job_id, None)

    def _cooldown(self, job_id: int, seconds: int):
        email = self.db.execute("SELECT email FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not email:
            return
        with self.lock:
            self.db.execute("UPDATE accounts SET cooldown_until=? WHERE email=?",
                            (int(time.time()) + seconds, email["email"]))
            self.db.commit()

    def stop_job(self, job_id: int) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT pid, state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] not in ("starting", "running"):
                return False
            if row["pid"]:
                try:
                    os.killpg(row["pid"], 15)
                except Exception:
                    pass
            self.db.execute("UPDATE jobs SET state='stopped', ended=?, "
                            "reason='stopped by operator' WHERE id=?",
                            (int(time.time()), job_id))
            self.db.commit()
        return True

    def list_jobs(self, limit=50):
        out = []
        for row in self.db.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)):
            out.append({
                "id": row["id"], "email": row["email"], "profile": row["profile"],
                "state": row["state"], "started": row["started"] * 1000,
                "ended": (row["ended"] * 1000) if row["ended"] else None,
                "levels": row["levels"], "packets": row["packets"],
                "reason": row["reason"],
            })
        return out

    def log_tail(self, job_id: int, lines=200) -> str:
        path = LOG_DIR / ("job%d.log" % job_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def state(self) -> dict:
        return {
            "accounts": self.list_accounts(),
            "jobs": self.list_jobs(),
            "caps": {"stagger": STAGGER, "cycle_cap": CYCLE_CAP,
                     "max_levels": 1},
        }


if __name__ == "__main__":
    print(json.dumps(JobCtl().state(), indent=2))
