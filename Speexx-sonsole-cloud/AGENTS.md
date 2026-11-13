# AGENTS.md — Speexx AutoCycle (standard packets / "sonsole")

Working directory: `/Users/blue/Projects/speexx/Speexx-sonsole` (note the spelling —
**sonsole**, not "soncole").
Platform: macOS (Apple Silicon). Python 3 via a project-local `.venv`. Node only for
running the solver self-check.

Sibling project: `/Users/blue/Projects/speexx/speaking` (the pronunciation bot).
Shared concepts (fingerprinting, multi-session, Accounts.md format) are documented in
`../speexx/AGENTS.md` — read that too if you work on both.

---

## What this is

An automation bot that drives **standard packets** (the reading/grammar/exercise flow at
`/articles/<id>/standard-packet/<n>/exercise/<m>`) so an account's course progress keeps
advancing unattended, indefinitely.

Two layers, and the split matters:

| Layer | File | Responsibility |
|---|---|---|
| **Orchestrator** (Python, Playwright) | `speexx_autocycle.py` | Login, navigation, deciding *what kind of page* we're on, start-gated exercises, pronunciation packets, packet roots, watchdogs, multi-session |
| **Solver** (JS, in-page) | `optimized.js` | The per-exercise cycle: patch the app's controls view, then `solve` → `correct` → `next`, escalating to packet advance when an exercise has no Next |

The orchestrator deliberately does **not** solve exercises; it only injects the solver and
supervises. The solver deliberately does **not** navigate between packets; it only works
inside the exercise it is on. Do not blur this boundary without reading both files.

---

## File inventory

| File | Lines | Purpose |
|---|---|---|
| `speexx_autocycle.py` | ~790 | The orchestrator. CLI entry point, all navigation decisions. |
| `check_level.py` | ~200 | `./check-level` worker. Logs in with each account's own profile and reports its CEFR level. |
| `check-level` | 9 | Wrapper that execs `check_level.py` through the project venv. |
| `optimized.js` | 551 | **The solver.** Production. Self-storing, self-resuming, patches `CourseExercisesControlsView`. Unchanged on disk by the orchestrator — the no-bounce variant is derived in Python (see below). |
| `working.js` | 111 | Historical prototype. Paste-into-console version of the view patch with a naive `solve/correct/next` loop baked into `initialize()`. **Superseded by `optimized.js`** — kept only for reference on how the view patch was derived. Do not paste it into a live page. |
| `check.js` | 41 | Node self-check for `optimized.js`'s *pure* logic only (ordinal parser, packet-root detector, `tryEval` heal). Copies of those regexes must stay byte-identical to `optimized.js`. |
| `bookmarklet.js` | 21 | Manual recovery: a bookmarklet that re-evals the solver source stashed in `localStorage`. Human-in-the-loop fallback; not used by the orchestrator. |
| `run.sh` | 29 | Single-account launcher. Creates `.venv`, installs deps + Chromium if missing, then execs the orchestrator. |
| `multi_run.sh` | 107 | N-account supervisor (parallel sessions, staggered, per-session logs, crash cycling). |
| `start.sh` | 36 | Kill old sessions → launch `multi_run.sh` in background. |
| `stop.sh` | 13 | Kill orchestrators, supervisors and browsers. |
| `Accounts.md` | — | Credentials, 2 lines per account. **Never paste credentials into docs, code or commits.** |
| `requirements.txt` | 1 | `playwright>=1.40` |

---

## Hard environment facts

- **Python venv is local**: `.venv` in this directory. Run everything through
  `./run.sh`, `multi_run.sh`, or `.venv/bin/python` — never bare `python3`.
- **Chromium comes from Playwright**, not from `/Applications`. Binary lives in
  `~/Library/Caches/ms-playwright/chromium-<rev>/`.
  **If someone clears their cache (or a Docker/cache-cleaner touches that path), every
  session dies instantly with `Executable doesn't exist at ...`. Fix:
  `.venv/bin/python -m playwright install chromium`.** This is the single most common
  "it won't start" failure — it looks like a code bug and isn't.
- **`node` is required only for `check.js`.** Installed via Homebrew at
  `/opt/homebrew/bin/node`.
- **Browser profile dirs are per account**, derived at runtime:
  `~/.speexx_autocycle_profile_<sha1(email)[:8]>` (`default_profile_dir()`). They are
  ~200 MB each. Deleting one just forces a fresh login for that account.
- Not a git repo. There is no `.gitignore`, no history, no branch protection — the code
  is the only backup. Be careful with bulk edits.

---

## URL grammar

Everything the orchestrator decides is derived from the URL:

```
https://portal.speexx.com/articles/<articleId>/standard-packet/<packetId>/exercise/<ordinal>
https://portal.speexx.com/articles/<articleId>/pronunciation-packet/<packetId>[/exercise/<ordinal>]
https://portal.speexx.com/articles/<articleId>                       # article root
https://portal.speexx.com/                                           # home/dashboard
```

- `PACKET_RE = /(pronunciation|standard)-packet/(\d+)/` → `url_kind()`, `url_packet()`
- `EXERCISE_RE = /exercise/(\d+)/` → `url_ordinal()`; **`findall` takes the LAST
  match**, because pronunciation URLs append a segment (`/exercise/21/speaking`).
- `url_kind(url) is None` ⇒ we're on home or an article root ⇒ `open_exercises()`.

`url_ordinal() is None` while a packet id exists ⇒ **packet root / summary page.** That
distinction drives two very different behaviours (see "Packet roots" below).

---

## The solver: `optimized.js`

### Self-storing, self-resuming design

The entire solver lives inside one named function, `__speexxAutoCycleMain()`, and its
first act is to write its own source into `localStorage`:

```js
localStorage.setItem("__speexxAutoCycleSrc", "(" + __speexxAutoCycleMain.toString() + ")();")
```

Why: a packet-boundary Continue causes a **full page reload**, which destroys the
controller and the view patch. On `load` / `pageshow`, if the resume flag
(`__speexxAutoCycleResume`) is set, the script re-evals the stored source — which
re-applies the view patch too, so the cycle keeps running on a patched view.
`beforeunload` sets the resume flag only while a controller is alive.

`tryEval()` self-heals a legacy broken copy: the stored form `function(){}();` is a
`SyntaxError` (declaration + stray call). On `SyntaxError` it strips the trailing `();`,
re-wraps as an IIFE, evals, and **overwrites** `localStorage` with the healed form.
`check.js` asserts both the broken and healed forms behave as claimed.

Idempotence guard: if `window.__speexxAutoCycleController._alive`, return immediately —
never start a second controller on one page.

### The view patch

Replaces the app's controls view class so the live view registers itself:

```js
CourseWare.CourseExercises.CourseExercisesControlsView =
  Backbone.Speexx.HandlebarsView.extend({ templateName: "cw-language-course-controls", className: "exercise-controls", ... })
```

The only behavioural change vs the app's own class is **passive registration**:
`initialize()` ends with `window.__speexxAutoCycleControls = this;` (and `onDestroy`
clears it). Everything else — `templateModel()`, the `modified` / `dialog:ended` /
`sync` / `change:textpool` listeners, the `events` map — is a faithful copy, because the
controller drives the view through those same events:

| Event | Wired to | Controller trigger point |
|---|---|---|
| `click .btn.solution` | `render()` + `trigger("solve")` | `solve` step |
| `click .btn.correct` | `CourseWare.Audio.stop()` + `trigger("correct")` | `correct` step (5 s later) |
| `click .btn.next` | `trigger("next")` | `advance()` |
| `keypress .btn.next` | `trigger("nextKeyboard")` | unused by controller |
| `click .btn.repeat` | `setStatic()` / re-render | unused by controller |

`templateModel()` computes `hasCorrect` / `hasSolution` / `hideResult` from
`type.hasResult`, `type.pronunciation`, `firstTime`, `result !== 100`. If you change the
app's expectations here the rendered controls go wrong and the whole cycle stalls.

If `CourseWare` / `Backbone` aren't loaded yet, the patch is skipped with a console WARN
and the controller falls back to **direct DOM clicks** (`clickControl(...)`) — degraded
but functional. That is why `wait_app_ready()` exists on the Python side.

### Controller

```js
window.__speexxAutoCycleController = { _alive: true, stop: function(){ ... } }
```

`stop()` sets `stopped`, clears every pending timer, and removes the resume flag. The
Python orchestrator calls it via `stop_controller()` before every re-inject.

All timers go through `addTimer(fn, delay)`, which tracks ids and swallows/logs throws —
**a throw inside a timer callback used to silently kill the entire cycle chain**, so this
wrapper is load-bearing, not decoration.

Delays (tuned by feel; changing them changes throughput and failure modes):

| Key | Value | Meaning |
|---|---|---|
| `init` | 500 ms | boot → first `runCycle` |
| `warmup` | 700 ms | between bounce steps |
| `correct` | 5000 ms | solve → correct (was 10 s in `working.js`) |
| `next` | 10000 ms | correct → advance |
| `reset` | 3000 ms | after an advance completes → next cycle |
| `skipPoll` | 600 ms | between "is there a Next?" polls |
| `skipMaxAttempts` | 8 | polls before escalating to packet advance |

### `runCycle()` decision tree

1. **`ordinal === null`** (no `/exercise/<n>`) — two sub-cases:
   - **On a packet root** (`/\/standard-packet\/\d+(?!\/exercise\/)/` **and** not
     `/exercise/`): this is an end-of-packet summary. Click a page-level
     primary/continue/next control, else fall back to a **text match** over a
     multi-language regex (`continue|next|weiter|avanti|…|继续|계속|…`). If the click
     didn't navigate within `waitForNewOrdinal`, **bump the packet id in the URL**
     (`standard-packet/<n+1>/exercise/1`). `pushState` alone doesn't fire the SPA router,
     hence the explicit `location.pathname` assignment.
   - **Not a packet root** (mid-navigation): poll up to 20×250 ms for the ordinal to
     settle, then `advance(null, …)` once as a last resort. The comment in the source
     matters: blind-clicking `.next` here caused the old `#null -> Next` infinite loop.
2. **Pronunciation sub-type** (`model.get("type").pronunciation`) → **skip solve/correct**
   and go straight to advance. Those have no `getResult()`, so "correct" would throw or
   hang; the orchestrator's `handle_pronunciation()` drives them instead.
3. Otherwise: `trigger("solve")` → after 5 s `trigger("correct")` → after 10 s
   `advance(ordinal, …)`. Any failure falls back to clicking `.solution` / `.correct`
   in the DOM.

### `advance()` escalation ladder

`done` is called exactly once — a watchdog timer guarantees the chain never dies.

1. View-scoped `view.$(".next")`.
2. Global `.exercise-controls .next, .btn.next, .next`.
3. If neither exists: poll every 600 ms, up to 8 attempts.
4. Then **pager icon** `.exercise-icon[data-ordinal="<n+1>"]` (jumps exercises, used to
   route around speaking sub-pages with no Next).
5. Then a page-level Next/Continue list (`.btn-next`, `.continue`, `[data-action=next]`, …).
6. Last resort: rewrite `standard-packet/<n>/exercise/<m>` → `standard-packet/<n+1>/exercise/1`
   and navigate.

### `boot()` and the exercise-1 bounce

```js
if (ordinal !== 1) { startCycle(); return; }
// ordinal 1: click exercise 2, wait, click exercise 1, wait, startCycle
```

Exercise 1's first view doesn't fully wire up, so the bounce primes it. **This is fatal
for start-gated exercises** (see below) — which is the single most important thing to
understand about this codebase.

---

## The orchestrator: `speexx_autocycle.py`

### The no-bounce transform

At import time, Python derives a second solver from `optimized.js` by string-replacing the
bounce block in `boot()` with `addTimer(startCycle, delays.warmup)`:

```python
_SOLVER_NO_BOUNCE = SOLVER.replace(<exact bounce block>, "    addTimer(startCycle, delays.warmup);")
if _SOLVER_NO_BOUNCE == SOLVER:
    raise RuntimeError("no-bounce transform did not apply to optimized.js (bounce block changed?)")
```

`optimized.js` on disk is never modified. **The `RuntimeError` guard is deliberate: if
anyone edits the bounce block in `optimized.js`, the import fails loudly instead of
silently regressing start-gated handling.** If you touch that block, update the Python
string to match exactly (whitespace included).

### Tuning constants

| Name | Where | Value | Effect |
|---|---|---|---|
| `PRON_INTERVAL` | module | 1.5 s | pause between pronunciation Next presses (override `--pron-interval`) |
| `DELAYS["poll"]` | module | 1.0 s | main loop observation tick — the heartbeat of everything |
| `DELAYS["after_login"]` | module | 7.0 s | settle after submitting login |
| `DELAYS["after_enter"]` | module | 3.0 s | settle after navigating into a packet |
| `DELAYS["after_inject"]` | module | 2.0 s | settle after evaluating the solver |
| `DELAYS["pron_next"]` | module | 3.0 s | fallback pause in `handle_pronunciation` |
| `DELAYS["settle"]` | module | 1.5 s | post-navigation settle |
| `STALL` | in `main()` | 60 s | idle on one exercise → `_nudge()` |
| `GATED_MAX_TRIES` | in `main()` | 5 | attempts before skipping a gated exercise |
| `GATED_VERIFY_TIMEOUT` | in `main()` | 25 s | no result after Start → fast retry |

### Loop order of operations (standard packets)

Per tick, in this order — the order is load-bearing:

1. `max_steps` / `runtime` checks.
2. Read URL; if login form present → `ensure_login()`, `continue`.
3. Off-portal URL → `goto(PORTAL)`.
4. `kind is None` → `open_exercises()`.
5. `kind == "pronunciation"` → `handle_pronunciation()`.
6. Packet root (`ordinal is None`) → wait for the app's own auto-advance to `exercise/1`;
   only if still stuck at root after ~3 s treat it as a real summary (click
   Continue-learning; after 6 attempts give up and go home).
7. Real exercise: update `last_key`/`last_enter`, reset gated state on key change.
8. **Gated verify** (see below) — may fast-retry or skip.
9. **`start_gated()` branch** (see below).
10. `controller_alive()`? No → `inject_solver()`; Yes → log and (after `STALL`) `_nudge()`.
11. Sleep `DELAYS["poll"]`.

### Start-gated exercises — the hard part

Some exercises render a **Start / "Take a very close look…" intro** with no controls view
registered. `start_gated(page)` returns true when a visible
`button.start-exercise, .start-exercise` exists.

Handled sequence (order matters):

1. `stop_controller(page)` — a stale controller would target a null view and its
   `advance()` would click the visible Next, **skipping the exercise**.
2. `clear_solver_state(page)` — drop the `__speexxAutoCycleResume` flag.
3. `wait_app_ready(page, timeout=10.0)` — Backbone/CourseWare must exist or the patch
   degrades to direct clicks. Longer than the normal 6 s because gated views are slower.
4. Inject **`_SOLVER_NO_BOUNCE`**. The bounce is fatal here: navigating 1→2→1 re-creates
   exercise 1's view *with the Start button back*, so solve/correct run against a re-gated
   view, the result stays null, and the packet wraps to exercise 1 forever.
5. `_click_start(page)` **immediately after the eval**, within the `warmup` window and
   *before* `startCycle` fires solve (~1.2 s later). Late clicks solve a view that's gone.
6. `wait_controls(page, timeout=12.0)` — confirm `window.__speexxAutoCycleControls` is
   live and not destroyed. Then **let the controller run; do not re-inject** (a second
   `boot()` re-bounces / restarts the cycle).

Anti-stall machinery around it:

- **Attempt cap**: `gated_tries >= GATED_MAX_TRIES` (5) → add the key to `skipped_gated`,
  `_skip_to_next()` and move on.
- **Skip set**: if the app wraps back to an already-skipped gated exercise, skip
  immediately instead of re-solving.
- **Verify / fast retry**: if Start was consumed but no `.result-badge-text` and no
  ordinal advance within `GATED_VERIFY_TIMEOUT`, re-inject no-bounce to restart the cycle
  instead of waiting for a slow wrap. Also capped, also ends in a skip.
- **Watchdog fix**: `last_enter` is refreshed **only on the first attempt** for a key, so
  the 60 s `STALL` nudge stays armed while retrying. (Refreshing it every pass silently
  disabled the watchdog — that's why the refresh is conditional on `gated_tries == 1`.)

`_skip_to_next(page, ordinal)`: pager icon for `ordinal+1` → `.next` family → packet-root
URL fallback.

### `_click_start()` vs `start_gated()`

They are deliberately different predicates. `start_gated()` is a *narrow* detection used to
route into the gated branch; `_click_start()` is a *broad* press used inside the branch and
by `_nudge()` — it tries `.start-exercise`, `.start`, `[data-action=start|play]`, then any
visible button whose text starts with `start|begin|play|…` (multi-language).

### Known race (real, observed, not yet fixed)

On a cold arrival the Start button can render slightly after the orchestrator's first
poll of a new exercise. If `start_gated()` is false on that single tick, the bouncing
solver gets injected and the gate is consumed, leaving "solver alive" forever on that
ordinal. In practice it self-heals (the bounce re-gates and the next poll catches it), but
it can cost a minute. The robust fix, if it ever becomes a problem, is to poll
`start_gated()` for ~2.5 s on `key != last_key` before deciding to inject — deliberately
not done yet because it adds latency to *every* exercise transition.

### Login

`ensure_login()` is passive: if neither `input[name="userName"]` nor
`input[name="password"]` exists, it does nothing (session already valid). Otherwise fill
username → `button.button-login` → wait 4 s → fill password → `button.button-login` →
sleep `after_login`. Note these are **name-attribute** selectors, unlike the speaking
project's `#userName` / `#button-next` / `#password` / `#button-sign-in` ids.

Because it is passive, **a profile that holds a different account's session will silently
run as that other account.** Per-account profile dirs (below) prevent this; don't "fix" it
by pointing several accounts at one shared dir.

### Pronunciation packets

`handle_pronunciation()` stops the controller, then: mid-packet → click `.next` and sleep
`PRON_INTERVAL`; at a packet root → up to 4 tries for a Continue-learning button, else
click `.next`. The solver's own pronunciation branch is skipped (no `getResult()`).

---

## Multi-session

Mirrors the speaking project's design. **Speexx allows one account per "device"**; a web
app fingerprints IP + browser, and all sessions on this Mac share an IP — so each parallel
session gets a distinct **device profile**.

`DEVICE_PROFILES` (6 entries, index modulo 6) varies UA, viewport, timezone, locale and a
seeded canvas-noise seed. `multi_run.sh` assigns profile `1..N`. Profile 0 means "minimal
change": `user_agent` and `timezone_id` are omitted entirely (real UA, system timezone, no
canvas noise) so a plain single run looks like an ordinary browser — but note the viewport
is still forced to the profile's size (1280x800) and locale to `en-US`.

Canvas noise (`_canvas_noise_source(seed)`) perturbs `toDataURL` / `toBlob` output
deterministically (LCG seeded per profile) so canvas fingerprints differ. Injected with
Playwright's `context.add_init_script()`.

| Flag | Default | Meaning |
|---|---|---|
| `--device-profile N` | 0 | fingerprint profile 0-5; **parallel sessions must differ** |
| `--profile-dir PATH` | per-account | browser profile dir override |
| `--runtime SECONDS` | none | clean exit after N s (exit 0) so the supervisor can cycle |
| `--max-steps N` | none | stop after N loop iterations |
| `--pron-interval S` | 1.5 | pronunciation Next pause |

Per-account default profile dir: `~/.speexx_autocycle_profile_<sha1(email)[:8]>`, via
`default_profile_dir()`. Deterministic, so the same account keeps its session across
restarts, and two accounts can never collide.

`multi_run.sh`: parses `Accounts.md` (accepts `email password` on one line **or** email
line + password line), launches one background session per account, **15 s stagger**
(avoids simultaneous logins), `CYCLE_CAP=1800` per Python cycle, rest 15 s after a clean
cycle / 30 s after a failed one, `trap stop_all INT TERM`. Logs:
`~/speexx_autocycle_multi_run.log` (master) and
`~/speexx_autocycle_multi/session<N>_<local-part>.log` + `.log.console` (per session —
the Python stdout redirect; the `.log` file only gets supervisor lifecycle lines).

```bash
./start.sh                 # 3.5 h window, all accounts in Accounts.md
./start.sh 7200            # 2 h
./start.sh 12600 Accounts.md
./stop.sh                  # kill everything
tail -f ~/speexx_autocycle_multi_run.log
tail -f ~/speexx_autocycle_multi/session1_*.log.console
```

Note the master log's final two lines are easy to misread: `stop_all()` logs "All sessions
stopped" whenever the trap fires, including when a tool/test harness kills the supervisor.

### Docker

**Not used here, deliberately.** There is no Dockerfile; sessions run the local `.venv`
against locally-installed Chromium. Don't copy the speaking project's Docker path over —
it adds nothing (containers share the host IP and browser build) and Playwright's Chromium
is managed locally anyway.

---

## How to run

```bash
# single account (creates .venv + Chromium on first run)
./run.sh <email> <password>

# all accounts, 3.5 h, in parallel
./start.sh 12600

# short supervised run with an explicit list
./multi_run.sh 300 /tmp/accounts_test.md

# single account, one cycle, then clean exit (used for smoke tests)
.venv/bin/python speexx_autocycle.py <email> <password> --runtime 60 --device-profile 2
```

Always stop with `./stop.sh` — it clears orchestrators, supervisors and
`chrome-mac-arm64` together. A hard `kill -9` can leave an orphaned browser holding a
profile lock, which then fails the next launch.

---

## Checking levels (`./check-level`)

`./check-level [accounts_file] [--headed]` reports every account's CEFR level
(A1..C2, e.g. `B2.1`). It is deliberately read-only: it never touches packets,
exercises or the solver.

- Reads the accounts file with the same two formats `multi_run.sh` accepts
  (email+password on one line, or on two consecutive lines; `#` comments skipped).
- Reuses each account's **AutoCycle browser profile dir**
  (`~/.speexx_autocycle_profile_<sha1(email)[:8]>`), so accounts that have run
  the bot before are usually already logged in.
- The level comes from the portal's own JSON, discovered by probing a live
  account (2026-11-12): after login the app lands on `/articles/<articleId>/`
  and `GET /articles/<articleId>/initial-data` returns `currentLevel`
  (e.g. `"B2.1"`), `currentLevelId`, and `courseLanguageUsage`. The script
  captures that response and only falls back to scanning visible page text for
  a CEFR token if the API response never arrives.
- **Stop the bot first (`./stop.sh`).** A running AutoCycle session holds the
  account's profile dir; the check reports that account as `profile busy`
  instead of fighting over it. Headless by default; `--headed` to watch.
- The table has a fourth column, `แบบฝึกหัด`: the exercises progress
  percentage from the article's own `/results` page (e.g. `0%`). The row is
  matched by localized label (Thai first, then Exercises/Übungen/Ejercicios/…),
  so it works on the Thai portal. Found after the level is read by navigating
  to `/articles/<id>/results`; `-` if the id or row can't be found.
- Verification on 2026-11-12: all 4 accounts in `Accounts.md` reported
  (B2.1, A2, C1.1, B2.1). A sublevel may be absent — one account reports plain
  `A2` with no `.n` suffix; that is the portal's value, not a parsing gap.

---

## Testing / verification

```bash
.venv/bin/python -m py_compile speexx_autocycle.py   # syntax
.venv/bin/python -m py_compile check_level.py         # syntax (./check-level worker)
node check.js                                          # solver pure-logic self-check
bash -n run.sh multi_run.sh start.sh stop.sh            # shell syntax
```

`check.js` deliberately covers **only** the pure regex helpers (ordinal parser,
packet-root detector, `tryEval` heal) — DOM and timer logic isn't unit-testable as
written. If you change any of those three regexes, change the copy in `check.js` in the
same commit; the file says so at the top. **Passing `check.js` does not mean the solver
works in the browser** — only a live run proves that.

Live smoke test (what "working" looks like):

```bash
printf '<email>\n<password>\n' > /tmp/accounts_smoke.md
./multi_run.sh 90 /tmp/accounts_smoke.md
tail -f ~/speexx_autocycle_multi/session1_*.log.console
```

Healthy output shows `login form detected` → `submitted login` → `packet <id> root ->
settled to ord` → `standard <id> ord=1 -> inject solver` → `solver alive, letting it
run`, with the ordinal incrementing every ~15 s.

---

## Gotchas (each one cost real debugging time)

1. **Never inject the solver on a packet root.** CourseWare/Backbone aren't loaded there,
   and the solver's own packet-root handler would hop to the *next* packet instead of
   entering this one's exercises. The orchestrator waits for the app's own auto-advance to
   `exercise/1` and only treats a lingering root as a summary.
2. **The exercise-1 bounce is fatal on start-gated exercises** → always inject
   `_SOLVER_NO_BOUNCE` there. See the transform's `RuntimeError` guard.
3. **`start_gated()` must be checked before injecting the bouncing solver**, or the gate
   gets consumed and the packet stalls on one ordinal.
4. **Click Start fast.** Within ~1.2 s of the eval, before `startCycle` fires.
5. **A stale controller on a gated exercise will skip it** (its `advance()` clicks the
   visible Next). Always `stop_controller()` first.
6. **`WARN: app not ready before inject` means degraded mode**, not failure — the solver
   falls back to direct DOM clicks and can't fill blanks. It's benign at packet entry but
   a reason to prefer the longer `timeout=10.0` wait in the gated branch.
7. **Packet ids are per account.** Never hardcode an article/packet id from another
   account; let `open_exercises()` discover it from the DOM. (The speaking project caches
   per-account article ids for the same reason.)
8. **One profile per account.** A shared profile silently runs as whichever account logged
   in last — this actually happened here (a "48624" run was really 50753).
9. **Cleared Playwright cache ⇒ instant "Executable doesn't exist".** Not a code bug.
10. **`node check.js` passing ≠ the bot works.** It's regex-only coverage.
11. **Don't re-inject inside the gated branch after clicking Start.** A second `boot()`
    restarts the cycle and loses the started view.
12. **`Popen`-style background runs launched from a tool harness get killed** when the
    harness call ends, which can look like "the supervisor stopped by itself". Use
    `nohup`/`start.sh` for long runs.

---

## Rules for changes

- **Keep `optimized.js` as the single source of truth.** Python derives variants from it by
  exact string replacement; changing the bounce block or the view patch breaks the
  transform guard.
- **Sync `check.js` with `optimized.js`** for any change to the three pure helpers.
- **Never commit credentials.** `Accounts.md` holds them; docs and code reference the file
  or use `<email> <password>` placeholders.
- **Multi-session changes must keep profiles 1..N distinct** and profiles per account.
- **Don't add a Docker path** unless someone actually asks; the local venv is the supported
  setup.
- Prefer native `element.click()` over synthetic events for app buttons
  (`_click_native`), because the app's own handlers sometimes ignore untrusted events.