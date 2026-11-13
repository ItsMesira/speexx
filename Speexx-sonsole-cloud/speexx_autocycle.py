#!/usr/bin/env python3
"""Speexx AutoCycle — orchestrator.

The solver is optimized.js (unchanged). This script only:
  * logs in,
  * navigates to / stays inside the course,
  * injects optimized.js into every exercise page (and RE-injects it on every
    page reload, since a new document loses the controller),
  * and when the URL is a pronunciation packet, presses Next until the
    "Continue learning" button shows, clicks it (returns home), then reopens
    the exercises and continues.

Usage:
    python speexx_autocycle.py <username> <password> [--max-steps N]
        [--device-profile N] [--profile-dir PATH] [--runtime SECONDS]
"""

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

PORTAL = "https://portal.speexx.com"
SOLVER = (Path(__file__).absolute().parent / "optimized.js").read_text(encoding="utf-8")


# ── Cloud / no-display launch options ──────────────────────────────────────
# Behaviour is identical to the local run; only the browser window changes.
# Colab, Kaggle, Codespaces and devcontainers have no display, and root runs
# need Chromium's sandbox disabled, so:
#   * headless=True on any detected no-display environment (override with
#     SPEEXX_HEADLESS=0 after starting xvfb),
#   * --no-sandbox when running as root.
def _running_in_cloud():
    if os.environ.get("COLAB_RELEASE_VERSION") or os.environ.get("CODESPACES"):
        return True
    if "KAGGLE" in " ".join(os.environ):
        return True
    # Any Linux box with no X display (devcontainers, CI) — macOS is unaffected.
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


_HEADLESS = os.environ.get("SPEEXX_HEADLESS", "").strip().lower()
_HEADLESS = (_HEADLESS not in ("0", "false", "no")) if _HEADLESS else _running_in_cloud()


def _launch_args():
    args = ["--disable-blink-features=AutomationControlled"]
    if (hasattr(os, "geteuid") and os.geteuid() == 0) or os.environ.get("SPEEXX_NO_SANDBOX") == "1":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    return args

# ── Multi-session device profiles ───────────────────────────────────────────
# Speexx allows ONE account per "device" at a time, and a web app identifies a
# device by IP + browser fingerprint. Parallel sessions on this Mac share the
# IP, so each session varies the fingerprint (UA, viewport, timezone, locale,
# canvas noise) to look like a distinct device. Profile 0 = default behavior.
DEVICE_PROFILES = [
    {"ua": None, "size": (1280, 800), "tz": None, "lang": "en-US", "seed": 0},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     "size": (1440, 900), "tz": "Asia/Bangkok", "lang": "th-TH", "seed": 11},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
     "size": (1512, 982), "tz": "Europe/London", "lang": "en-GB", "seed": 23},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
     "size": (1920, 1080), "tz": "America/New_York", "lang": "en-US", "seed": 37},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
     "size": (1366, 768), "tz": "Asia/Tokyo", "lang": "ja-JP", "seed": 53},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
     "size": (1536, 864), "tz": "Europe/Berlin", "lang": "de-DE", "seed": 67},
]


def _canvas_noise_source(seed):
    """JS that perturbs canvas read APIs deterministically per seed so each
    session gets a different canvas fingerprint."""
    return """
(() => {
    let rng = %d || 1;
    const rnd = () => { rng = (rng * 9301 + 49297) %% 233280; return rng / 233280; };
    try {
        const orig = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function () {
            try {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const w = Math.min(this.width || 1, 64), h = Math.min(this.height || 1, 64);
                    const d = ctx.getImageData(0, 0, w, h);
                    const off = Math.floor(rnd() * 4);
                    for (let i = off; i < d.data.length; i += 4) d.data[i] = (d.data[i] ^ 1) & 0xff;
                    ctx.putImageData(d, 0, 0);
                }
            } catch (e) {}
            return orig.apply(this, arguments);
        };
        const origB = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function () {
            try {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const w = Math.min(this.width || 1, 64), h = Math.min(this.height || 1, 64);
                    const d = ctx.getImageData(0, 0, w, h);
                    const off = Math.floor(rnd() * 4);
                    for (let i = off; i < d.data.length; i += 4) d.data[i] = (d.data[i] ^ 1) & 0xff;
                    ctx.putImageData(d, 0, 0);
                }
            } catch (e) {}
            return origB.apply(this, arguments);
        };
    } catch (e) {}
})();
""" % (seed,)


def default_profile_dir(username):
    """Per-account profile dir so parallel sessions never share a login."""
    h = hashlib.sha1(username.strip().lower().encode("utf-8")).hexdigest()[:8]
    return str(Path.home() / (".speexx_autocycle_profile_" + h))

# Variant of optimized.js whose boot() skips the 1 -> 2 -> 1 bounce. The bounce
# exists to prime a normal exercise 1's first view, but for START-GATED
# exercises that navigation re-creates the view fresh (Start button comes back,
# started state lost), so solve/correct fire against a re-gated view and the
# packet never completes. We only need the bounce skipped for gated exercises;
# the plain SOLVER still handles normal ones. The transform lives here so
# optimized.js on disk stays untouched.
_SOLVER_NO_BOUNCE = SOLVER.replace(
    """    addTimer(function () {
      if (!clickExerciseOrdinal(2)) { startCycle(); return; }
      waitForOrdinal(2, function () {
        addTimer(function () {
          if (!clickExerciseOrdinal(1)) { startCycle(); return; }
          waitForOrdinal(1, startCycle);
        }, delays.warmup);
      });
    }, delays.warmup);""",
    "    addTimer(startCycle, delays.warmup);",
)
if _SOLVER_NO_BOUNCE == SOLVER:
    raise RuntimeError("no-bounce transform did not apply to optimized.js (bounce block changed?)")

# Seconds between pronunciation Next presses. Edit here (or override with
# --pron-interval on the command line). Lower = faster, higher risk of the app
# not registering an exercise as complete before it advances.
PRON_INTERVAL = 1.5

# Auto-stop for the "no entry found" loop: open_exercises() failing this many
# times IN A ROW (each failure is ~7 s of waiting) means the account has no
# course entry to open and the session would just spin home -> open exercises
# forever. The session exits with code 5, and multi_run.sh stops that session
# permanently on it (logged as such in the session log). Transient mid-load
# misses are fine: two failures in a row is normal, 10 is a stuck account.
# Override or disable with SPEEXX_OPEN_EMPTY_MAX (0 = never auto-stop).
OPEN_EMPTY_MAX = int(os.environ.get("SPEEXX_OPEN_EMPTY_MAX", "10") or "10")
_open_empty = 0

DELAYS = {
    "poll": 1.0,        # fast observation tick when deciding what to do
    "after_login": 7.0,
    "after_enter": 3.0,
    "after_inject": 2.0,
    "pron_next": 3.0,   # pause between pronunciation Next presses
    "settle": 1.5,
}

PACKET_RE = re.compile(r"/(pronunciation|standard)-packet/(\d+)")
EXERCISE_RE = re.compile(r"/exercise/(\d+)")

NEXT_SELECTORS = [
    "button.next",
    "button.nxt-exercise",
    ".nxt-exercise",
    "a.next",
]

PRON_CONTINUE_RE = re.compile(r"continue\s*learning|continue", re.IGNORECASE)


def log(msg):
    print("[autocycle] %s" % msg, flush=True)


def url_kind(url):
    m = PACKET_RE.search(urlparse(url).path)
    return m.group(1) if m else None


def url_packet(url):
    m = PACKET_RE.search(urlparse(url).path)
    return int(m.group(2)) if m else None


def url_ordinal(url):
    ms = EXERCISE_RE.findall(urlparse(url).path)
    return int(ms[-1]) if ms else None


# ── Playwright helpers ──────────────────────────────────────────────────────

def _click_any(page, selectors, label):
    for sel in selectors:
        loc = page.locator(sel)
        for i in range(loc.count()):
            try:
                el = loc.nth(i)
                if el.is_visible() and el.is_enabled():
                    el.click(timeout=1500)
                    log("%s : %s" % (label, sel))
                    return True
            except Exception:
                continue
    return False


def _click_native(page, selectors, label):
    """Native element.click() like the bookmark uses (bypasses overlay checks)."""
    for sel in selectors:
        try:
            ok = page.evaluate(
                "(function(sel){var el=document.querySelector(sel);"
                "if(!el)return false;el.click();return true;})(%r)" % sel
            )
            if ok:
                log("%s : %s" % (label, sel))
                return True
        except Exception:
            continue
    return False


def _nudge(page):
    """Native-click Next; on a root/summary, fall back to a primary button."""
    if _click_native(page, ["button.next", "button.nxt-exercise", "a.next"],
                     "wdog nudge-next"):
        return True
    if _click_start(page):
        log("wdog nudge-start")
        return True
    try:
        return bool(page.evaluate(
            "(function(){"
            "var els=document.querySelectorAll("
            "'button.btn-primary,.btn-continue,a.btn-primary,[data-action=\"continue\"]');"
            "for(var i=0;i<els.length;i++){"
            "if(els[i].offsetParent!==null){els[i].click();return true;}}return false;"
            "})()"
        ))
    except Exception:
        return False


def _skip_to_next(page, ordinal):
    """Give up on an exercise and move to the next one (pager icon, Next
    button, or packet-root URL fallback). Returns True if it navigated."""
    if _click_native(
            page,
            ['.exercise-icon[data-ordinal="%d"]' % (ordinal + 1)],
            "skip pager"):
        return True
    if _click_native(page, ["button.next", "button.nxt-exercise", "a.next",
                            ".nxt-exercise"], "skip next"):
        return True
    try:
        root = re.sub(r"/exercise/\d+$", "", page.url)
        if root != page.url:
            page.goto(root, timeout=30000, wait_until="domcontentloaded")
            log("skip -> packet root %s" % root)
            return True
    except Exception:
        pass
    return False


def _click_continue(page):
    """Click a visible 'Continue learning' button if present (native click)."""
    try:
        return bool(page.evaluate(
            "(function(){"
            "var els=document.querySelectorAll('button,a.btn,.btn,a[role=button]');"
            "for(var i=0;i<els.length;i++){var el=els[i];"
            "if(el.offsetParent!==null&&(/continue/i.test(el.textContent||''))){"
            "el.click();return true;}}return false;"
            "})()"
        ))
    except Exception:
        return False


def _click_start(page):
    """Click a visible Start/Begin/Play button if present (native click)."""
    try:
        return bool(page.evaluate(
            "(function(){"
            "var sels=['button.start-exercise','.start-exercise',"
            "'button.start','.btn-start','.start',"
            "'[data-action=\"start\"]','[data-action=\"play\"]'];"
            "for(var s=0;s<sels.length;s++){var el=document.querySelector(sels[s]);"
            "if(el&&el.offsetParent!==null){el.click();return true;}}"
            "var els=document.querySelectorAll('button,a.btn,.btn,a[role=button]');"
            "for(var i=0;i<els.length;i++){var b=els[i];"
            "if(b.offsetParent!==null&&(/^(start|begin|play|starten|comenzar|inizia|demarrer|开始|시작)\\b/i.test((b.textContent||'').trim()))){"
            "b.click();return true;}}return false;"
            "})()"
        ))
    except Exception:
        return False


def start_gated(page):
    """True if a visible Start-gated exercise button is present."""
    try:
        return bool(page.evaluate(
            "(function(){"
            "var els=document.querySelectorAll('button.start-exercise,.start-exercise');"
            "for(var i=0;i<els.length;i++){if(els[i].offsetParent!==null)return true;}"
            "return false;"
            "})()"
        ))
    except Exception:
        return False


def controls_ready(page):
    """True once the real exercise controls view is registered (Start clicked)."""
    try:
        return bool(page.evaluate(
            "!!(window.__speexxAutoCycleControls && "
            "!window.__speexxAutoCycleControls._isDestroyed)"
        ))
    except Exception:
        return False


def wait_controls(page, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if controls_ready(page):
            return True
        time.sleep(0.5)
    return False


# ── Login ──────────────────────────────────────────────────────────────────
def ensure_login(page, username, password):
    if page.locator('input[name="userName"]').count() == 0 and \
       page.locator('input[name="password"]').count() == 0:
        return  # already logged in
    log("login form detected")
    if page.locator('input[name="userName"]').count() > 0:
        try:
            page.locator('input[name="userName"]').first.fill(username)
            page.locator("button.button-login").first.click()
            time.sleep(4)
        except Exception as e:
            log("login step1 error: %s" % e)
    if page.locator('input[name="password"]').count() > 0:
        page.locator('input[name="password"]').fill(password)
        page.locator("button.button-login").click()
        log("submitted login")
        time.sleep(DELAYS["after_login"])


# ── optimized.js injection ─────────────────────────────────────────────────
def controller_alive(page):
    try:
        return bool(page.evaluate(
            "!!(window.__speexxAutoCycleController && "
            "window.__speexxAutoCycleController._alive)"
        ))
    except Exception:
        return False


def clear_solver_state(page):
    try:
        page.evaluate(
            "try{localStorage.removeItem('__speexxAutoCycleResume');"
            "}catch(e){}"
        )
    except Exception:
        pass


def app_ready(page):
    """True once the app's Backbone/CourseWare modules exist so the solver's
    view patch can install (without them the solver degrades to direct-click
    mode and can't fill blanks)."""
    try:
        return bool(page.evaluate(
            "typeof CourseWare !== 'undefined' && CourseWare.CourseExercises && "
            "typeof Backbone !== 'undefined' && Backbone.Speexx && "
            "Backbone.Speexx.HandlebarsView"
        ))
    except Exception:
        return False


def wait_app_ready(page, timeout=6.0):
    """Poll until the app modules are loaded (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_ready(page):
            return True
        time.sleep(0.4)
    return False


def inject_solver(page, no_bounce=False):
    clear_solver_state(page)
    if not wait_app_ready(page):
        log("WARN: app not ready before inject (will use fallback)")
    try:
        page.evaluate(_SOLVER_NO_BOUNCE if no_bounce else SOLVER)
        log("injected optimized.js%s" % (" (no-bounce)" if no_bounce else ""))
    except Exception as e:
        log("inject error: %s" % e)
    time.sleep(DELAYS["after_inject"])


def stop_controller(page):
    try:
        page.evaluate(
            "if(window.__speexxAutoCycleController){"
            "window.__speexxAutoCycleController.stop();}"
        )
    except Exception:
        pass


# ── Course entry ───────────────────────────────────────────────────────────
def open_exercises(page):
    """From home / article root, go into a standard packet (avoids /support)."""
    global _open_empty
    # Give SPA links a chance to render before giving up.
    for _ in range(3):
        if page.locator('a[href*="standard-packet"], a[href*="pronunciation-packet"]').count():
            break
        time.sleep(2)
    loc = page.locator('a[href*="standard-packet"]')
    for i in range(loc.count()):
        href = loc.nth(i).get_attribute("href") or ""
        if "support" in href or "exercise/" in href:
            continue
        full = href if href.startswith("http") else (PORTAL + href)
        target = full.split("?")[0]
        path = urlparse(target).path
        if not re.search(r"/articles/\d+/standard", path):
            continue
        log("open exercises -> %s" % path)
        _open_empty = 0
        try:
            page.goto(target, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log("open error: %s" % e)
        time.sleep(DELAYS["after_enter"])
        return True

    # Fallback: click a visible "Continue"-like button.
    btns = page.locator("button, a.btn, .btn, a[role=button]")
    for i in range(btns.count()):
        el = btns.nth(i)
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            txt = (el.get_attribute("textContent") or "").strip()
            if re.search(r"continue|opstart|start|เข้าสู่บทเรียน", txt, re.I):
                el.click(timeout=1500)
                log("open exercises -> clicked %r" % txt[:30])
                _open_empty = 0
                time.sleep(DELAYS["after_enter"])
                return True
        except Exception:
            continue
    log("open_exercises: no entry found (may be mid-load), waiting")
    _open_empty += 1
    return False


# ── Pronunciation: press Next until Continue-learning, then home ───────────
def handle_pronunciation(page, ordinal=None):
    stop_controller(page)  # don't let the solver fight over this session
    # Mid-packet (ordinal is set): just press Next; no Continue button exists here.
    if ordinal is not None:
        if _click_native(page, ["button.next", "button.nxt-exercise", "a.next"],
                         "pron pronunciation-next"):
            time.sleep(PRON_INTERVAL)
        else:
            time.sleep(DELAYS["pron_next"])
        return
    # Pronunciation summary (ordinal is None): the "Continue learning" button
    # may still be rendering; give it a few tries before pressing Next.
    for _ in range(4):
        if _click_continue(page):   # "Continue learning" -> home
            log("pronunciation: clicked Continue-learning")
            return
        time.sleep(1)
    if not _click_native(page, ["button.next", "button.nxt-exercise", "a.next"],
                         "pron pronunciation-next"):
        log("pronunciation: no Next button found")
    time.sleep(DELAYS["pron_next"])


# ── main loop ──────────────────────────────────────────────────────────────
def main(username, password, max_steps=None, runtime=None,
         device_profile=0, profile_dir=None):
    global _open_empty
    prof = DEVICE_PROFILES[device_profile % len(DEVICE_PROFILES)]
    profile = profile_dir or default_profile_dir(username)
    started = time.time()
    ctx_opts = dict(
        headless=_HEADLESS,
        viewport={"width": prof["size"][0], "height": prof["size"][1]},
        locale=prof["lang"],
        args=_launch_args(),
    )
    if prof["ua"]:
        ctx_opts["user_agent"] = prof["ua"]
    if prof["tz"]:
        ctx_opts["timezone_id"] = prof["tz"]
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, **ctx_opts)
        if prof["seed"]:
            context.add_init_script(_canvas_noise_source(prof["seed"]))
        page = context.pages[0] if context.pages else context.new_page()

        log("opened profile %s (device profile %d: %sx%s %s %s)" %
            (profile, device_profile, prof["size"][0], prof["size"][1],
             prof["lang"], prof["tz"] or "no-tz"))
        try:
            page.goto(PORTAL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log("initial goto error: %s" % e)

        steps = 0
        last_key = None     # (packet, ordinal) we last arrived at
        last_enter = None   # time we arrived at last_key
        last_nudge = 0.0
        last_summary_pkt = None   # last packet whose summary we acted on
        summary_tries = 0
        STALL = 60.0        # idle seconds before nudging Next
        gated_key = None        # (packet, ordinal) we last start-gated
        gated_tries = 0         # consecutive attempts on gated_key
        gated_entered = 0.0     # time of the latest Start click on gated_key
        gated_retried = False   # fast-retry already done for this attempt
        skipped_gated = set()   # gated exercises given up on this session
        GATED_MAX_TRIES = 5
        GATED_VERIFY_TIMEOUT = 25.0
        while True:
            if max_steps and steps >= max_steps:
                log("max-steps reached, stopping")
                break
            if runtime and time.time() - started >= runtime:
                log("runtime %.0fs reached, stopping" % runtime)
                break
            try:
                url = page.url
            except Exception:
                time.sleep(DELAYS["poll"])
                continue

            # Login if needed. On login screen we cannot act on events yet.
            if page.locator('input[name="userName"]').count() > 0 or \
               page.locator('input[name="password"]').count() > 0:
                ensure_login(page, username, password)
                continue

            if not url.startswith(PORTAL):
                try:
                    page.goto(PORTAL, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass
                continue

            kind = url_kind(url)
            ordinal = url_ordinal(url)

            if kind is not None:
                _open_empty = 0  # a packet page breaks any no-entry streak

            if kind is None:
                log("home/root (%s) -> open exercises" % url)
                open_exercises(page)
                if OPEN_EMPTY_MAX and _open_empty >= OPEN_EMPTY_MAX:
                    log("open_exercises failed %d times in a row — stopping "
                        "this session (exit 5, supervisor will not restart "
                        "it). Disable with SPEEXX_OPEN_EMPTY_MAX=0."
                        % _open_empty)
                    sys.exit(5)
                time.sleep(DELAYS["poll"])
                continue

            if kind == "pronunciation":
                log("pronunciation %s ord=%s -> next through" %
                    (url_packet(url), ordinal))
                handle_pronunciation(page, ordinal)
                steps += 1
                time.sleep(DELAYS["poll"])
                continue

            # Standard packet: keep optimized.js running; re-inject if reloaded.
            pk = url_packet(url)
            key = (pk, url_ordinal(url))
            now = time.time()

            if url_ordinal(url) is None:
                # Packet root: NEVER inject the solver here. CourseWare/Backbone
                # aren't loaded on root pages, and the solver's own packet-root
                # handler clicks Continue / URL-bumps, hopping to the NEXT packet
                # instead of entering this one's exercises. The app auto-advances
                # root -> exercise/1 by itself within ~2s; wait for that first.
                # Only if we're STILL stuck at root after a few seconds (i.e. a
                # real end-of-packet summary) do we click "Continue learning".
                if page.locator('input[name="userName"]').count() > 0 or \
                   page.locator('input[name="password"]').count() > 0:
                    continue
                settled = False
                for _ in range(6):
                    time.sleep(0.5)
                    if url_ordinal(page.url) is not None:
                        settled = True
                        break
                if settled:
                    log("packet %s root -> settled to ord" % pk)
                    continue
                # Still root: real summary. Click Continue-learning by text only.
                if _click_continue(page):
                    log("packet %s root: clicked Continue-learning" % pk)
                if pk != last_summary_pkt:
                    last_summary_pkt = pk
                    summary_tries = 0
                summary_tries += 1
                if summary_tries >= 6:
                    log("packet %s stuck at root -> returning home" % pk)
                    last_summary_pkt = None
                    summary_tries = 0
                    try:
                        page.goto(PORTAL, timeout=30000,
                                  wait_until="domcontentloaded")
                    except Exception:
                        pass
                steps += 1
                time.sleep(DELAYS["poll"])
                continue

            summary_tries = 0  # any real exercise means we moved on

            if key != last_key:
                last_key = key
                last_enter = now
            if gated_key and gated_key != key:
                gated_key = None
                gated_tries = 0
                gated_entered = 0.0
                gated_retried = False

            # Verify a start-gated exercise actually completed: if Start was
            # consumed but no result appeared and the ordinal never advanced
            # within GATED_VERIFY_TIMEOUT, count a fast retry (re-inject the
            # no-bounce variant to restart the cycle on the started view)
            # instead of waiting for a slow wrap cycle to re-trigger the
            # gated branch. If the Start button is back (view re-gated), the
            # start-gated branch below handles it as a fresh attempt.
            if gated_key and gated_key == key and not start_gated(page) and \
                    gated_entered and now - gated_entered > GATED_VERIFY_TIMEOUT:
                try:
                    res = page.evaluate(
                        "(() => { const el = document.querySelector('.result-badge-text');"
                        "return el ? el.textContent : null; })()")
                except Exception:
                    res = None
                if res is None or str(res).strip() == "":
                    if gated_tries >= GATED_MAX_TRIES:
                        log("standard %s ord=%s -> gated no result after %d tries -> "
                            "skipping exercise" % (pk, ordinal, GATED_MAX_TRIES))
                        skipped_gated.add(gated_key)
                        stop_controller(page)
                        _skip_to_next(page, ordinal)
                        gated_key = None
                        gated_tries = 0
                        gated_entered = 0.0
                        gated_retried = False
                        last_key = (url_packet(page.url), url_ordinal(page.url))
                        last_enter = time.time()
                        steps += 1
                        time.sleep(DELAYS["poll"])
                        continue
                    gated_tries += 1
                    gated_retried = True
                    log("standard %s ord=%s -> no result after %.0fs -> fast retry %d"
                        % (pk, ordinal, GATED_VERIFY_TIMEOUT, gated_tries))
                    stop_controller(page)
                    try:
                        page.evaluate(_SOLVER_NO_BOUNCE)
                    except Exception:
                        pass
                    gated_entered = time.time()
                    last_key = key
                    last_enter = time.time()
                    steps += 1
                    time.sleep(DELAYS["poll"])
                    continue
                else:
                    log("standard %s ord=%s -> gated result=%s" %
                        (pk, ordinal, res.strip()))
                    gated_key = None

            # Start-gated exercise: a visible Start button gates the real view.
            # Sequence matters:
            #   1. stop any stale controller (its cycle targets a null view and
            #      advance() would click the visible Next, skipping the exercise),
            #   2. inject the NO-BOUNCE solver variant, whose boot() calls
            #      startCycle() directly instead of bouncing 1 -> 2 -> 1. The
            #      bounce is fatal here: it navigates away and back, re-creating
            #      exercise 1's view fresh with the Start button back, so the
            #      cycle's solve/correct fire against a re-gated view (result
            #      stays null) and the packet wraps back to exercise 1 forever.
            #   3. click Start IMMEDIATELY after the eval (within the warmup
            #      window, BEFORE startCycle->runCycle fires solve ~1.2s later),
            #      so solve/correct run against the real revealed view.
            #   4. wait for the view, then LET the controller run. Do NOT
            #      re-inject here (a second boot re-bounces / restarts the cycle).
            if start_gated(page):
                gk = (pk, ordinal)
                if gk != gated_key:
                    gated_key = gk
                    gated_tries = 0
                    gated_entered = 0.0
                    gated_retried = False
                # Already gave up on this exercise this session: skip straight
                # through instead of re-solving it forever.
                if gk in skipped_gated:
                    log("standard %s ord=%s -> start-gated already skipped -> advancing" %
                        (pk, ordinal))
                    stop_controller(page)
                    _skip_to_next(page, ordinal)
                    gated_key = None
                    gated_tries = 0
                    gated_entered = 0.0
                    gated_retried = False
                    last_key = (url_packet(page.url), url_ordinal(page.url))
                    last_enter = time.time()
                    steps += 1
                    time.sleep(DELAYS["poll"])
                    continue
                # Cap reached: give up on this exercise, skip to the next one.
                if gated_tries >= GATED_MAX_TRIES:
                    log("standard %s ord=%s -> start-gated gave up after %d tries -> "
                        "skipping exercise" % (pk, ordinal, GATED_MAX_TRIES))
                    skipped_gated.add(gk)
                    stop_controller(page)
                    _skip_to_next(page, ordinal)
                    gated_key = None
                    gated_tries = 0
                    gated_entered = 0.0
                    gated_retried = False
                    last_key = (url_packet(page.url), url_ordinal(page.url))
                    last_enter = time.time()
                    steps += 1
                    time.sleep(DELAYS["poll"])
                    continue
                gated_tries += 1
                log("standard %s ord=%s -> start-gated (attempt %d/%d)" %
                    (pk, ordinal, gated_tries, GATED_MAX_TRIES))
                stop_controller(page)
                clear_solver_state(page)
                if not wait_app_ready(page, timeout=10.0):
                    log("WARN: app not ready before inject (will use fallback)")
                try:
                    page.evaluate(_SOLVER_NO_BOUNCE)
                    log("standard %s ord=%s -> injected no-bounce solver" %
                        (pk, ordinal))
                except Exception as e:
                    log("inject error: %s" % e)
                if _click_start(page):
                    log("standard %s ord=%s -> clicked start button" %
                        (pk, ordinal))
                if not wait_controls(page, timeout=12.0):
                    log("standard %s ord=%s -> controls view not ready after Start"
                        % (pk, ordinal))
                gated_entered = time.time()
                gated_retried = False
                # Only refresh last_enter on the first attempt for a key so the
                # STALL watchdog stays armed while we re-try a gated exercise.
                if gated_tries == 1:
                    last_key = (pk, ordinal)
                    last_enter = time.time()
                steps += 1
                time.sleep(DELAYS["poll"])
                continue

            if not controller_alive(page):
                log("standard %s ord=%s -> inject solver" %
                    (url_packet(url), url_ordinal(url)))
                inject_solver(page)
                last_key = (url_packet(page.url), url_ordinal(page.url))
                last_enter = time.time()
                steps += 1
            else:
                log("standard %s ord=%s -> solver alive, letting it run" %
                    (url_packet(url), url_ordinal(url)))
                steps += 1
                if last_enter is not None and now - last_enter > STALL and \
                        now - last_nudge > STALL:
                    log("stalled at %s for > %.0fs -> nudging Next" %
                        (str(last_key), STALL))
                    _nudge(page)
                    last_nudge = now
            time.sleep(DELAYS["poll"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Speexx AutoCycle orchestrator")
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--pron-interval", type=float, default=PRON_INTERVAL,
                    help="seconds between pronunciation Next presses "
                         "(default: %s)" % PRON_INTERVAL)
    ap.add_argument("--device-profile", type=int, default=0,
                    help="device fingerprint profile index (0-5). Parallel "
                         "sessions must use different profiles.")
    ap.add_argument("--profile-dir", default=None,
                    help="browser profile dir (default: per-account dir under "
                         "home)")
    ap.add_argument("--runtime", type=float, default=None,
                    help="seconds to run before a clean exit (multi-session "
                         "supervisor cycles)")
    args = ap.parse_args()
    if args.pron_interval != PRON_INTERVAL:
        PRON_INTERVAL = args.pron_interval
    try:
        main(args.username, args.password, args.max_steps, args.runtime,
             args.device_profile, args.profile_dir)
    except KeyboardInterrupt:
        log("stopped by user.")
        sys.exit(0)