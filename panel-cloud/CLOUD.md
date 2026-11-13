# CLOUD.md — Speexx Panel on GitHub Codespaces / Google Colab / Kaggle

This folder is the cloud deployment copy of the local panel project. The panel logic is
**byte-identical** (`server.py`, `users.py`, `keys.py`, `accounts.py`, `vault.py`,
`jobs.py`, `preflight.py`, `auth.py`, `db.py`, `runner.py` navigation, solver.js,
tests, static frontend — all unchanged). Only the environment layer differs.

## What is different here (and why)

| Change | Files | Reason |
|---|---|---|
| Headless runner browser in any no-display environment (Colab, Kaggle, Codespaces, devcontainers) | `runner.py` | detection mirrors the sonsole bot; `SPEEXX_HEADLESS=0` overrides after starting `xvfb`. |
| `--no-sandbox --disable-dev-shm-usage` when root | `runner.py` | Chromium refuses to start as root with the sandbox on; Colab is root, Codespaces runs as a normal user. |
| Linux system libraries for Chromium | `run.sh`, `cloud_setup.sh` | Ubuntu images lack the NSS/ATK/GBM stack. |
| Cloud tunnel path in `tunnel.sh` + `tunnel_ngrok.py` | tunnel | no ngrok binary/config on Colab/Kaggle → pyngrok driven by `NGROK_AUTHTOKEN`. |
| Allowlist covers `*.ngrok-free.app` too, and `*.app.github.dev` when `CODESPACES` is set | `run.sh` | free-tier ngrok assigns either suffix; Codespaces' built-in port forward serves that host. |

## Codespaces

The devcontainer builds both venvs and Chromium automatically. Then:

```bash
cd panel-cloud
./run.sh 8787
```

Open the Ports panel → 8787 → the forwarded URL (set the port visibility to
**Public** to reach it from a phone). No ngrok needed; the allowlist accepts the
`*.app.github.dev` host automatically. An ngrok tunnel still works if you prefer
an independent URL: set `NGROK_AUTHTOKEN` and `./run.sh 8787 --with-tunnel`.

First person to sign up owns the panel, or bootstrap one before starting:

```bash
SPEEXX_PANEL_ADMIN_USERNAME=admin SPEEXX_PANEL_ADMIN_PASSWORD='one you choose' ./run.sh 8787
```

Codespace disk dies with the codespace: set `SPEEXX_PANEL_VAULT_KEY` (64 hex
chars) to a kept value, or saved logins vanish on rebuild.

## Reaching the panel from outside (Colab / Kaggle)


```python
# one cell
!bash cloud_setup.sh          # ~2-4 min on a fresh session
```

## Reaching the panel from outside

```bash
export NGROK_AUTHTOKEN='your ngrok authtoken'   # ngrok dashboard, free
./run.sh --with-tunnel                          # panel + public URL
```

- The public URL is printed and written to `data/tunnel-url.txt`.
- Free-tier ngrok shows a **"Visit Site" warning page** once per browser — that is
  the interstitial, not a broken panel.
- Colab alternative: `from google.colab import output; output.serve_kernel_port_as_window(8787)`
  also works for one user, but it only exposes the window to the Colab UI, so the
  ngrok tunnel is the supported path.
- If `speexx.tykunanon.online` (the Vercel 307 redirect) should point at this
  tunnel, update the redirect target in `vercel-redirect/vercel.json` and
  redeploy — the URL changes per session unless your ngrok account has a static
  domain configured.

## Persistence — read this before adding accounts

- **Kaggle**: set `SPEEXX_PANEL_DATA_DIR=/kaggle/working/panel-data` so the
  database, vault key and job logs survive the session (and land in the notebook
  output). Without it, data lives under the project dir, which on Kaggle datasets
  is read-only — the panel then fails to start.
- **Colab**: `/content` is wiped when the runtime dies. Every saved Speexx login
  is sealed with the vault key in `data/vault.key`; **lose the key and every saved
  login is gone**. To keep accounts across sessions either
  - set `SPEEXX_PANEL_VAULT_KEY=<64 hex chars>` to a value you keep, or
  - mount Google Drive and set `SPEEXX_PANEL_DATA_DIR=/content/drive/MyDrive/panel-data`.

## The first admin

Either let the first sign-up own the panel (same as local), or set, before
starting:

```bash
export SPEEXX_PANEL_ADMIN_USERNAME=admin
export SPEEXX_PANEL_ADMIN_PASSWORD='one you choose'
```

Secrets names used by `panel_cloud.ipynb`: `NGROK_AUTHTOKEN`,
`SPEEXX_PANEL_ADMIN_PASSWORD` (Kaggle Secrets / Colab userdata).

## Cloud caveats

- **Datacenter IP.** The runner's browser logs into Speexx from a cloud IP. Speexx
  locks by address on repeated *bad* logins — the panel already refuses retries
  for `bad_credentials`/`locked` (`preflight.py`); do not bypass that here.
- **One Speexx account, one session.** A run started here and one started in the
  local bot fight over the account. Same rule as the local multi-session.
- Device profiles (1..N) still matter: parallel jobs on this box share one public
  IP, which is exactly what the profile pool differentiates.
- Tests work unchanged: `.venv/bin/python -m unittest discover -s tests -t .`
  (they always use a temp `SPEEXX_PANEL_DATA_DIR`).
