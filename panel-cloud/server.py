"""HTTP surface.

The security posture, and why each piece is here even though the service binds to
127.0.0.1 by default:

* **Host validation on every request.** A loopback service is still reachable from
  any page the user has open, and DNS rebinding lets a hostile page aim its own
  hostname at loopback. Agents most often skip this.
* **CSRF token plus Origin check** on every state-changing request.
* **Every user-reachable query is scoped by `user_id` in the SQL.** The main risk
  in a multi-user service is not a weak password, it is one missing
  `WHERE user_id = ?` handing someone else's data over. `require_user()` is the
  only way in, and it hands the id to the query rather than trusting the route.
* **No inline script or style anywhere**, so the CSP can stay at 'self'.

Sign-in answers are deliberately uniform: every failure returns the same 401
shape with a `code`, and the address-based decoy hash in `users.authenticate`
keeps the timing flat whether or not the account exists.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import accounts
import auth
import db
import jobs
import keys
import users

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: Per-user sign-in throttling on top of the per-IP one.
LOGIN_PER_USER = (10, 900)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    reaped = jobs.reap_orphans()
    users.purge_expired()
    generated = users.bootstrap_admin()
    if generated:
        print("\n  admin name     : %s"
              % (os.environ.get("SPEEXX_PANEL_ADMIN_USERNAME") or "admin"))
        print("  admin password : %s   (shown once)\n" % generated)
    elif users.no_admin_exists():
        print("\n  No admin yet. The next person to sign up owns this panel.\n")
    if reaped:
        print("  reaped %d orphaned job(s) from a previous run" % reaped, flush=True)
    yield


app = FastAPI(
    title="Speexx Panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


# ── Middleware ────────────────────────────────────────────────────────────

def _harden(request: Request, response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    )
    # API answers and hand-written pages are never cached: a stale copy is how
    # a request for a deleted file (an old /app.js) happens long afterwards.
    if request.url.path.startswith("/api/") or request.url.path.endswith((".html", "/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def bad_body(request: Request, exc: RequestValidationError):
    """Answer a malformed body in the panel's own words.

    FastAPI's default is a 422 carrying the field layout, which tells a caller
    exactly what the server expects and differs by input shape. Same message
    for every shape instead.
    """
    return _harden(request, JSONResponse(
        {"error": "That does not look right. Check the fields and try again.",
         "code": "bad_request"}, status_code=400))


@app.middleware("http")
async def guard(request: Request, call_next):
    # Applied to refusals too, so the headers hold on every response.
    if not auth.host_allowed(request.headers.get("host")):
        return _harden(request, JSONResponse({"error": "unknown host"}, status_code=400))
    try:
        response = await call_next(request)
    except auth.RateLimited as exc:
        return _harden(request, JSONResponse(
            {"error": "too many requests", "code": "rate_limited"},
            status_code=429, headers={"Retry-After": str(exc.retry_after)}))
    return _harden(request, response)


# ── Session helpers ───────────────────────────────────────────────────────

def _cookie(request: Request) -> str | None:
    return request.cookies.get(auth.COOKIE_NAME)


def _session(request: Request) -> dict | None:
    return users.read_session(_cookie(request))


def require_user(request: Request) -> dict:
    """Any signed-in, active user. The only entrance to user-scoped routes."""
    session = _session(request)
    if not session:
        raise HTTPException(status_code=401, detail={"error": "sign in required",
                                                     "code": "no_session"})
    if request.method != "GET" and not auth.origin_allowed(request):
        raise HTTPException(status_code=403, detail={"error": "bad origin",
                                                     "code": "bad_origin"})
    if request.method != "GET":
        token = request.headers.get("x-csrf-token", "")
        if not token or token != session["csrf"]:
            raise HTTPException(status_code=403, detail={"error": "bad csrf token",
                                                         "code": "bad_csrf"})
    return session


def require_admin(request: Request) -> dict:
    session = require_user(request)
    if session["role"] != "admin":
        raise HTTPException(status_code=403, detail={"error": "admin only",
                                                     "code": "not_admin"})
    return session


# ── Models ────────────────────────────────────────────────────────────────

class SignupIn(BaseModel):
    # Deliberately loose. Field-level length rules would answer 422 before the
    # account code ever ran, and a 422 echoes the field layout back to whoever
    # is probing. users.check_username is the single authority.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class AccountIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=60)


class KeyIn(BaseModel):
    # Loose on purpose: keys.redeem is the single authority, and a strict
    # pattern here would answer 422 and confirm the exact shape being guessed.
    code: str = Field(min_length=1, max_length=32)


class MintIn(BaseModel):
    """Minting needs no code -- the server chooses it. A separate model so the
    redeem shape never has to be faked to make this call."""
    note: str = Field(default="", max_length=80)


class StatusIn(BaseModel):
    # Loose on purpose: users.set_status is the single authority and answers
    # with a 404 for a state it does not know, rather than a 422 that would
    # confirm this admin endpoint exists and differ by input shape.
    status: str = Field(min_length=1, max_length=20)


# ── Auth ──────────────────────────────────────────────────────────────────

@app.post("/api/auth/signup")
def signup(payload: SignupIn, request: Request):
    if not auth.origin_allowed(request):
        raise HTTPException(status_code=403, detail={"error": "bad origin",
                                                     "code": "bad_origin"})
    ident = auth.client_ip(request)
    auth.rate_limit("signup", ident)
    try:
        user_id = users.create_user(payload.username, payload.password)
    except users.AuthError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc),
                                                     "code": exc.code})
    row = users.get_by_id(user_id)
    if row["role"] == "admin":
        message = ("Account created, and it is the first one, so it owns the panel. "
                   "You are the admin.")
    elif row["status"] == "active":
        message = "Account created. You can sign in now."
    else:
        message = "Account created. An admin has to approve it first."
    return {"ok": True, "status": row["status"], "role": row["role"],
            "username": row["username"], "message": message}


@app.post("/api/auth/login")
def login(payload: LoginIn, request: Request, response: Response):
    if not auth.origin_allowed(request):
        raise HTTPException(status_code=403, detail={"error": "bad origin",
                                                     "code": "bad_origin"})
    ident = auth.client_ip(request)
    auth.rate_limit("login", ident)
    name = users.normalise_username(getattr(payload, "username", "")
                                    or getattr(payload, "email", ""))
    auth.rate_limit("login_user", "login:" + name)
    try:
        user = users.authenticate(name, payload.password)
    except users.AuthError as exc:
        raise HTTPException(status_code=401, detail={"error": str(exc),
                                                     "code": exc.code})
    token, csrf = users.create_session(user["id"], ident,
                                      request.headers.get("user-agent", ""))
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="strict",
        secure=auth.is_https(request), path="/", max_age=auth.SESSION_TTL)
    return {"ok": True, "csrf": csrf, "user": users.public_view(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, session: dict = Depends(require_user)):
    """Requires the session *and* a CSRF token, like every other write.

    Logout looks harmless -- it only clears a cookie -- but it is a login
    action: without the check, any page in any tab could sign somebody out at
    the moment they were using the panel.
    """
    users.destroy_session(_cookie(request))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    session = _session(request)
    if not session:
        return {"authenticated": False}
    return {"authenticated": True, "csrf": session["csrf"],
            "user": {"id": session["user_id"], "username": session["username"],
                     "role": session["role"]}}


# ── Saved accounts ────────────────────────────────────────────────────────

@app.get("/api/accounts")
def accounts_list(session: dict = Depends(require_user)):
    return {"accounts": accounts.list_for(session["user_id"]),
            "max": accounts.MAX_ACCOUNTS_PER_USER}


@app.post("/api/accounts")
def accounts_add(payload: AccountIn, request: Request, session: dict = Depends(require_user)):
    # Each save performs a scrypt hash and an AES-GCM seal, and a burst would
    # spend CPU plus fill the vault. Keyed by person and by address separately:
    # one shared IP must not let anyone past their own allowance.
    auth.rate_limit("account_add", str(session["user_id"]))
    auth.rate_limit("account_add_ip", auth.client_ip(request))
    try:
        account_id = accounts.add(session["user_id"], payload.email,
                                  payload.password, payload.label)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc),
                                                     "code": exc.code})
    return {"ok": True, "id": account_id}


@app.delete("/api/accounts/{account_id}")
def accounts_remove(account_id: int, session: dict = Depends(require_user)):
    if not accounts.remove(session["user_id"], account_id):
        raise HTTPException(status_code=404, detail={"error": "no such account",
                                                     "code": "not_found"})
    return {"ok": True}


@app.post("/api/accounts/{account_id}/unblock")
def accounts_unblock(account_id: int, session: dict = Depends(require_user)):
    if not accounts.clear_block(session["user_id"], account_id):
        raise HTTPException(status_code=404, detail={"error": "no such account",
                                                     "code": "not_found"})
    return {"ok": True}


# ── Jobs ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def jobs_list(session: dict = Depends(require_user)):
    return {"jobs": jobs.jobs_for(session["user_id"])}


@app.get("/api/jobs/current")
def jobs_current(session: dict = Depends(require_user)):
    job = jobs.current_for(session["user_id"])
    if not job:
        return {"job": None}
    return {"job": job}


class StartIn(BaseModel):
    account_id: int = Field(ge=1)


@app.post("/api/jobs")
def jobs_start(payload: StartIn, request: Request, session: dict = Depends(require_user)):
    auth.rate_limit("job_start", str(session["user_id"]))
    auth.rate_limit("job_start_ip", auth.client_ip(request))
    try:
        job = jobs.start_job(session["user_id"], payload.account_id,
                             max_levels=jobs.MAX_LEVELS)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=404, detail={"error": str(exc),
                                                     "code": exc.code})
    except jobs.TooManyJobs as exc:
        raise HTTPException(status_code=429, detail={"error": str(exc),
                                                     "code": "too_many_jobs"})
    except jobs.AccountBusy:
        raise HTTPException(status_code=409, detail={"error": "that account already has a live run",
                                                     "code": "account_busy"})
    except jobs.AccountBlocked as exc:
        raise HTTPException(status_code=409, detail={"error": "blocked until %d (%s)"
                                                     % (exc.until, exc.reason),
                                                     "code": "account_blocked"})
    except jobs.NoFreeProfile:
        raise HTTPException(status_code=409, detail={"error": "every device slot is busy, try shortly",
                                                     "code": "no_slot"})
    except jobs.NeedsKey:
        raise HTTPException(status_code=403, detail={
            "error": "You are out of levels. Ask the admin for another key.",
            "code": "needs_key"})
    return job


@app.post("/api/jobs/{job_id}/stop")
def jobs_stop(job_id: str, session: dict = Depends(require_user)):
    # Owner-scoped: someone else's job id is simply not found.
    if not jobs.stop_job(job_id, session["user_id"]):
        raise HTTPException(status_code=404, detail={"error": "no such running job",
                                                     "code": "not_found"})
    return {"stopped": True}


# ── Run keys ──────────────────────────────────────────────────────────────

@app.get("/api/key")
def key_status(session: dict = Depends(require_user)):
    """What the dashboard needs: how many levels are left, and any held codes."""
    return keys.for_user(session["user_id"])


@app.post("/api/key/redeem")
def key_redeem(payload: KeyIn, request: Request, session: dict = Depends(require_user)):
    """Spend a code. This is the endpoint a million guesses would hit, so it is
    the most tightly limited thing in the panel: five tries per person per hour,
    twenty per address per hour."""
    auth.rate_limit("key_redeem", str(session["user_id"]))
    auth.rate_limit("key_redeem_ip", auth.client_ip(request))
    try:
        result = keys.redeem(session["user_id"], payload.code)
    except keys.KeyError_ as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc),
                                                     "code": exc.code})
    return {"ok": True, "levels": result["levels"]}


# ── Admin ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/overview")
def admin_overview(session: dict = Depends(require_admin)):
    return {
        "users": [dict(u, levels=keys.credits(u["id"]),
                       unused_keys=len([k for k in keys.for_user(u["id"])["keys"]
                                        if k["status"] == "unused"]))
                  for u in users.list_users()],
        "jobs": jobs.jobs_all(40),
        "cooldowns": jobs.cooldowns(),
        "open_signup": users.open_signup(),
        "active": [
            {"id": r["id"], "user_id": r["user_id"], "email": r["email"],
             "profile": r["profile"]}
            for r in jobs.active_jobs()
        ],
        "limits": {
            "per_user": jobs.MAX_PER_USER,
            "pool": jobs.PROFILE_POOL,
            "stagger": jobs.LOGIN_STAGGER_SECONDS,
            "cycle_cap": jobs.CYCLE_CAP_SECONDS,
        },
    }


@app.post("/api/admin/users/{user_id}/key")
def admin_mint_key(user_id: int, payload: MintIn,
                   session: dict = Depends(require_admin)):
    """Mint a 6 digit key for one person. The code comes back exactly once."""
    try:
        code = keys.mint(user_id, payload.note)
    except keys.KeyError_ as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc),
                                                     "code": exc.code})
    return {"ok": True, "code": code, "username": users.get_by_id(user_id)["username"]}


@app.delete("/api/admin/keys/{key_id}")
def admin_revoke_key(key_id: int, session: dict = Depends(require_admin)):
    """Void an unused key, for whoever it was minted for."""
    row = db.one("SELECT user_id FROM keys WHERE id = ?", (int(key_id),))
    if not row or not keys.revoke(row["user_id"], key_id):
        raise HTTPException(status_code=404, detail={"error": "no such unused key",
                                                     "code": "not_found"})
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/status")
def admin_status(user_id: int, payload: StatusIn,
                 session: dict = Depends(require_admin)):
    if user_id == session["user_id"] and payload.status != "active":
        raise HTTPException(status_code=400, detail={"error": "you cannot disable yourself",
                                                     "code": "self_disable"})
    if not users.set_status(user_id, payload.status):
        raise HTTPException(status_code=404, detail={"error": "no such user",
                                                     "code": "not_found"})
    return {"ok": True}


# ── Pages ─────────────────────────────────────────────────────────────────

@app.get("/")
def page_index():
    return FileResponse(STATIC / "index.html")


@app.get("/signup")
def page_signup():
    return FileResponse(STATIC / "signup.html")


@app.get("/login")
def page_login():
    return FileResponse(STATIC / "login.html")


@app.get("/dashboard")
def page_dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/admin")
def page_admin():
    return FileResponse(STATIC / "admin.html")


# Browsers ask for these whether or not a page linked them. Answering 204 is
# quieter than a 404 in the log, and the SVG above is the actual icon.
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def icon_probe():
    return Response(status_code=204)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/service-worker.js")
def no_service_worker():
    """Browsers probe this on every origin. 204 keeps it out of the log."""
    return Response(status_code=204)


app.mount("/", StaticFiles(directory=str(STATIC), html=False), name="static")