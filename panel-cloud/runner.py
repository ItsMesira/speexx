"""Standard-packet runner for the panel.

Self-contained on purpose: nothing here imports from or modifies the sibling
Speexx-soncole project. The in-page solver (solver.js) is a byte-identical copy
of the proven file, and the orchestrator below keeps the same navigation rules.

Differences from that original, all deliberate:

  * Ephemeral browser context. `launch()` + `new_context()` instead of
    `launch_persistent_context(dir)`, so no cookies, localStorage or profile
    directory survives the process. The sibling project documents a real
    incident where a shared profile silently ran as the wrong account; an
    in-memory context makes that class of bug impossible rather than unlikely.
  * Credentials arrive on stdin, never argv. argv is world-readable through ps.
  * Login is verified positively and classified; bad_credentials and locked
    abort immediately with no retry. See preflight.py.
  * Lessons are counted and capped, and progress is announced on stdout with
    machine-readable markers the panel parses.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import preflight

HERE = Path(__file__).resolve().parent
PORTAL = "https://portal.speexx.com"
SOLVER = (HERE / "solver.js").read_text(encoding="utf-8")


# ── Cloud / no-display launch options ───────────────────────────────────────
# Identical behaviour to the local run; only the browser window changes.
# Colab, Kaggle, Codespaces and devcontainers have no display (headless) and
# root runs need the sandbox disabled (--no-sandbox).
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
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    if root or os.environ.get("SPEEXX_NO_SANDBOX") == "1":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    return args

EXIT_OK = 0
EXIT_LOGIN_FATAL = 3  # bad password or locked — deliberately distinct
EXIT_ERROR = 4

# ── Device fingerprints ───────────────────────────────────────────────────
# Speexx permits one account per device and identifies a device by IP plus
# browser fingerprint. Every session on this machine shares one public IP, so
# concurrent jobs must differ. Six profiles, index taken modulo six; profile 0
# is "change nothing" and is never leased by the panel.
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

PACKET_RE = re.compile(r"(pronunciation|standard)-packet/(\d+)")
EXERCISE_RE = re.compile(r"/exercise/(\d+)")


def canvas_noise_source(seed: int) -> str:
    """Deterministic per-profile canvas perturbation (same LCG as the original)."""
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


# ── Output ────────────────────────────────────────────────────────────────

_SECRETS: list[str] = []


def log(msg: str) -> None:
    """Print one line to stdout, with every known secret scrubbed out.

    The panel persists stdout, so a credential that leaks into a log line
    leaks to disk. Scrubbing at the single choke point every message passes
    through is cheaper to get right than trying to remember at each call site.
    """
    text = str(msg)
    for secret in _SECRETS:
        if secret and len(secret) >= 4 and secret in text:
            text = text.replace(secret, "[redacted]")
    print(text, flush=True)


def event(kind: str, message: str) -> None:
    """A line the panel parses for state. Kept on one line and greppable."""
    log("%s %s" % (kind, message))


def read_credentials() -> tuple[str, str]:
    """email on line 1, password on line 2, both from stdin.

    Deliberately not argv and not a file: argv is visible to every process on
    the machine via ps, and a file has to be cleaned up.
    """
    data = sys.stdin.read().split("\n", 1)
    if len(data) < 2:
        raise SystemExit("expected email and password on stdin")
    email, password = data[0].strip(), data[1].rstrip("\n")
    if password:
        _SECRETS.append(password)
    if not email or not password:
        raise SystemExit("empty credentials on stdin")
    return email, password


# ── URL helpers ───────────────────────────────────────────────────────────

def url_kind(url: str) -> str | None:
    m = PACKET_RE.search(url or "")
    return m.group(1) if m else None


def url_packet(url: str) -> str | None:
    m = PACKET_RE.search(url or "")
    return m.group(2) if m else None


def url_ordinal(url: str) -> int | None:
    matches = EXERCISE_RE.findall(url or "")
    # LAST match, because pronunciation URLs append a segment
    # (/exercise/21/speaking).
    return int(matches[-1]) if matches else None


# ── Solver variants ───────────────────────────────────────────────────────

# The exercise-1 bounce primes exercise 1's view, which fails to wire up on
# first paint. It is fatal on start-gated exercises: bouncing 1 -> 2 -> 1
# re-creates the view with the Start button back, so solve/correct fire
# against a re-gated view and the packet wraps to exercise 1 forever. Hence the
# no-bounce variant for the gated branch.
_BOUNCE_BLOCK = """    if (ordinal !== 1) {
      startCycle();
      return;
    }
    addTimer(function () {
      if (!clickExerciseOrdinal(2)) { startCycle(); return; }
      waitForOrdinal(2, function () {
        addTimer(function () {
          if (!clickExerciseOrdinal(1)) { startCycle(); return; }
          waitForOrdinal(1, startCycle);
        }, delays.warmup);
      });
    }, delays.warmup);"""

SOLVER_NO_BOUNCE = SOLVER.replace(
    _BOUNCE_BLOCK, "    addTimer(startCycle, delays.warmup);"
)
if SOLVER_NO_BOUNCE == SOLVER:
    # Fail loudly rather than silently regressing start-gated handling if
    # someone edits the bounce block in solver.js.
    raise RuntimeError(
        "no-bounce transform did not apply to solver.js (the boot() bounce block changed?)"
    )


# ── Login ─────────────────────────────────────────────────────────────────

def submit_login(page, email: str, password: str) -> None:
    """Fill and submit the two-step login form. Raises if the form is absent."""
    if page.locator('input[name="userName"]').count() > 0:
        page.locator('input[name="userName"]').first.fill(email)
        page.locator("button.button-login").first.click()
        time.sleep(3.0)
    if page.locator('input[name="password"]').count() > 0:
        page.locator('input[name="password"]').fill(password)
        page.locator("button.button-login").click()
    else:
        raise RuntimeError("password field never appeared")


#: A login is accepted only once the "off /login, no form" state has held across
#: this many consecutive polls. The dashboard can render slowly, and the app
#: briefly passes through pages while it navigates.
LOGIN_STABLE_CHECKS = 2
LOGIN_SETTLE_SECONDS = 25.0
LOGIN_POLL_SECONDS = 1.2


def login_once(page, email: str, password: str) -> tuple[bool, str]:
    """One login attempt. Returns (ok, reason). Never raises on a rejection.

    Polls rather than sleeping a fixed interval: the old fixed four-second sleep
    was too short for a slow dashboard, and combined with a text-only success
    check it reported a perfectly good login as a failure.
    """
    if not preflight.login_form_present(page):
        return preflight.verify(page)

    submit_login(page, email, password)

    deadline = time.time() + LOGIN_SETTLE_SECONDS
    stable = 0
    reason = preflight.UNKNOWN
    while time.time() < deadline:
        time.sleep(LOGIN_POLL_SECONDS)
        ok, reason = preflight.verify(page)
        if reason in preflight.FATAL:
            # A wrong password or a lock is definitive the moment it appears.
            # Returning here is what keeps retries from ever happening.
            return False, reason
        if ok:
            stable += 1
            if stable >= LOGIN_STABLE_CHECKS:
                log("login settled (%s confirmation)" % preflight.describe_login(page))
                return True, preflight.OK
        else:
            stable = 0
    return False, reason


def ensure_login(page, email: str, password: str, max_attempts: int = 3) -> str:
    """Log in, or return the reason we gave up.

    The two fatal reasons return immediately with no retry at all. That is the
    entire point of this function: a wrong password must never be submitted
    twice, because repeated failed logins are what get an IP locked.
    """
    backoff = [0.0, 60.0, 120.0, 240.0]
    reason = preflight.UNKNOWN

    for attempt in range(1, max_attempts + 1):
        wait = backoff[min(attempt - 1, len(backoff) - 1)]
        if wait:
            log("login attempt %d failed (%s); waiting %.0fs before retrying"
                % (attempt, reason, wait))
            time.sleep(wait)

        try:
            ok, reason = login_once(page, email, password)
        except Exception as exc:
            reason = preflight.UNKNOWN
            log("login attempt %d raised: %s" % (attempt, exc))

        if ok:
            return preflight.OK

        if reason in preflight.FATAL:
            # Not a single retry. Not a backoff. Not ever.
            return reason

        log("login attempt %d did not succeed (%s)" % (attempt, reason))

    return reason


# ── Page interaction ──────────────────────────────────────────────────────

def _click_native(page, selectors: list[str], label: str) -> bool:
    """Native element.click(), not synthetic events: the app ignores untrusted ones."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                log("%s: clicked %s" % (label, sel))
                return True
        except Exception:
            continue
    return False


def _click_start(page) -> bool:
    """Broad Start press. Distinct from start_gated(), which is a narrow detector."""
    if _click_native(page, [".start-exercise", "button.start-exercise", ".start",
                            '[data-action="start"]', '[data-action="play"]'], "start"):
        return True
    try:
        return bool(page.evaluate("""() => {
            const re = /^(start|begin|play|เริ่ม|开始|開始|시작|starten|commencer)/i;
            for (const b of document.querySelectorAll('button, a.btn, [role=button]')) {
              const t = (b.innerText || '').trim();
              if (t && re.test(t) && b.offsetParent !== null) { b.click(); return true; }
            }
            return false;
        }"""))
    except Exception:
        return False


def start_gated(page) -> bool:
    """Narrow detector: a visible Start gate is on screen."""
    try:
        return page.locator("button.start-exercise, .start-exercise").count() > 0
    except Exception:
        return False


def app_ready(page) -> bool:
    """Backbone/CourseWare exist, so the solver's view patch can install."""
    try:
        return bool(page.evaluate(
            "typeof CourseWare !== 'undefined' && CourseWare.CourseExercises && "
            "typeof Backbone !== 'undefined' && Backbone.Speexx && "
            "Backbone.Speexx.HandlebarsView"
        ))
    except Exception:
        return False


def wait_app_ready(page, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_ready(page):
            return True
        time.sleep(0.4)
    return False


def controller_alive(page) -> bool:
    try:
        return bool(page.evaluate(
            "!!(window.__speexxAutoCycleController && "
            "window.__speexxAutoCycleController._alive)"
        ))
    except Exception:
        return False


def clear_solver_state(page) -> None:
    try:
        page.evaluate("try{localStorage.removeItem('__speexxAutoCycleResume');}catch(e){}")
    except Exception:
        pass


def stop_controller(page) -> None:
    try:
        page.evaluate(
            "if(window.__speexxAutoCycleController){"
            "window.__speexxAutoCycleController.stop();}"
        )
    except Exception:
        pass


def inject_solver(page, no_bounce: bool = False) -> None:
    clear_solver_state(page)
    if not wait_app_ready(page):
        log("WARN: app not ready before inject (solver will use direct clicks)")
    try:
        page.evaluate(SOLVER_NO_BOUNCE if no_bounce else SOLVER)
        log("injected solver.js%s" % (" (no-bounce)" if no_bounce else ""))
    except Exception as exc:
        log("inject error: %s" % exc)
    time.sleep(2.0)


def controls_ready(page) -> bool:
    try:
        return bool(page.evaluate(
            "!!(window.__speexxAutoCycleControls && !window.__speexxAutoCycleControls.isDestroyed)"
        ))
    except Exception:
        return False


def wait_controls(page, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if controls_ready(page):
            return True
        time.sleep(0.5)
    return False


def open_exercises(page) -> bool:
    """From home or an article root, click into a standard packet.

    Returns False when the page offers no packet at all — which is what an
    account sitting at the end of its level looks like. The caller needs that
    answer, otherwise it re-polls the same empty page until the time cap.
    """
    for _ in range(3):
        if page.locator('a[href*="standard-packet"], a[href*="pronunciation-packet"]').count():
            break
        time.sleep(2.0)
    loc = page.locator('a[href*="standard-packet"]')
    for i in range(loc.count()):
        href = loc.nth(i).get_attribute("href") or ""
        if "support" in href or "exercise/" in href:
            continue
        target = href if href.startswith("http") else PORTAL + href
        path = urlparse(target.split("?")[0]).path
        if not re.search(r"/articles/\d+/standard", path):
            continue
        log("open exercises -> %s" % path)
        try:
            page.goto(target, timeout=30000, wait_until="domcontentloaded")
            return True
        except Exception as exc:
            log("open error: %s" % exc)
    return False


def handle_pronunciation(page, ordinal) -> None:
    """Pronunciation packets have no getResult(); just walk Next through them."""
    stop_controller(page)
    if ordinal is None:
        for _ in range(4):
            if _click_native(page, [".continue", ".btn-continue"], "pron continue"):
                time.sleep(1.5)
                break
            time.sleep(1.0)
    _click_native(page, ["button.next", "button.nxt-exercise", "a.next"], "pron next")
    time.sleep(1.5)


# ── Main loop ─────────────────────────────────────────────────────────────

#: Consecutive polls that may find no packet before the job gives up.
EMPTY_POLL_LIMIT = 3
GATED_MAX_TRIES = 5
GATED_VERIFY_TIMEOUT = 25.0
STALL = 60.0


def run(page, email: str, password: str, max_levels: int | None,
        profile_index: int, fast: bool, max_seconds: float | None = None) -> int:
    prof = DEVICE_PROFILES[profile_index % len(DEVICE_PROFILES)]
    poll = 0.45 if fast else 1.0
    log("device profile %d (%dx%d %s %s) poll=%.2fs"
        % (profile_index, prof["size"][0], prof["size"][1], prof["lang"],
           prof["tz"] or "system-tz", poll))

    try:
        page.goto(PORTAL, timeout=60000, wait_until="domcontentloaded")
    except Exception as exc:
        log("initial goto error: %s" % exc)

    # ── Login gate. Nothing else runs until this positively succeeds. ──
    reason = ensure_login(page, email, password)
    if reason != preflight.OK:
        event("LOGIN_FAILED", "reason=%s" % reason)
        if reason in preflight.FATAL:
            log("refusing to retry a %s login; stopping to protect the IP" % reason)
        return EXIT_LOGIN_FATAL
    event("LOGIN_OK", "verified=%s" % hashlib.sha256(email.encode()).hexdigest()[:8])

    # A LEVEL is the unit of work; a PACKET is the subunit. A level holds many
    # packets (an account can be around seventy in one), so counting packets as
    # "lessons" would stop a fifth of the way through.
    levels_done = 0
    level_packets = 0     # packets finished in the level currently being worked
    total_packets = 0    # packets finished across the whole job
    current_packet: str | None = None
    worked_packets: set[str] = set()
    in_level = False     # has at least one packet been finished since we started?

    def finish_packet(packet: str | None) -> None:
        nonlocal level_packets, total_packets, current_packet, in_level
        level_packets += 1
        total_packets += 1
        in_level = True
        event("PACKET_DONE", "n=%d packet=%s level_packets=%d"
              % (level_packets, packet or "?", level_packets))
        current_packet = None

    def finish_level() -> bool:
        """Level exhausted. Returns True once the cap is reached.

        Detection: the app navigates back to the article root (url_kind() is
        None) *after* at least one packet was finished. Both bot projects agree
        on that signal — the speaking one names it "the bare /articles/<id>
        page" and uses it to re-navigate for sustain mode. Counting only when a
        packet is already behind us stops the root landing *before* the level
        starts from being mistaken for its end.
        """
        nonlocal levels_done, level_packets, in_level
        if not in_level:
            return False
        levels_done += 1
        event("LEVEL_DONE", "n=%d packets=%d" % (levels_done, level_packets))
        level_packets = 0
        in_level = False
        worked_packets.clear()
        return bool(max_levels and levels_done >= max_levels)

    last_key = None
    last_enter = None
    last_nudge = 0.0
    summary_tries = 0
    last_summary_pkt = None
    gated_key = None
    gated_tries = 0
    gated_entered = 0.0
    skipped_gated: set[tuple] = set()
    empty_polls = 0
    started = time.time()

    while True:
        if max_seconds and time.time() - started > max_seconds:
            # A stall that never reaches the level end must not leak a browser
            # and a profile slot forever. Reported as a failure, not a done.
            event("JOB_DONE", "levels=%d packets=%d reason=time_cap"
                  % (levels_done, total_packets))
            return EXIT_ERROR
        try:
            url = page.url
        except Exception:
            time.sleep(poll)
            continue

        # A login form here means the session dropped mid-run. Re-authenticate,
        # but never more than the bounded retries inside ensure_login.
        if preflight.login_form_present(page):
            reason = ensure_login(page, email, password)
            if reason != preflight.OK:
                event("LOGIN_FAILED", "reason=%s phase=midrun" % reason)
                return EXIT_LOGIN_FATAL
            event("LOGIN_OK", "recovered=%s" % hashlib.sha256(email.encode()).hexdigest()[:8])
            # Losing the session sends us back through the root, which is the
            # level-end signal. Clearing in_level stops that bounce from being
            # counted as a completed level; the packet tally is kept.
            current_packet, worked_packets, in_level = None, set(), False
            continue

        if not url.startswith(PORTAL):
            try:
                page.goto(PORTAL, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(poll)
            continue

        kind = url_kind(url)
        ordinal = url_ordinal(url)

        if kind is None:
            # Home or article root. With packets behind us this is the level
            # running out, so count it and go back in for the next one.
            if finish_level():
                break
            log("home/root -> open exercises")
            if not open_exercises(page):
                # Nothing to enter. Repeating this is how the first live run
                # burned eight minutes on an account that had already finished
                # its level. Say so plainly and stop.
                empty_polls += 1
                log("no packet available (%d/%d) — nothing left to do on this account"
                    % (empty_polls, EMPTY_POLL_LIMIT))
                if empty_polls >= EMPTY_POLL_LIMIT:
                    if in_level:
                        # A level WAS in progress and is now exhausted.
                        finish_level()
                    event("JOB_DONE", "levels=%d packets=%d reason=no_work"
                          % (levels_done, total_packets))
                    return EXIT_ERROR
                time.sleep(poll)
                continue
            empty_polls = 0
            summary_tries = 0
            time.sleep(poll)
            continue

        if kind == "pronunciation":
            handle_pronunciation(page, ordinal)
            time.sleep(poll)
            continue

        pk = url_packet(url)
        key = (pk, ordinal)
        now = time.time()

        if pk != current_packet:
            if current_packet and current_packet in worked_packets:
                finish_packet(current_packet)
            current_packet = pk

        if ordinal is None:
            # Packet root. Never inject here: CourseWare is not loaded and the
            # solver's own root handler would hop to the NEXT packet instead of
            # entering this one. Wait for the app's own advance into exercise 1.
            settled = False
            for _ in range(6):
                time.sleep(0.5)
                if url_ordinal(page.url) is not None:
                    settled = True
                    break
            if settled:
                log("packet %s root -> settled to ord" % pk)
                time.sleep(poll)
                continue
            _click_native(page, [".btn-next", ".continue", '[data-action="next"]',
                                  "button.next"], "summary")
            if pk != last_summary_pkt:
                last_summary_pkt, summary_tries = pk, 0
            summary_tries += 1
            if summary_tries >= 6:
                log("packet %s stuck at root -> home" % pk)
                last_summary_pkt, summary_tries = None, 0
                try:
                    page.goto(PORTAL, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass
            time.sleep(poll)
            continue

        summary_tries = 0
        worked_packets.add(pk)

        if key != last_key:
            last_key, last_enter = key, now
        if gated_key and gated_key != key:
            gated_key, gated_tries, gated_entered = None, 0, 0.0

        # Start-gated branch. Order matters:
        #   1. stop a stale controller — its advance() would click the visible
        #      Next and skip the exercise outright;
        #   2. inject the no-bounce variant, because bouncing re-creates the
        #      view with the Start button back and the packet wraps forever;
        #   3. click Start immediately, before startCycle fires solve.
        if start_gated(page):
            gk = (pk, ordinal)
            if gk != gated_key:
                gated_key, gated_tries, gated_entered = gk, 0, 0.0
            if gk in skipped_gated:
                stop_controller(page)
                _click_native(page, ['.exercise-icon[data-ordinal="%d"]' % (ordinal + 1),
                                      "button.next", ".btn.next"], "skip-gated")
                gated_key, gated_tries = None, 0
                time.sleep(poll)
                continue
            if gated_tries >= GATED_MAX_TRIES:
                log("start-gated gave up after %d tries -> skipping" % gated_tries)
                skipped_gated.add(gk)
                stop_controller(page)
                _click_native(page, ['.exercise-icon[data-ordinal="%d"]' % (ordinal + 1),
                                      "button.next", ".btn.next"], "skip-gated")
                gated_key, gated_tries = None, 0
                time.sleep(poll)
                continue
            gated_tries += 1
            log("standard %s ord=%s start-gated (attempt %d/%d)"
                % (pk, ordinal, gated_tries, GATED_MAX_TRIES))
            stop_controller(page)
            clear_solver_state(page)
            if not wait_app_ready(page, timeout=10.0):
                log("WARN: app not ready before inject (will use fallback)")
            try:
                page.evaluate(SOLVER_NO_BOUNCE)
                log("standard %s ord=%s injected no-bounce solver" % (pk, ordinal))
            except Exception as exc:
                log("inject error: %s" % exc)
            _click_start(page)
            wait_controls(page, timeout=12.0)
            gated_entered = time.time()
            if gated_tries == 1:
                # Only on the first attempt, so the STALL watchdog stays armed
                # while we retry. Refreshing every pass silently disabled it.
                last_key, last_enter = (pk, ordinal), time.time()
            time.sleep(poll)
            continue

        if not controller_alive(page):
            log("standard %s ord=%s -> inject solver" % (pk, ordinal))
            inject_solver(page)
            last_key, last_enter = (pk, url_ordinal(page.url)), time.time()
        else:
            log("standard %s ord=%s -> solver alive" % (pk, ordinal))
            if last_enter is not None and now - last_enter > STALL and now - last_nudge > STALL:
                log("stalled at %s -> nudging Next" % (str(last_key),))
                _click_native(page, ["button.next", ".btn.next"], "nudge")
                last_nudge = now
        time.sleep(poll)

    event("JOB_DONE", "levels=%d packets=%d" % (levels_done, total_packets))
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="Speexx standard-packet runner")
    ap.add_argument("username", nargs="?", help="ignored when --credentials-stdin is used")
    ap.add_argument("password", nargs="?", help="ignored when --credentials-stdin is used")
    ap.add_argument("--credentials-stdin", action="store_true",
                    help="read 'email\\npassword' from stdin (the panel's path; "
                         "keeps credentials out of argv and out of ps)")
    ap.add_argument("--max-levels", type=int, default=1,
                    help="stop after this many completed levels (default: 1)")
    ap.add_argument("--max-seconds", type=float, default=3 * 3600,
                    help="hard wall-clock cap so a stalled run cannot leak "
                         "forever; the job exits non-zero having done less")
    ap.add_argument("--device-profile", type=int, default=0, help="fingerprint 0-5")
    ap.add_argument("--fast", action="store_true",
                    help="tighter poll interval; unproven against the default timings")
    args = ap.parse_args()

    if args.credentials_stdin:
        email, password = read_credentials()
    elif args.username and args.password:
        email, password = args.username, args.password
        _SECRETS.append(password)
    else:
        ap.error("need --credentials-stdin, or a username and password")

    prof = DEVICE_PROFILES[args.device_profile % len(DEVICE_PROFILES)]
    launch_opts = {
        "headless": _HEADLESS,
        "args": _launch_args(),
    }
    # Context options only. headless and args belong to launch(); mixing them is
    # the bug that makes an ephemeral context impossible to express.
    ctx_opts = {"viewport": {"width": prof["size"][0], "height": prof["size"][1]},
                "locale": prof["lang"]}
    if prof["ua"]:
        ctx_opts["user_agent"] = prof["ua"]
    if prof["tz"]:
        ctx_opts["timezone_id"] = prof["tz"]

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        try:
            # Ephemeral by construction: nothing is written to disk and no state
            # survives this context, so one job can never inherit another's login.
            context = browser.new_context(**ctx_opts)
            if prof["seed"]:
                context.add_init_script(canvas_noise_source(prof["seed"]))
            page = context.new_page()
            return run(page, email, password, args.max_levels, args.device_profile,
                       args.fast, args.max_seconds)
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopped by signal")
        sys.exit(EXIT_ERROR)
