#!/usr/bin/env python3
"""import_accounts.py — seed the lite panel's account list from an
Accounts.md-format file (same two formats multi_run.sh accepts).

Deliberately does NOT instantiate jobctl.JobCtl(): that class's boot() marks
active jobs stopped, and the panel server may be running with live jobs right
now. This script only touches the accounts table through its own connection.

Passwords are sealed (AES-256-GCM) exactly like the panel itself seals them;
nothing is printed except the email and a count.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import jobctl


def parse_accounts(path: Path):
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    pairs, i = [], 0
    while i < len(lines):
        if not lines[i] or lines[i].startswith("#"):
            i += 1
            continue
        parts = lines[i].split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
            i += 1
        elif i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith("#"):
            pairs.append((lines[i], lines[i + 1]))
            i += 2
        else:
            print("[import] line %d has no password, skipping" % (i + 1))
            i += 1
    return pairs


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: import_accounts.py <accounts-file>")
        return 1
    path = Path(sys.argv[1])
    if not path.is_file():
        print("[import] no such file: %s" % path)
        return 1
    pairs = parse_accounts(path)
    if not pairs:
        print("[import] no accounts found in %s" % path)
        return 1

    jobctl.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(jobctl.DB_PATH, timeout=10)
    db.execute(jobctl._SCHEMAS[0])  # make sure the table exists even pre-boot
    added = skipped = 0
    import time
    for email, password in pairs:
        if "@" not in email:
            print("[import] %r does not look like an email, skipping" % email)
            continue
        exists = db.execute("SELECT id FROM accounts WHERE email=?",
                            (email,)).fetchone()
        if exists:
            skipped += 1
            continue
        db.execute(
            "INSERT INTO accounts (email, secret, created) VALUES (?,?,?)",
            (email, jobctl.seal(email, password), int(time.time())))
        added += 1
        print("[import] saved %s" % email)
    db.commit()
    db.close()
    print("[import] done: %d added, %d already saved" % (added, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
