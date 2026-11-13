#!/usr/bin/env python3
"""server_lite.py — single-operator cloud panel: add accounts, watch jobs.

The full panel's account system exists for many strangers signing up and
minting keys to each other. The lite panel assumes exactly one operator (you):
no users, no signup, no keys, no sessions, no CSRF — and no run-key gate.

What replaces all of that is environmental:

  * Codespaces: a PRIVATE forwarded port is visible only to your GitHub
    session, so the panel needs no token at all.
  * A public tunnel (ngrok): set PANEL_LITE_TOKEN (or let run.sh generate one
    and print it once). The browser asks for it once, stores it in
    localStorage, and sends it as X-Token on every request. With no token set
    the API is open — only do that behind a private port.

Everything else is inherited deliberately: the same runner, the same stdout
marker contract, the same login classification (bad_credentials and locked
never retry — jobctl writes a per-address cooldown instead).
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import botwatch
import jobctl

HERE = Path(__file__).resolve().parent
TOKEN = os.environ.get("PANEL_LITE_TOKEN", "").strip()


@contextlib.asynccontextmanager
async def lifespan(_app):
    # On SIGTERM (stop.sh sends it first, for exactly this): TERM every
    # active run's process group so the runner AND its browser exit cleanly
    # instead of being orphaned by a hard kill.
    yield
    ctl.shutdown()


app = FastAPI(title="speexx-lite", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
ctl = jobctl.JobCtl()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── host allowlist (same idea as the full panel's auth.host_allowed) ────────
_default_hosts = "127.0.0.1,localhost,::1"
_extra = os.environ.get("SPEEXX_LITE_ALLOWED_HOSTS", "")
ALLOWED = [h.strip().lower() for h in (_default_hosts + ("," + _extra if _extra else "")).split(",") if h.strip()]
if os.environ.get("CODESPACES"):
    ALLOWED.append("*.app.github.dev")


def host_allowed(hostname: str) -> bool:
    hostname = (hostname or "").lower()
    for entry in ALLOWED:
        if entry.startswith("*."):
            if hostname.endswith(entry[1:]) or hostname == entry[2:]:
                return True
        elif hostname == entry:
            return True
    return False


@app.middleware("http")
async def guard(request: Request, call_next):
    hostname = request.headers.get("host", "").split(":", 1)[0]
    if not host_allowed(hostname):
        return JSONResponse({"error": "unknown host"}, status_code=400)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _authorized(request: Request) -> bool:
    if not TOKEN:
        return True
    return request.headers.get("X-Token", "") == TOKEN


class TokenRequired(Exception):
    pass


@app.exception_handler(TokenRequired)
async def token_required(request, exc):
    return JSONResponse({"error": "token"}, status_code=401)


# ── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/state")
async def state(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    payload = ctl.state()
    payload["access"] = {"token_required": bool(TOKEN)}
    return payload


@app.post("/api/accounts")
async def add_account(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    body = await request.json()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    if not EMAIL_RE.match(email) or len(email) > 254:
        return JSONResponse({"error": "That does not look like a Speexx address."},
                            status_code=400)
    if not password or len(password) > 200:
        return JSONResponse({"error": "A password is required."}, status_code=400)
    try:
        jobctl.seal(email, password)  # prove the vault works before saving
        account_id = ctl.add_account(email, password)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not seal the password."}, status_code=500)
    return {"id": account_id}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    try:
        ok = ctl.delete_account(account_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"deleted": ok} if ok else JSONResponse(
        {"error": "No such account."}, status_code=404)


@app.post("/api/jobs/start")
async def start_job(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    body = await request.json()
    try:
        job_id = ctl.start_job(int(body.get("account_id", 0)))
    except LookupError:
        return JSONResponse({"error": "No such account."}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=429)
    except Exception:
        return JSONResponse({"error": "Could not start the run."}, status_code=500)
    return {"id": job_id}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    return {"stopped": ctl.stop_job(job_id)}


@app.get("/api/jobs/{job_id}/log")
async def job_log(job_id: int, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    return {"id": job_id, "log": ctl.log_tail(job_id)}


@app.get("/api/bot")
async def bot_sessions(request: Request):
    """Read-only view of the AutoCycle bot's own supervisor sessions (its
    per-session log files). The bot runs outside this panel's job table; this
    is how the page shows everything that is happening on the box."""
    if not _authorized(request):
        return JSONResponse({"error": "token"}, status_code=401)
    return botwatch.sessions()


# ── static ───────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=HERE / "static"), name="static")
