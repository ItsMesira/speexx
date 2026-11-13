#!/usr/bin/env python3
"""botwatch.py — surface the AutoCycle bot's live sessions in the lite panel.

The bot (Speexx-sonsole-cloud) runs as its own supervisor with per-session
logs, completely outside this panel's job table. To "show everything", the
panel reads those logs read-only:

  $HOME/speexx_autocycle_multi/session<N>_<local-part>.log.console  (bot stdout)
  $HOME/speexx_autocycle_multi_run.log                              (master)

Emails come from the master log's "Launching session i/N: <email> (profile P,
log: ...)" lines, keyed by the session log name. Never written to, only read.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get(
    "SPEEXX_BOT_LOG_DIR", Path.home() / "speexx_autocycle_multi"))
MASTER_LOG = Path(os.environ.get(
    "SPEEXX_BOT_MASTER_LOG", Path.home() / "speexx_autocycle_multi_run.log"))

LAUNCH_RE = re.compile(
    r"Launching session \d+/\d+:\s+(\S+)\s+\(profile (\d+), log: .*?(session\d+_[^\s/]+)\.log\)")
TAIL_LINES = 12
STALE_AFTER = 180  # seconds without console output -> treat as idle


def _tail(path: Path, lines: int = TAIL_LINES, max_bytes: int = 8192):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            text = f.read()
        return [l for l in text.splitlines() if l.strip()][-lines:]
    except OSError:
        return []


def _emails_from_master() -> dict:
    """session log name -> email, from the master log's launch lines."""
    out = {}
    try:
        for line in MASTER_LOG.read_text(encoding="utf-8",
                                         errors="replace").splitlines():
            m = LAUNCH_RE.search(line)
            if m:
                out[m.group(3)] = m.group(1)
    except OSError:
        pass
    return out


def session_state(tail: list, mtime: float) -> str:
    """A rough state pill from the newest console line, so the panel reads at
    a glance. This is display logic only — the bot's own supervision is
    unaffected by what we guess here."""
    if time.time() - mtime > STALE_AFTER:
        return "stopped"
    text = "\n".join(tail)
    if "session finished" in text:
        return "done"
    if "login form detected" in text or "submitted login" in text:
        return "starting"
    if "solver alive" in text or "inject solver" in text or "settled to ord" in text:
        return "running"
    return "starting"


def sessions() -> dict:
    if not LOG_DIR.is_dir():
        return {"present": False, "dir": str(LOG_DIR), "sessions": []}
    emails = _emails_from_master()
    out = []
    for path in sorted(LOG_DIR.glob("session*_*.log.console")):
        name = path.name[: -len(".log.console")]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        tail = _tail(path)
        out.append({
            "name": name,
            "email": emails.get(name, ""),
            "mtime": int(mtime * 1000),
            "state": session_state(tail, mtime),
            "tail": tail,
        })
    # newest activity first
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return {"present": True, "dir": str(LOG_DIR), "sessions": out}
