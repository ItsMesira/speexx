"""Login verification and failure classification.

This module exists because of a specific, observed failure. The original
orchestrator's login was passive: it filled the form, submitted, and returned
nothing. Its main loop then re-detected the login form on the next tick and
submitted again — roughly every eleven seconds, forever. A wrong password
therefore became an unbounded stream of failed logins, which is what gets the
IP locked. Speaking's bot was better (it returned a bool and backed off) but it
could not tell a wrong password from a rate limit, because both simply left the
URL on /login.

The rules here:

  * Success is verified positively, not inferred from a URL change.
  * A failure is classified, so the two fatal cases can be refused forever.
  * bad_credentials and locked are never retried. Not once. Not with backoff.
  * Everything else is transient and gets a small bounded number of retries.
  * The caller records a per-account cooldown on the fatal cases, so even a
    fresh submission cannot hammer the portal.

Rule ordering matters. "Too many failed attempts" contains the phrase "too
many", so the locked rules are evaluated before the rate-limit rules; matching
them the other way round would classify an outright lock as merely transient and
retry it, which is precisely the behaviour being fixed.
"""

from __future__ import annotations

OK = "ok"
BAD_CREDENTIALS = "bad_credentials"
LOCKED = "locked"
RATE_LIMITED = "rate_limited"
CAPTCHA = "captcha"
UNKNOWN = "unknown"

#: Never retried. The panel puts the account on cooldown for these.
FATAL = frozenset({BAD_CREDENTIALS, LOCKED})

TRANSIENT = frozenset({RATE_LIMITED, CAPTCHA, UNKNOWN})

#: Ordered. First match wins — see the module docstring.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CAPTCHA,
        (
            "captcha", "recaptcha", "hcaptcha", "verify you are human",
            "i'm not a robot", "are you a robot", "verify that you",
        ),
    ),
    (
        LOCKED,
        (
            "account locked", "account has been locked", "account is locked",
            "has been suspended", "account suspended", "temporarily locked",
            "too many failed", "too many incorrect", "too many unsuccessful",
            "maximum number of attempts", "access denied for", "locked out",
            # Non-English, because this portal serves several locales.
            "ปิดการใช้งาน", "บัญชีถูกล็อค", "ถูกระงับ",
            "账号已锁定", "帳戶已鎖定", "账户已被锁定",
            "계정이 잠금", "계정 잠금",
            "konto gesperrt", "gesperrt",
            "compte verrouill", "verrouill",
            "アカウントロック", "停止",
        ),
    ),
    (
        RATE_LIMITED,
        (
            "too many requests", "rate limit", "too many attempts",
            "slow down", "please wait", "try again in", "temporarily unavailable",
            "service unavailable", "มีคำขอมากเกินไป", "请求过于频繁",
            "リクエストが多すぎ", "zu viele anfragen",
        ),
    ),
    (
        BAD_CREDENTIALS,
        (
            "incorrect password", "wrong password", "invalid password",
            "incorrect username", "incorrect user name", "invalid username",
            "invalid user name", "username or password", "user name or password",
            "email or password", "no such user", "unknown user",
            "account not found", "invalid credentials", "login failed",
            "authentication failed", "incorrect details", "invalid details",
            "รหัสผ่านไม่ถูกต้อง", "ไม่ถูกต้อง", "ชื่อผู้ใช้ไม่ถูกต้อง",
            "密码错误", "密码不正确", "用户名或密码",
            "비밀번호가", "아이디 또는 비밀번호",
            "falsches passwort", "passwort ist nicht korrekt",
            "mot de passe incorrect", "identifiants",
            "パスワードが", "パスワードが正しく",
        ),
    ),
)

#: Text on the page that proves we are actually signed in. Deliberately several
#: independent signals: any one of them can be absent on a given layout.
#: Secondary confirmation only — never the *decision*. A logged-in page that
#: renders in Thai, Chinese or Korean need not contain any of these, and an
#: earlier version required one, so every non-English login was reported as a
#: failure and then retried with a 60 s backoff.
SIGNED_IN_MARKERS = (
    "log out", "logout", "sign out", "sign out", "my account", "my profile",
    "subscriptions", "dashboard", "my learning", "account settings",
    "ออกจากระบบ", "บัญชีของฉัน", "ออกจากระบบ", "ข้อมูลส่วนตัว",
    "退出登录", "登出", "注销", "我的账户", "退出登入",
    "로그아웃", "로그아웃하기", "계정 설정", "내 정보",
    "cerrar sesión", "señalizar", "mon compte", "mein konto", "abmelden",
    "アカウント", "ログアウト", "マイアカウント", "アカウント設定",
    "đăng xuất", "tài khoản",
)


def classify_text(text: str) -> str:
    """Map visible page text to a failure reason. Pure, so it is unit tested."""
    if not text:
        return UNKNOWN
    lowered = text.lower()
    for reason, needles in RULES:
        for needle in needles:
            if needle in lowered:
                return reason
    return UNKNOWN


def signed_in(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in SIGNED_IN_MARKERS)


def login_form_present(page) -> bool:
    """The two-step login form is on screen."""
    try:
        return (
            page.locator('input[name="userName"]').count() > 0
            or page.locator('input[name="password"]').count() > 0
        )
    except Exception:
        return False


def page_text(page, limit: int = 1200) -> str:
    try:
        return page.evaluate(
            "(n => (document.body ? document.body.innerText : '').slice(0, n))(%d)" % int(limit)
        ) or ""
    except Exception:
        return ""


def verify(page) -> tuple[bool, str]:
    """Decide whether a login succeeded. Returns (ok, reason).

    The decision is **structural**, not textual: the URL has left /login and the
    login form is gone. That is the actual signal, and it holds in every
    language.

    This previously required a signed-in word to appear in the page text, and
    returned `unknown` otherwise. On a Thai account that logged in perfectly the
    check failed, the runner waited 60 s and retried, and a successful login was
    reported as a failure three times over.

    Text markers are still read, but only to *label* the outcome for the log —
    never to reject it. The caller settles on this state across consecutive
    polls, so a transient pass through an error page cannot be mistaken for
    success.
    """
    try:
        url = page.url
    except Exception:
        return False, UNKNOWN

    if "/login" in url:
        return False, classify_text(page_text(page))
    if login_form_present(page):
        return False, classify_text(page_text(page))

    return True, OK


def describe_login(page) -> str:
    """Best-effort note about which confirmation was seen, for the log only."""
    try:
        return "marker" if signed_in(page_text(page)) else "structural"
    except Exception:
        return "structural"
