# AGENTS.md — panel-lite (cloud, single operator)

The lite panel is the full panel (`panel-cloud/`) with the multi-stranger
machinery removed: **no users, no signup, no keys, no sessions, no CSRF, no
run-key gate.** One operator: add a Speexx account, press run, watch it work
one whole level, stop it if needed. Same visual theme (the same compiled
`app.css`, same tile/pill/logbox classes) and the same runner stack.

## Files

| File | Role |
|---|---|
| `server_lite.py` | FastAPI app: page + JSON API + host allowlist + token gate |
| `jobctl.py` | sqlite state, runner spawn/stop, stdout-marker parsing, profile lease, stagger, cooldowns |
| `runner.py`, `preflight.py` | byte-copies of panel-cloud's (the cloud/headless variants) |
| `solver.js`, `solver_check.js` | byte-identical to `Speexx-sonsole-cloud/optimized.js` — verify with `diff` after any copy |
| `static/` | copied `app.css`/`fonts.css`/`fonts`/favicon from panel-cloud + new `index.html`/`lite.js` |
| `tunnel_lite.py` | pyngrok public URL, writes `lite_data/tunnel-url.txt` |
| `run.sh` / `stop.sh` | start/stop; stop.sh TERMs uvicorn first so the shutdown hook can stop runs gracefully |
| `lite_data/` | sqlite db, vault key, job logs, tunnel URL (gitignored, credentials-sealed) |

## Access model (this replaces the login system)

- **Codespaces, private forwarded port**: visible only to your GitHub session.
  No token. `run.sh` does not require one.
- **Public tunnel (ngrok)**: `run.sh` generates a token (`secrets.token_urlsafe`)
  and prints it **once** when `PANEL_LITE_TOKEN` is unset and `NGROK_AUTHTOKEN`
  is set; you can also set `PANEL_LITE_TOKEN` yourself. The browser asks for it
  once, stores it in `localStorage`, and sends it as `X-Token` on every request.
- **No token, no tunnel** (bare `127.0.0.1`): open API — only acceptable behind
  a private port; `run.sh` warns loudly.
- Host allowlist: loopback always; `*.ngrok-free.dev/app` when tunneling;
  `*.app.github.dev` when `CODESPACES` is set; plus
  `SPEEXX_LITE_ALLOWED_HOSTS` (comma list). Unknown Host → 400.

## API

- `GET /api/state` → `{accounts, jobs, caps, access}` (401 `{"error":"token"}` when locked)
- `POST /api/accounts` `{email, password}` → `{id}`; duplicate → 400
- `DELETE /api/accounts/{id}` → 409 while a run is active
- `POST /api/jobs/start` `{account_id}` → `{id}`; 429 for stagger/cooldown/profile-busy
- `POST /api/jobs/{id}/stop`, `GET /api/jobs/{id}/log` (tail ≤200 lines)

## What jobctl keeps from the full panel, and why

- **Passwords sealed with AES-256-GCM** into `lite_data/lite.db` (key in
  `lite_data/vault.key`, 0600; or `SPEEXX_PANEL_VAULT_KEY` hex env). AAD binds
  `lite:<email>`. Never in argv — credentials go to the child on **stdin**.
- **bad_credentials / locked never retry**: a per-address cooldown
  (15 min / 60 min) is set from `LOGIN_FAILED reason=...`; starting during
  cooldown is a 429. Retrying bad passwords is what gets an IP locked.
- **One active run per address** (the portal allows one session per account).
- **Device profiles 1..5 then 0** leased per active run
  (`SPEEXX_LITE_PROFILE_POOL`); 15 s login stagger (`SPEEXX_LITE_STAGGER`).
- **Marker contract** (`LOGIN_OK`, `LOGIN_FAILED`, `PACKET_DONE`, `LEVEL_DONE`,
  `JOB_DONE`) parsed by a watcher thread from the job's log file.
- **Process-group lifecycle**: each run is spawned `start_new_session=True`
  (runner + its Chromium share one group). `stop_job`/`shutdown`/`boot` mark the
  row `stopped` **before** TERM so the watcher cannot finalize a killed child as
  `failed exit -15`; shutdown joins the watcher before uvicorn exits so the
  exit code is recorded.
- Cycle cap: one level, `--max-seconds 1800` (`SPEEXX_LITE_CYCLE_CAP`).

## Env vars

`PANEL_LITE_TOKEN`, `NGROK_AUTHTOKEN`, `SPEEXX_LITE_PYTHON`,
`SPEEXX_LITE_ALLOWED_HOSTS`, `SPEEXX_LITE_DATA_DIR`, `SPEEXX_LITE_STAGGER`,
`SPEEXX_LITE_CYCLE_CAP`, `SPEEXX_LITE_PROFILE_POOL`, `SPEEXX_LITE_RUNNER`
(test override for the runner path), `LITE_FORCE_BROWSERS=1` (stop.sh also -9s
playwright Chromium — do this only when no other speexx bot is running).

## Python selection (`run.sh`)

`SPEEXX_LITE_PYTHON` → sibling `panel-cloud/.venv` if it has
fastapi/uvicorn/playwright/cryptography → own `.venv` (built with the same
get-pip fallback as panel-cloud's `cloud_setup.sh`, because Colab's Debian
Python has broken `ensurepip`). Prefer the panel-cloud venv: it already has the
matching Chromium build installed.

## Verification

```bash
.venv/bin/python -m py_compile server_lite.py jobctl.py runner.py preflight.py
node solver_check.js
bash -n run.sh stop.sh
diff solver.js ../Speexx-sonsole-cloud/optimized.js   # must be empty
# smoke: start server, add account, run tests/fake_runner.py via
# SPEEXX_LITE_RUNNER=../panel/tests/fake_runner.py — LOGIN_OK/PACKET_DONE/
# LEVEL_DONE/JOB_DONE, bad_credentials cooldown, hang+stop, TERM-shutdown
```

The lite panel is **not** covered by the full panel's test suite (its pure
logic — host allowlist, marker parsing — lives inline). The fake-runner smoke
above is the regression check; a real browser run is the only proof the runner
itself still works, same as the full panel.

## House rules (same as the parent workspace)

- Credentials never in code, logs, commit messages, or this doc.
- `solver.js` stays byte-identical to `Speexx-sonsole-cloud/optimized.js`.
- Do not modify `panel-cloud/` from here; this folder only *copies* from it.
- Broad `pkill python`-style patterns are forbidden; stop.sh patterns are
  scoped to `server_lite:app`, `panel-lite/runner.py`, `tunnel_lite.py`.
