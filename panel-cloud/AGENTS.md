# AGENTS.md — Speexx Panel

Working directory: `/Users/blue/Projects/speexx/panel`. Self-contained project:
own venv, own SQLite database, own browser orchestration. **It must never import
from or modify `../Speexx-sonsole` or `../speaking`.** Those are read-only
references.

macOS (Apple Silicon). Python 3.14. Playwright 1.62.0.

---

## What this is

A multi-user web panel. **Anyone can sign up and sign in.** They save their Speexx
login once, and press **run** — but only once the admin has given them a **six digit
key**. Without a key they can sign in and look around; they cannot run anything.

One run works through **one whole level** — every packet in it — then stops by itself
and reports done.

People sign in with a **username**. There is **no email address** anywhere in the
panel's own accounts — not as an identifier, not as a column, not in the admin list.
The password minimum is **8** characters.

The one address the panel still holds is the **Speexx** login, stored per saved
account in `speexx_accounts.email`. That one is not optional: it is the portal's
credential, and the runner cannot log in without it.

| Page | Route | Who |
|---|---|---|
| Landing | `/` | anyone |
| Sign up | `/signup` | anyone; the first one owns the panel, the rest queue |
| Sign in | `/login` | anyone |
| Dashboard | `/dashboard` | a signed-in person |
| Admin | `/admin` | **stands alone**: sign-in form *on the page*, then the console — people, keys, runs. No run button |

Each key buys **one level**, spent the moment somebody presses run, and stopping early
does not return it. The console shows "N levels left" per person and one **Give a
level** button.

`/admin` never redirects to `/login`. It used to, and a bounce read as "the admin
page is broken" rather than "you are not signed in". There is no link from the admin
console to the user pages either: the control room is not somewhere you go to get to
the dashboard.

The console deliberately **cannot run anything**. An earlier version gave the admin its
own logins and a run button, which put the owner's Speexx credentials in a page that is
also an all-users view — for no benefit, since the same admin can mint themselves a key
and redeem it on the dashboard like anyone else.

**Sign-up is open.** No approval queue, no gate on having an account. `users.open_signup()`
always returns true; the key, not approval, is what limits what an account can *do*.

**The first arrival owns the panel.** When no admin exists, the next person to sign up
becomes one, and is told so. That is not a convenience: approval-only sign-up plus a
randomly generated admin password means a fresh install has nobody who can approve
anybody, which is the dead end that started this rewrite. Every account after the first
is an ordinary one.

**The admin is not a user.** `/admin` has no accounts section and no run button — it
mints keys, approves nobody, watches every run. An admin who wants to run a level mints
themselves a key and redeems it on their own dashboard like anybody else.

---

## Commands

```bash
./run.sh                     # 127.0.0.1:8787, creates venv + browser on first run
./run.sh 9000                # different port
./run.sh --open-signup       # let anyone register without approval
./run.sh --reset-admin       # drop admin accounts; the next sign-up owns the panel
./run.sh --reset             # wipe everything: accounts, saved logins, runs, audit
./stop.sh                    # stops the panel and its runners only

# Verification — all of these, in this order
.venv/bin/python -m py_compile server.py db.py users.py accounts.py vault.py \
    auth.py jobs.py preflight.py runner.py
.venv/bin/python -m unittest discover -s tests -t .   # 140 tests (~50 s: scrypt is slow by design)
node solver_check.js                                  # in-page solver's pure helpers
cd web && npx @tailwindcss/cli -i tailwind.css -o ../static/app.css --minify
bash -n run.sh stop.sh tunnel.sh

diff solver.js ../Speexx-sonsole/optimized.js         # must be identical
```

`static/app.css` is **generated**. Edit `web/tailwind.css` and rebuild; never
hand-edit the built file. The panel needs no Node at runtime — only to rebuild
the CSS.

Screenshots of every page, desktop and mobile, with console errors surfaced:

```bash
.venv/bin/python shot_users.py http://127.0.0.1:8787 ../.review-users \
    admin@example.test 'the admin password'
```

---

## Files

| File | Role |
|---|---|
| `server.py` | FastAPI app, routes, session/CSRF/Origin/Host middleware, static mount |
| `users.py` | Signup, sign-in, lockout, sessions. Owns the `users` table |
| `keys.py` | Minting, redeeming and revoking the six digit run keys, and `has_access` gate |
| `accounts.py` | A person's saved Speexx logins, encrypted, and the per-address cooldown |
| `vault.py` | AES-256-GCM seal/open for those saved passwords, and the key lifecycle |
| `jobs.py` | Job state machine, process spawning, profile lease, stagger, supervision |
| `runner.py` | The bot. Browser, login gate, navigation, level/packet counting, stdout markers |
| `preflight.py` | Login verification and failure classification |
| `auth.py` | scrypt hashing, Host/Origin checks, rate-limit buckets |
| `db.py` | SQLite schema, migrations, and the partial unique indexes that carry invariants |
| `solver.js` | **Byte-identical copy** of `../Speexx-sonsole/optimized.js`. Do not edit |
| `solver_check.js` | Copy of that project's `check.js`; its regexes must stay identical |
| `web/tailwind.css` | Design tokens and every component class |
| `static/` | `index.html`, `login.html`, `signup.html`, `dashboard.html`, `admin.html` + one `.js` each, `common.js`, generated `app.css`, `fonts/` |
| `tests/test_panel.py` | Jobs, sessions, rate limits, leases, classifier, design rules |
| `tests/test_users.py` | Signup, lockout, encryption, isolation, and the schema migrations |
| `tests/test_keys.py` | Mint/redeem/revoke, the run gate, and the shape of the rate limits |
| `tests/test_design.py` | Asserts the style reference's explicit rules across every page |
| `tests/fake_runner.py` | Stub runner for integration testing — **never points at the portal** |
| `shot_users.py` | Playwright screenshotter for design review |
| `vercel-redirect/` | Vercel project that 307s `speexx.tykunanon.online` to the tunnel |

The **key** is not the credential and never touches the portal: it is the panel's own
permission to run. The credential is the encrypted Speexx login in
`speexx_accounts`. Keep those two apart — conflating them is how a key ends up being
treated like a login.

---

## Ownership: the one rule that matters

**Every query that returns somebody's data filters on `user_id`.** A missing
`WHERE user_id = ?` is a data leak that looks exactly like correct code.

```python
def get_owned(user_id: int, account_id: int):          # accounts.py
    return db.one("SELECT * FROM speexx_accounts WHERE id = ? AND user_id = ?",
                  (int(account_id), int(user_id)))
```

- It returns **`None`** for someone else's row rather than raising, so a caller
  that forgets to check still cannot read it.
- `accounts.credentials()` raises `AccountError` instead — it must not hand back
  a decrypted password it was not entitled to.
- `jobs.get_job(job_id, user_id)` and `jobs.stop_job(job_id, user_id)` take the
  owner and filter on it. `stop_job` returns `False` for a stranger's run.
- Admin reads go through separate functions — `jobs.jobs_all()`, `users.list_users()`
  — so an admin path can never be reached by dropping an argument on a user path.

`tests/test_users.py::OwnershipTests` tries each attack directly rather than
asserting the shape of the code.

### One Speexx login, one person

`speexx_accounts.email` is **globally** unique, not unique per user. Two people
saving the same portal address would fight over one Speexx session and invalidate
each other. `accounts.add()` refuses it, and `jobs_one_active_per_account` is the
run-time backstop for rows that get in another way.

---

## Encryption at rest

A saved Speexx password is sealed with **AES-256-GCM** (`vault.py`) and stored as
a `BLOB` in `speexx_accounts.secret`. The AAD is `user_id:email`, so a ciphertext
moved to a different row **fails to decrypt** — copy a row to another person and
it is not readable.

- The key is `data/vault.key`, written `0600`, or `SPEEXX_PANEL_VAULT_KEY`
  (32 bytes, hex). **There is no recovery: lose the key and every saved login is
  gone.** That is the intended trade for storing passwords at all.
- `vault.decrypt` raising `VaultError` is the signal for "wrong key, moved row or
  tampering" — it is never swallowed.
- A plaintext Speexx password must not appear in `panel.db`, in any log, in `ps`,
  or in a URL. `tests/` asserts the database case; the live check is
  `grep -r <password> data/`.

---

## The bug this project exists to prevent

The sibling orchestrator's login was **passive**: it filled the form, submitted, and
returned nothing. Its main loop then re-detected the login form on the next tick and
submitted again — roughly every eleven seconds, forever. A wrong password became an
unbounded stream of failed logins, **which is what gets the IP locked**. The speaking
project was better (returned a bool, backed off) but could not distinguish a wrong
password from a rate limit, because both merely left the URL on `/login`.

The rules that fix it, all in `preflight.py` and `runner.py:ensure_login`:

- **Success is verified structurally** — the URL has left `/login` *and* the login form is
  gone, held across two consecutive polls about a second apart so a transient pass through
  a page cannot pass as a login. Signed-in *words* (`my account`, `dashboard`, …) are read
  only to label the outcome, never to reject it: requiring one reported a real Thai login
  as a failure three times over with 60 s backoffs, because the page was in Thai. The
  stability requirement is what replaces the old textual check.
- **Failure is classified** into `bad_credentials`, `locked`, `rate_limited`, `captured`
  (`captcha`), `unknown`. Rule order matters: "too many failed attempts" also contains
  "too many attempts", so **locked is matched before rate_limited**. Reversed, a hard lock
  would classify as transient and get retried — the exact bug.
- **`bad_credentials` and `locked` are never retried.** Not once. Not with backoff. The
  runner exits `3` immediately.
- **A per-address cooldown** (15 min / 60 min) is written on those two reasons, and the
  panel refuses new jobs for that address until it expires. It is keyed by the Speexx
  **address, not by user**, because the portal's lockout is about the address.
- **Rules are multilingual** — the portal serves Thai, Chinese, Korean, Japanese, German
  and French. Adding a locale means adding to `preflight.RULES`.

**Never "improve" the login into a retry loop.** If a login fails for a reason you think
is transient, add it to the retry path explicitly and say why.

### Testing the bad-credential path

Point `SPEEXX_PANEL_RUNNER` at `tests/fake_runner.py` and let it decide from the
password (`wrong` → `bad_credentials`, `hang` → logs in then sleeps, otherwise a whole
12-packet level). **A live bad-password attempt against a real account must happen at most
once.** Repeats are what cause the lock.

---

## Run keys — one key, one level

`keys.py`. Only the admin mints. **A key buys exactly one level**, and it is spent
the moment a run starts.

```
unused  --redeem-->  redeemed  --run-->  spent
   ^                    |                    |
   +----- revoke -------+---- revoke --------+
```

`revoked` is the admin's kill switch and it works on a code nobody has claimed *and*
on a credit somebody is already holding. There is deliberately **no path back from
`spent`**: a refund would make a key a way to run without having been given one.

| Step | Endpoint | Notes |
|---|---|---|
| mint for a person | `POST /api/admin/users/{id}/key` | returns the code **once**; only a hash is kept |
| status | `GET /api/key` | levels in hand, codes waiting, levels spent |
| redeem | `POST /api/key/redeem` | turns a code into one level's credit |
| revoke | `DELETE /api/admin/keys/{id}` | voids an **unspent** key |

**Stopping a run does not give the key back.** That was an explicit decision (2026-10-07,
asked directly): charging only on completion would let somebody explore the portal for
free by pressing Stop repeatedly. So the credit is spent at spawn, in `start_job`:

```python
if not keys.has_access(user_id):
    raise NeedsKey()                  # -> 403, code "needs_key"
...
spent_key = keys.consume(user_id)     # before the browser opens
try:
    profile = reserve_slot(...)
except Exception:
    keys.refund(user_id, spent_key)   # nothing started, so nothing charged
    raise
```

The refund is the narrow exception: it undoes only the row this call just moved, in
this call, when the run could not start at all (no free slot, account busy). There is
no user-facing way to refund.

**Concurrency.** `consume` is a single `UPDATE ... AND status = 'redeemed'` with a
`rowcount` check, so of two tabs pressing Run with one credit exactly one wins and the
other gets `needs_key` rather than double-spending.

### Six digits is a million guesses

- **The code is never stored.** `code_hash` is a scrypt hash, so a copied `panel.db`
  yields nothing to try. A test greps the raw database file for the code it just minted.
- **Redeeming is rate limited** hardest of anything here: 5 per person per hour, 20 per
  address per hour (`key_redeem`, `key_redeem_ip`). A test asserts no other bucket is
  stricter.
- **A key is bound to one username at mint time.** Redeeming somebody else's code fails
  with the identical message, so it is not a transferable licence.
- **Uniqueness is checked by scanning unused keys with scrypt.** That is O(unused) per
  mint, which is bounded by `MAX_HELD_PER_USER`, and minting is a rare admin action. It
  is the obvious thing to "optimise" with a fast hash — **do not**: a plain SHA-256 of a
  six digit code is reversible in a second and would leak every code in the database.
- `MAX_HELD_PER_USER` (5) counts unclaimed codes **and** redeemed credits. Only `mint`
  checks it: redeeming converts a code to a credit and does not change the total, so a
  cap in `redeem` would refuse somebody the last key they were legitimately given.

Refusals are **uniform**: wrong code, right code minted for someone else, malformed
code, and over the cap all return `"That key is not right."` with code `bad_key`.
Differing by input shape would turn the endpoint into an oracle.

**`keys` has two generations in the wild.** The original panel minted anonymous keys
against a browser fingerprint (`key_hash`, `fingerprint`, `used_by_email`). A `keys`
table without `user_id` is **dropped**, not translated, before SCHEMA runs.

## Usernames, and who owns the panel

- `users.username` is the **only** identifier, unique case-insensitively. `admin`,
  `root`, `administrator`, `support` and `speexx` are reserved so a stranger cannot sign
  up as "admin". The `users` table has no `email` column; `_migrate_users()` rebuilds an
  older one to drop it.
- **The reserved list has one exemption:** the environment bootstrap passes
  `allow_reserved=True`, because otherwise it could never create the very account it
  exists to create.
- The admin list and the console show **usernames, not addresses**. `jobs.jobs_all()`
  labels a run with the owner's `username`; asking it for `email` 500s the admin page
  whenever a run existed.
- `MIN_PASSWORD` is **8**, with a short blocklist of the passwords that would pass eight
  characters and nothing else.

`users.no_admin_exists()` — not "no users exist" — decides who owns the panel. That is
load-bearing: after `./run.sh --reset-admin` there are still pending sign-ups on file, so
a check on the user count would leave the panel unadministrable again.

## Lockout of the panel's own accounts

`users.py` allows `MAX_FAILS` (5) wrong passwords before an account is locked, then
backs off 15 min doubling to a 6 h cap. The lock is armed **only on the fifth
failure** — arming it on the first typo meant one stray keystroke cost a legitimate
user fifteen minutes.

Sign-in failures are deliberately uniform: a wrong password and an unknown name raise
the same message and the same `code`, and `authenticate` always performs the scrypt work
so response time does not reveal whether the account exists. Disabling
someone destroys their sessions immediately, so the role change takes effect at once.

---

## Credentials: never in argv

`runner.py` reads `email\npassword` from **stdin** (`--credentials-stdin`). `argv` is
world-readable through `ps`; a temp file has to be cleaned up. The password is held in
the panel's memory only for the lifetime of the spawn and used a second time to scrub
whatever is persisted. It is decrypted only at the moment of the spawn.

Two scrubbers exist on purpose: `runner.log()` for the child's own output and
`jobs.scrub()` for what the panel writes. Both skip secrets shorter than four characters,
because a two-character "secret" would otherwise redact ordinary log text.

The integration test asserts the password appears in neither the database nor `ps`.

---

## No saved browser profiles

`runner.py` uses `p.chromium.launch()` + `browser.new_context()` — nothing is written to
disk and no cookie or `localStorage` survives the process. This is deliberate: the
sibling project's docs record a real incident where a shared profile silently ran as
**the wrong account**. An ephemeral context makes that class of bug impossible rather
than merely unlikely.

Cost: a fresh login every run (~15–25 s). Worth it. Nothing to clean up, no ~200 MB
profile directories, no profile locks after a hard kill.

Note the option split — `headless` and `args` belong to `launch()`, everything else to
`new_context()`. Mixing them is the bug that makes this hard to express.

---

## Concurrency: what the database enforces

Speexx allows **one account per device** and fingerprints a device by IP plus browser
fingerprint. Every session on this Mac shares one IP, so concurrent runs must differ.
There are six device profiles (`runner.DEVICE_PROFILES`, index modulo 6, profile 0 =
"change nothing" and leased last).

`multi_run.sh` in both sibling projects is the model this borrows from, and the numbers
match it on purpose: **`STAGGER=15`** seconds between logins, **`CYCLE_CAP=1800`**
seconds per run. Login on time A can never look like an attack to the portal.

Two **partial unique indexes** do the rest, because application-level checks race:

```sql
CREATE UNIQUE INDEX jobs_one_active_per_account
    ON jobs(email) WHERE state IN ('queued','starting','running');
CREATE UNIQUE INDEX jobs_one_active_per_profile
    ON jobs(profile) WHERE state IN ('queued','starting','running') AND profile IS NOT NULL;
```

`jobs.reserve_slot()` relies on the second one and retries the next profile on
`IntegrityError`; SQLite names the column rather than the index in that message, so it
matches `"jobs.email"` and raises `AccountBusy` rather than trying every profile.
**Do not replace this with a read-then-write** — `lease_profile()` is read-only and
exists for tests and introspection; using it in the request path reintroduces the race.

`jobs.MAX_PER_USER` (2) additionally caps how many runs one person may have at once, so a
single account cannot occupy the whole machine.

On boot, `jobs.reap_orphans()` SIGTERMs then SIGKILLs any recorded process group still
marked running. Without it a half-dead row holds a profile forever and the pool starves
after six restarts.

**Never `pkill -f` from this project.** `stop.sh` matches `uvicorn server:app` and
`panel/runner.py` only. The sibling projects' `stop.sh` scripts use patterns that would
kill this project's processes, and a broad pattern like `python` would kill all three.

---

## Schema order matters

`connect()` runs, in order:

1. `_drop_legacy_sessions()` — the pre-multi-user `sessions` table had no owner, so
   those cookies would authenticate a stranger into the new panel. Dropped.
2. `SCHEMA` — `CREATE TABLE IF NOT EXISTS` only.
3. `_migrate()` — rebuilds `users` without its `email` column, adds columns an older
   `panel.db` lacks, and repairs dangling foreign keys.
4. `INDEXES`.

**`ALTER TABLE ... RENAME TO` rewrites the `REFERENCES` clauses of every other table.**
The `users` rebuild renames it to a temporary name, so `sessions.user_id` and
`speexx_accounts.user_id` silently repointed at that temporary table — and dropping it
left them referencing nothing, which made *every* `DELETE FROM users` fail with "no such
table". `_migrate_users` therefore wraps the rename in `PRAGMA legacy_alter_table = ON`,
and `_repair_stale_user_references()` fixes any database already damaged that way.
`tests/test_users.py::MigrationTests` covers both. **Do not turn that pragma off.**

**Indexes come last on purpose.** Several name columns (`jobs.user_id`,
`jobs.account_id`) that an older database does not have yet, and an index created
before its column exists aborts the whole bootstrap — which is exactly how the panel
came to refuse to start on its own existing database.

`jobs.LOG_DIR` is derived from `db.DATA_DIR`, not from the file's location, so
`SPEEXX_PANEL_DATA_DIR` relocates the database **and** its logs together.

---

## stdout contract between `runner.py` and `jobs.py`

The panel parses these prefixes and nothing else:

`GET /api/jobs/current` returns **200 with `{"job": null}`** when there has never been a
job — not a 404. The dashboard polls it every 2 s while a job is live and backs off when
idle, so an idle panel does not generate a request every two seconds for hours.

```
LOGIN_OK verified=<8 hex>          # may be preceded by "login settled (...)"
LOGIN_FAILED reason=<bad_credentials|locked|rate_limited|captcha|unknown> [phase=midrun]
PACKET_DONE n=<int> packet=<id> level_packets=<int>
LEVEL_DONE  n=<int> packets=<int>
JOB_DONE    levels=<int> packets=<int> [reason=time_cap|no_work]
```

Exit codes: `0` done, `3` login refused as fatal, `4` other failure (including hitting
`--max-seconds`). Keep the markers on one line — `jobs._MARKERS` matches by prefix.

### Levels and packets

These were once conflated and the distinction matters. A **packet** is
`standard-packet/<id>` and holds many exercises. A **level** is the whole article level
and holds *many packets* — an account can be around seventy in one. Counting packets as
"lessons" stopped a job about a fifth of the way through a level, which is wrong.

- A packet is finished when the app moves to a different packet after having shown
  exercises in the current one. Emits `PACKET_DONE`.
- A **level** is finished when the app returns to the article root (`url_kind()` is
  `None`) **and at least one packet was already finished**. Both sibling projects agree on
  that signal — the speaking one calls it "the bare `/articles/<id>` page" and re-navigates
  from it. The "already finished one packet" guard is what stops the root landing *before*
  the level starts from being read as its end.
- The cap is `--max-levels`, default **1**. On reaching it the runner emits `JOB_DONE`,
  exits `0`, and the UI shows LEVEL DONE.
- `--max-seconds` (default 3 h) is a wall-clock backstop. Hitting it reports
  `reason=time_cap` and exits `4`, deliberately **not** `done` — a stalled run must not be
  reported as a completed level.
- A mid-run re-login clears `in_level`, so the bounce back through the article root that
  follows a session loss cannot be counted as a finished level. The packet tally survives.
- **Nothing to do is a real outcome.** If `open_exercises()` finds no packet link
  `EMPTY_POLL_LIMIT` times in a row — what an account at the end of its level looks like —
  the job stops with `reason=no_work` and exit `4`. Without that check the first live run
  re-polled the same empty page for eight minutes until the time cap. Verified on a real
  account: `login settled` → `LOGIN_OK` → `no packet available (3/3)` →
  `JOB_DONE levels=0 packets=0 reason=no_work`.

The **full level loop is still unverified on a real account**, because the only account
available had no packets left (`no_work`). Everything up to `LOGIN_OK` is proven live.

If level detection ever proves wrong on a real account, the fix belongs in `finish_level()`
and nowhere else.

---

## Exposure: localhost is not automatically safe

`run.sh` binds `127.0.0.1` and refuses `0.0.0.0` without `--allow-remote`. But a
browser-served loopback service is still reachable from **any page the user has open**,
and DNS rebinding lets a hostile page aim its own hostname at loopback. So all of this
ships regardless:

- **Host allowlist** on every request (`auth.host_allowed`, compared on hostname only).
- **CSRF token plus Origin check** on every state-changing request, **including
  `/api/auth/logout`**. `SameSite=Strict` alone is not sufficient, and logout is a login
  action: unchecked, any page could sign somebody out mid-run.
- **Opaque sessions** — 256-bit id in an `HttpOnly` cookie, stored hashed server-side, so
  logout is a real `DELETE` and there is no signing key to leak.
- **CSP with no `unsafe-inline`**, which is why there is no inline script or style
  anywhere and the fonts are self-hosted. `tests/test_design.py` fails if any page
  reintroduces one.
- **Uniform failures** — every unusable credential is the same `401`; wrong, expired and
  nonexistent are indistinguishable.

Rate-limit buckets are per **person where it can be** and per IP as a floor:
`signup`, `login` (IP), `login_user` (address), `account_add` (person),
`account_add_ip` (IP), `job_start` (person), `job_start_ip` (IP).

### Getting to it from outside the machine

**ngrok, via `tunnel.sh`, `./run.sh --with-tunnel`, or the `speexx.tykunanon.online`
redirect below.** An authtoken already exists in
ngrok's own config (`~/Library/Application Support/ngrok/ngrok.yml`), so the assigned
`*.ngrok-free.dev` domain is **stable across restarts** and needs no DNS record at all.

Note that the redirect does **not** change which `Host` the panel sees: the browser is
sent to the ngrok domain, so `speexx.tykunanon.online` never reaches the panel at all.

`SPEEXX_PANEL_ALLOWED_HOSTS` must include `*.ngrok-free.dev` — the allowlist supports
`*.suffix` entries, and the **Origin** check uses the same matcher, or every `POST` would
be refused `403` after the page itself loaded.

**ngrok's free tier shows a warning interstitial to browsers** — `ERR_NGROK_6024` — with
a **Visit Site** button. Clicking it sets a cookie, so it appears once per browser.

- **`curl` and API tests sail straight through**, because the interstitial is keyed on the
  browser `User-Agent`. A green API test set proves nothing about what a person sees.
- `shot_users.py` against a tunnel URL gets the interstitial, not your page — click
  through first or the assertions fail mysteriously.
- It appears on every new browser and private window, including on a phone. This is the
  main practical reason to prefer Tailscale Funnel.

Two ngrok processes share port `127.0.0.1:4040` (its web UI/API), so **only one can run at
a time** — the existing `com.blue.immich-ngrok` agent and this one will fight over it.

### `speexx.tykunanon.online` exists — it is a Vercel redirect

Set up 2026-10-07. `vercel-redirect/` is a Vercel project that deploys one
`vercel.json` and nothing else: it answers `speexx.tykunanon.online` with a **307** to
the current ngrok tunnel.

```
speexx.tykunanon.online  ──307 (Vercel)──▶  <ngrok url>  ──▶  panel
```

- The `speexx` DNS record already pointed at Vercel and returned
  `404 DEPLOYMENT_NOT_FOUND` — the domain was never attached to a project. Adding it to
  `speexx-redirect` fixed that.
- **`/:path*` does not match the bare root.** The first deploy 404'd on `/` while
  redirecting `/admin` correctly, so there are two rules: one for `/` and one for
  everything else. Both are needed.
- **`permanent: false` is deliberate.** A 308 is cached hard by browsers; if the ngrok
  URL changes, everyone holding a cached redirect is stranded. Verified: a fresh browser
  context goes `speexx.tykunanon.online` → `307` → `ERR_NGROK_6024` → "Visit Site" →
  the panel.

**What this does not fix.** It is a redirect, not a proxy:

- The **ngrok warning page still appears** on first visit per browser. Clicking
  "Visit Site" sets a cookie, so it is once per browser, not per request.
- The **URL bar shows the ngrok domain**, not `speexx.tykunanon.online`.

To get rid of either, the options are **Tailscale Funnel** (real URL, no interstitial,
free) or ngrok's paid custom domain ($20/mo — verified as Pay-as-you-go only, not Free
or Hobbyist). A URL-bar-preserving *proxy* is not available on Vercel Hobby.

### Why not a Cloudflare Tunnel

`tykunanon.online`'s nameservers are `ns1/ns2.vercel-dns.com`, so Cloudflare is not
authoritative and cannot be given a route for a subdomain. Measured 2026-10-05:

- A CNAME `speexx` → `<uuid>.cfargotunnel.com` in Vercel resolves, but the target has
  **no public address**: `dig` at 1.1.1.1, 8.8.8.8 and 9.9.9.9 all return only
  `fd10:aec2:5dae::`, a private ULA, and `curl` hangs on `[fd10::]:443`.
- Cloudflare's docs describe pointing your current DNS provider at that hostname, but
  the route only exists inside Cloudflare's own DNS. Publishing a subdomain *zone* to
  Cloudflare is Enterprise-only; CNAME/partial setup is Business-and-up.

So a Cloudflare Tunnel is out, and the Vercel redirect is what a custom domain can
actually be here — as a redirect, never as a tunnel.

The inert `speexx-panel` Cloudflare tunnel is still registered and would become usable
only if the zone's nameservers moved to Cloudflare; otherwise
`cloudflared tunnel delete speexx-panel`.

Behind a tunnel two more things are load-bearing and easy to miss:

- **Rate limiting must read `CF-Connecting-IP`, and only when the peer is loopback.**
  Otherwise every visitor shares one `127.0.0.1` bucket — either the limiter is useless
  or five typos lock the owner out of their own panel.
- **`--proxy-headers`** so `Secure` cookies and the scheme check work. `run.sh` passes it
  with `--forwarded-allow-ips '127.0.0.1'`.

---

## Traps that already cost time

1. **Tailwind v4 spacing.** The style reference's spacing scale is in **px**
   (`--spacing-16: 16px`). Declaring those inside `@theme` redefines Tailwind's entire
   spacing scale: `h-16` becomes 16px instead of 64px and `mt-12` becomes 12px. They live
   in a plain `:root` block in `web/tailwind.css` for exactly this reason. Do not move
   them into `@theme`.
2. **`@source "../static";` is required.** v4 auto-detects sources relative to the CSS
   file, which lives in `web/`. Without it every utility is purged and the page renders
   unstyled while still looking plausible.
3. **An absolutely-positioned decoration needs a `relative` ancestor.** The gray band's
   ribbon had none, so it positioned against the document and painted across the nav.
4. **Sticker `<svg>` wrappers need `viewBox="0 0 64 64"`.** They `<use>` a `<symbol>`,
   which scales to the use element's viewport; without a viewBox the size is arbitrary.
5. **`overflow-x: clip`, not `hidden`.** On `body`, `hidden` propagates to the viewport
   and leaves body itself visible, so the scrollbar disappears while the overflow remains
   — and full-page screenshots then come out wider than the page.
6. **scrypt `n=2**14`, not `2**15`.** `2**15` needs ~32 MiB and fails outright with
   "memory limit exceeded" unless `maxmem` is raised.
7. **`Origin` is compared by hostname only.** An Origin carries a port; matching the full
   origin against a port-less allowlist rejects every request on a non-default port.
8. **Playwright's revision must match `~/Library/Caches/ms-playwright`.** A mismatch makes
   every launch die with `Executable doesn't exist at ...`. Fix:
   `.venv/bin/python -m playwright install chromium`. **This currently also affects
   `../Speexx-sonsole`**: its cache holds revision 1243 while Playwright 1.62.0 wants
   1234, so that bot will not launch until it installs its own build.
9. **A `*/` inside a CSS comment closes the comment.** `p-*/m-*` in prose broke the build
   with a baffling "Unterminated string".
10. **`*.suffix` allowlist entries must apply to the Origin check too.** `host_allowed`
    learned about wildcards and `origin_allowed` did not, so a tunnel host loaded the page
    (`GET /admin` → 200) and then every `POST /api/admin/login` was refused **403** before
    the password was ever compared. Both checks go through `auth.host_matches()`.
11. **A display face is for digits.** `display-sm` on a *word* like "By approval" wrapped
    out of its tile, so `statTile` picks the size by content.
12. **A hidden element does not move its siblings.** The admin gate was inserted above
    the marquee in `admin.html`, so with the console hidden the marquee rendered
    *below* the sign-in form. Markup order is the layout; `hidden` only removes a node
    from flow, it does not reorder anything.
13. **Tailwind will not generate a class you have not rebuilt for.** A new utility in a
    template does nothing until `npx @tailwindcss/cli` runs again — it looked like the
    class was wrong rather than absent from the build. Rebuild, then measure with
    Playwright instead of guessing.
14. **A `422` is a schema leak.** Field-level `min_length` on a request model answers
    `422` carrying the field layout, which differs by input shape and tells a caller what
    the server expects. The signup models are deliberately loose and
    `users.check_username` is the single authority; a `RequestValidationError` handler
    turns anything malformed into one plain `400`.
15. **The browser's cached copy of a page outlives the file it named.** A `404` for
    `/app.js` appeared long after that script was deleted, because the tab held the old
    markup. Hand-written HTML is served `Cache-Control: no-store` so a stale page cannot
    outlive a change.
16. **A full-page screenshot lies about `position: sticky`.** The nav is sticky, and
    Playwright's `full_page` shot paints it at the scroll offset the page happened to be
    at — it appeared halfway down a correct page. Measure with
    `getBoundingClientRect()` before believing a screenshot; `shot_users.py` now
    scrolls to the top first.
17. **`SPEEXX_PANEL_DB` does not exist.** The variable is `SPEEXX_PANEL_DATA_DIR` (a
    *directory*). Setting the wrong name is silently ignored, so the module falls back to
    `./data` — the **live** database. This actually happened: a test module set
    `SPEEXX_PANEL_DB`, so its fixtures signed users up in the live database and a
    `DELETE FROM jobs` in a `setUp` wiped the real job history. `tests/test_users.py`
    now asserts the database lives in a temporary directory, so it fails loudly instead of
    quietly writing to `data/`. **Before adding a test module, copy the env-var block from
    an existing one.**

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SPEEXX_PANEL_DATA_DIR` | `./data` | database and logs, created `0700` |
| `SPEEXX_PANEL_VAULT_KEY` | `data/vault.key` | 32-byte hex key for saved passwords |
| `SPEEXX_PANEL_ADMIN_USERNAME` | `admin` | admin name when bootstrapping from the environment |
| `SPEEXX_PANEL_ADMIN_PASSWORD` | unset | set it to create the admin; unset means no admin is created |
| `keys.MAX_UNUSED_PER_USER` | `5` | unused codes held for one person at a time (a constant, not env) |
| `SPEEXX_PANEL_ALLOWED_HOSTS` | `127.0.0.1, localhost, ::1` | comma-separated Host allowlist |
| `SPEEXX_PANEL_PROFILE_POOL` | `1,2,3,4,5,0` | lease order; profile 0 last |
| `SPEEXX_PANEL_MAX_PER_USER` | `2` | concurrent runs per person |
| `SPEEXX_PANEL_STAGGER` | `15` | seconds between logins (matches `multi_run.sh`) |
| `SPEEXX_PANEL_CYCLE_CAP` | `1800` | seconds per run (matches `multi_run.sh`) |
| `SPEEXX_PANEL_PYTHON` | `./.venv/bin/python` | interpreter for the child |
| `SPEEXX_PANEL_RUNNER` | `./runner.py` | lets tests substitute a stub |
| `PORT` (in `tunnel.sh`) | `8787` | the port ngrok forwards to |
| `SPEEXX_PANEL_FAST` | unset | `1` adds `--fast` (tighter poll, **unproven**) |

---

## The first admin

Two ways to have one, and no third.

**1. Sign up.** On a panel with no admin, the first account created becomes one. This
is the normal path and needs no configuration.

**2. The environment**, when you would rather not race for it:

```bash
SPEEXX_PANEL_ADMIN_USERNAME=admin \
SPEEXX_PANEL_ADMIN_EMAIL=admin@localhost \
SPEEXX_PANEL_ADMIN_PASSWORD='choose one' ./run.sh
```

`users.bootstrap_admin()` runs at startup and does nothing unless
`SPEEXX_PANEL_ADMIN_PASSWORD` is set. It used to *generate* an admin and print the
password in the banner; that was the mechanism that stranded this panel, because a
password in a log file is not a password anyone remembers. An admin already exists also
makes it a no-op, so restarting never resets anything.

Losing it:

```bash
./run.sh --reset-admin     # removes admin rows and sessions; the next sign-up owns it
```

That leaves any other accounts in place. The next person to sign up becomes the admin,
because `no_admin_exists()` is now true — which is the point.

---

## House rules

- **Credentials never enter documentation, code, fixtures, commit messages or filenames.**
- **Docs must match code.** This file, `PRODUCT.md` and `DESIGN.md` were verified against
  the source on 2026-10-06. If you change a flag, a token, a marker or a constant, update
  them in the same pass — there is no git history here, so the code is the only backup.
- **Prefer the smallest change that fixes a real observed failure.** The timings in
  `runner.py` are tuned by feel. `--fast` exists but is **unproven**; measure packets per
  hour before and after before enabling it.
- **Prefer native `element.click()`** over synthetic events for app buttons — the app's
  own handlers ignore untrusted events.
- **`solver.js` stays byte-identical** to the sibling project's `optimized.js`. Its
  no-bounce variant is derived by exact string replacement at import time, with a
  `RuntimeError` guard so a mismatch fails loudly. Verify with
  `diff solver.js ../Speexx-sonsole/optimized.js`.