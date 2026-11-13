#!/usr/bin/env python3
"""check_level.py — report the CEFR level of every account in Accounts.md.

Logs in with each account's own persistent browser profile (the same dir the
AutoCycle bot uses, so most accounts are already logged in and this is fast),
then reads the level from the portal's own JSON:
    GET /articles/<articleId>/initial-data  ->  {"currentLevel": "B2.1", ...}
Falls back to scanning the visible page text for a CEFR token (A1..C2).

Usage:
    ./check-level                     # all accounts in Accounts.md
    ./check-level /path/to/file.md    # a different accounts file
    ./check-level --headed            # show the browser while checking

NOTE: stop the AutoCycle bot first (./stop.sh) — a running session holds the
account's profile dir and this script cannot open it while locked.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

PORTAL = "https://portal.speexx.com"
DEFAULT_ACCOUNTS = Path(__file__).absolute().parent / "Accounts.md"
CEFR_RE = re.compile(r"\b[ABC][12](?:\.\d)?\b")


# ── Cloud / no-display launch options — mirrors speexx_autocycle.py ────────
def _running_in_cloud():
    if os.environ.get("COLAB_RELEASE_VERSION") or os.environ.get("CODESPACES"):
        return True
    if "KAGGLE" in " ".join(os.environ):
        return True
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


_HEADLESS = os.environ.get("SPEEXX_HEADLESS", "").strip().lower()
_HEADLESS = (_HEADLESS not in ("0", "false", "no")) if _HEADLESS else _running_in_cloud()


def _launch_args():
    args = ["--disable-blink-features=AutomationControlled"]
    if (hasattr(os, "geteuid") and os.geteuid() == 0) or os.environ.get("SPEEXX_NO_SANDBOX") == "1":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    return args

# Login selectors (name attributes — NOT the speaking bot's #id selectors).
USER_INPUT = 'input[name="userName"]'
PASS_INPUT = 'input[name="password"]'
LOGIN_BTN = "button.button-login"


def log(msg):
    print("[check-level] %s" % msg, flush=True)


def parse_accounts(path):
    """Same formats multi_run.sh accepts:
    'email password' on one line, or an email line followed by a password line.
    # comments and blank lines are skipped."""
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
            log("WARNING: line %d of %s has no password, skipping" % (i + 1, path))
            i += 1
    return pairs


def profile_dir(username):
    """Same per-account dir the AutoCycle bot uses, so stored logins carry over."""
    h = hashlib.sha1(username.strip().lower().encode("utf-8")).hexdigest()[:8]
    return str(Path.home() / (".speexx_autocycle_profile_" + h))


def do_login(page, email, password):
    """Fill the two-step login form if it is up. Returns False if a login form
    is still visible afterwards (wrong password / locked out)."""
    deadline = time.time() + 10
    while time.time() < deadline and page.locator(USER_INPUT).count() == 0:
        time.sleep(0.5)
    if page.locator(USER_INPUT).count() == 0:
        return True  # already logged in
    log("  login form detected")
    page.locator(USER_INPUT).first.fill(email)
    page.locator(LOGIN_BTN).first.click()
    time.sleep(4)
    if page.locator(PASS_INPUT).count() > 0:
        page.locator(PASS_INPUT).fill(password)
        page.locator(LOGIN_BTN).click()
        time.sleep(7)
    if page.locator(USER_INPUT).count() > 0 or page.locator(PASS_INPUT).count() > 0:
        return False
    return True


def scan_dom_level(page):
    """Fallback: CEFR token in visible page text (e.g. the localized
    'English B2.1' label on the course card)."""
    try:
        txt = page.evaluate(
            "(() => { const o=[];const w=document.createTreeWalker("
            "document.body,NodeFilter.SHOW_TEXT);let n;"
            "while((n=w.nextNode())){const t=(n.textContent||'').trim();"
            "if(t){const el=n.parentElement;"
            "if(el&&el.offsetParent!==null)o.push(t);}}return o.join(' ');})()"
        )
    except Exception:
        return None
    m = CEFR_RE.search(txt)
    return m.group(0) if m else None


def check_account(pw, email, password, headed=False):
    """Returns (level, course, status). level/course may be None; status is one
    of 'ok', 'login failed', 'no level found', 'profile busy', 'error'."""
    import playwright._impl._errors as pwe  # noqa: E402  (local: cheap import)

    captured = {}   # url path -> parsed initial-data JSON

    def on_response(resp):
        try:
            path = urlparse(resp.url).path
            if "/initial-data" in path and resp.ok:
                captured[path] = json.loads(resp.text())
        except Exception:
            pass

    try:
        ctx = pw.chromium.launch_persistent_context(
            profile_dir(email), headless=(not headed) if not _HEADLESS else True,
            args=_launch_args())
    except Exception as e:
        if "ProcessSingleton" in str(e) or "SingletonLock" in str(e) or \
                "Failed to launch" in str(e):
            return (None, None, "profile busy (bot running?)")
        return (None, None, "error: %s" % str(e)[:80])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(PORTAL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log("  goto error: %s" % str(e)[:80])
        if not do_login(page, email, password):
            return (None, None, "login failed (bad password?)")

        # Wait for the app to land on /articles/<id> and fire initial-data.
        deadline = time.time() + 20
        data = None
        while time.time() < deadline:
            for d in captured.values():
                if isinstance(d, dict) and d.get("currentLevel"):
                    data = d
                    break
            if data:
                break
            time.sleep(0.5)
        if data:
            lvl = data.get("currentLevel")
            course = (data.get("courseLanguageUsage") or {}).get("englishName") \
                or (data.get("courseLanguageUsage") or {}).get("name")
            return (lvl, course, "ok")

        # Fallback: whatever URL we're on, ask for its initial-data directly;
        # then fall back to a DOM scan.
        try:
            path = urlparse(page.url).path
            if "/articles/" in path:
                d = json.loads(ctx.request.get(
                    page.url.rstrip("/") + "/initial-data").text())
                if d.get("currentLevel"):
                    return (d["currentLevel"],
                            (d.get("courseLanguageUsage") or {}).get("englishName"),
                            "ok")
        except Exception:
            pass
        lvl = scan_dom_level(page)
        if lvl:
            return (lvl, None, "ok (dom)")
        return (None, None, "no level found (url: %s)" % urlparse(page.url).path)
    except Exception as e:
        return (None, None, "error: %s" % str(e)[:80])
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Check every account's CEFR level")
    ap.add_argument("accounts", nargs="?", default=str(DEFAULT_ACCOUNTS),
                    help="accounts file (default: Accounts.md)")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser windows while checking")
    args = ap.parse_args()

    pairs = parse_accounts(Path(args.accounts))
    if not pairs:
        log("ERROR: no accounts found in %s" % args.accounts)
        sys.exit(1)
    log("%d account(s) in %s" % (len(pairs), args.accounts))

    rows, failures = [], []
    with sync_playwright() as pw:
        for n, (email, password) in enumerate(pairs, 1):
            log("(%d/%d) checking %s" % (n, len(pairs), email))
            level, course, status = check_account(pw, email, password,
                                                  headed=args.headed)
            rows.append((email, course, level, status))
            if status != "ok" or not level:
                failures.append(email)
            log("  -> %s  (%s)" % (level or "-", status))

    print()
    print("%-34s %-10s %-8s %s" % ("EMAIL", "COURSE", "LEVEL", "STATUS"))
    print("-" * 78)
    for email, course, level, status in rows:
        print("%-34s %-10s %-8s %s" % (email, course or "-", level or "-", status))
    print("-" * 78)
    ok = [r for r in rows if r[3].startswith("ok")]
    log("%d/%d accounts checked OK" % (len(ok), len(rows)))
    if failures:
        log("problems: %s" % ", ".join(failures))


if __name__ == "__main__":
    main()
