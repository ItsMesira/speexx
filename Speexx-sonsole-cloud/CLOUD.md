# CLOUD.md — Speexx AutoCycle on GitHub Codespaces / Google Colab / Kaggle

This folder is the cloud deployment copy of the local Speexx-sonsole project. The bot logic is
**byte-identical** (`speexx_autocycle.py` solver path, `optimized.js`, `check.js`,
`multi_run.sh` supervision, device profiles, delays — all unchanged). Only the
environment layer differs.

## What is different here (and why)

| Change | Files | Reason |
|---|---|---|
| Headless browser in any no-display environment (Colab, Kaggle, Codespaces, devcontainers) | `speexx_autocycle.py`, `check_level.py` | no display there. Detection: Colab/Kaggle/Codespaces env markers, or Linux without `$DISPLAY`. `SPEEXX_HEADLESS=0` overrides (only after starting `xvfb`). |
| `--no-sandbox --disable-dev-shm-usage` when running as root | same | Chromium refuses to start as root with the sandbox on; Colab is root, Codespaces runs as a normal user. |
| Linux system libraries for Chromium | `run.sh`, `cloud_setup.sh` | Ubuntu images lack the NSS/ATK/GBM stack Chromium links against. |
| `chrome-linux/` kill pattern | `stop.sh`, `start.sh` | the browser binary path on Linux differs from macOS `chrome-mac-arm64`. |
| Placeholder/no `Accounts.md` in the repo | — | credentials must never ride inside a notebook or repo; supply them at runtime (`.gitignore` covers `Accounts.md`/`alluser.md`). |

Everything else — login selectors, packet discovery, the no-bounce transform guard,
stagger/cycle constants, per-account profile dirs — is intentionally **not** changed.
Timings are tuned by feel; changing them changes throughput and failure modes.

## Codespaces

The repo's `.devcontainer/devcontainer.json` runs `cloud_setup.sh` for both folders
automatically on first build. Then:

```bash
cd Speexx-sonsole-cloud
nano Accounts.md                 # paste credentials (gitignored), or from a secret:
printf '%s\n' "$SPEEXX_ACCOUNTS" > Accounts.md
./start.sh 12600                 # all accounts, 3.5 h window, in background
tail -f ~/speexx_autocycle_multi/session1_*.log.console
./stop.sh
```

All parallel accounts on one codespace share one public IP — that is exactly why
the device profiles (1..N) exist and are assigned by `multi_run.sh`.

## Setup (Colab / Kaggle)


```python
# one cell
!bash cloud_setup.sh          # ~2-4 min on a fresh session
```

## Credentials

Two supported ways, in order of preference:

1. **Secrets store** (recommended — nothing appears in the notebook or logs):
   - Kaggle: Add-ons → Secrets, name `SPEEXX_ACCOUNTS`, one `email password` per line.
   - Colab: Colab Pro+ secrets or `google.colab.userdata`, same name and format.
   The notebook (`sonsole_cloud.ipynb`) writes them to `Accounts.md` at runtime.
2. **Paste into `Accounts.md`** in the cell that runs before launching. Never
   `print` the file, and don't save the notebook with credentials in it.

## Run

```bash
./start.sh 12600          # all accounts, 3.5 h window, in background
./stop.sh                 # stop everything
tail -f ~/speexx_autocycle_multi/session1_*.log.console   # what actually happened
```

Same log locations as the local project (`~/speexx_autocycle_multi_run.log`).

## Cloud caveats

- **Nothing persists.** A Colab/Kaggle session ends → profile dirs, logs and
  `.venv` are gone. Re-run `cloud_setup.sh` and re-login on every fresh session
  (Kaggle's `/kaggle/working` survives, but the Playwright cache still does not).
- **One public IP per session.** All parallel accounts on one Colab/Kaggle box
  share that IP — that is exactly why the device profiles (1..N) exist and are
  assigned by `multi_run.sh`. Do not reduce the stagger (15 s).
- **Never run the same account here and in the local bot at the same time** —
  two devices on one account means one invalidates the other's session.
- A **bad password here burns logins from a datacenter IP**, which is what gets
  an IP locked. Test with one known-good account first.
- `check-level` works but must run with `./stop.sh` first, same as locally.
