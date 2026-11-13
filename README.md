# speexx — cloud copies of the two bots

Two independent bots for `portal.speexx.com`, packaged to run unmodified in a
**GitHub Codespace**, Colab or Kaggle. The macOS originals live elsewhere; the
bot logic here is byte-identical to them, only the environment layer differs.

| Folder | What it is | Entry points |
|---|---|---|
| `Speexx-sonsole-cloud/` | standard-packet AutoCycle bot (multi-account) | `cloud_setup.sh`, `start.sh`, `AGENTS.md` |
| `panel-cloud/` | multi-user web panel + its runner | `cloud_setup.sh`, `run.sh`, `AGENTS.md` |
| `panel-lite/` | single-operator panel: add account → run → watch. No users, no signup, no keys | `run.sh`, `stop.sh`, `AGENTS.md` |

Per-project docs: `Speexx-sonsole-cloud/CLOUD.md`, `panel-cloud/CLOUD.md`, plus
the full behavior reference in each folder's `AGENTS.md`.

### Panel Lite — when you just want to add and watch runs

`panel-cloud` assumes several strangers sharing one panel (accounts, keys,
sessions). If you are the only operator, `panel-lite` is the same theme and the
same runner with all of that removed:

```bash
cd panel-lite
./run.sh 8788        # 127.0.0.1:8788; tunnels via NGROK_AUTHTOKEN if set
./stop.sh
```

- Add a Speexx email + password on the page, press **Run one level** — it
  stops by itself after one whole level.
- Access: a **private Codespaces port needs no token**; a public tunnel gets a
  token (printed once by `run.sh`, remembered by the browser).
- Same safety rules as the full panel: bad-password runs never retry
  (cooldown instead), one run per address, distinct device profiles, 15 s
  login stagger. See `panel-lite/AGENTS.md`.

## Run both at once

```bash
bash run_all.sh            # panel-lite (public URL when possible) + all bot accounts, 3.5 h
bash run_all.sh 7200       # 2 h bot window
bash run_all.sh stop       # stop both
```

The panel in this one-command flow is **panel-lite** (port 8788): no users, no
keys — it prints an access token once when a tunnel is up; paste it on the page.
It writes `Accounts.md` from the `SPEEXX_ACCOUNTS` secret if you have one, builds
whichever environment is missing, starts the panel first (prints the public URL or
the Codespaces port-forward hint), then the bot. On Colab/Kaggle open
`run_both_cloud.ipynb` at the repo root and run it top to bottom instead.

## Codespaces: first run

The devcontainer builds both environments automatically (venvs, Playwright
Chromium, Linux system libraries). Nothing else to install.

### Credentials

Never commit credentials — `Accounts.md` and `alluser.md` are gitignored and the
repo ships none. Inside the Codespace:

```bash
cd Speexx-sonsole-cloud
nano Accounts.md        # or: export from a Codespaces secret
```

Format (two accepted shapes):

```
# comment lines skipped
email@example.com
password-on-its-own-line
email2@example.com password2-on-the-same-line
```

Or set a **Codespaces secret** `SPEEXX_ACCOUNTS` (one `email password` per line)
and write it out at runtime:

```bash
cd Speexx-sonsole-cloud
printf '%s\n' "$SPEEXX_ACCOUNTS" > Accounts.md
```

### Run the bot

```bash
cd Speexx-sonsole-cloud
./start.sh 12600        # all accounts, 3.5 h window, staggered, in background
tail -f ~/speexx_autocycle_multi/session1_*.log.console   # what actually happened
./stop.sh               # stop everything
```

Browser runs headless automatically (no display in a codespace). Device profiles
1..N are assigned per session so parallel accounts on the one codespace IP look
like distinct devices — do not reduce the 15 s stagger.

### Run the panel

`run_all.sh` starts **panel-lite** (port 8788) — see the Panel Lite section
above. The full multi-user panel below is still available when you want
accounts/keys for several people; it is not part of the one-command flow.

```bash
cd panel-cloud
./run.sh 8787
```

Open the Ports panel → port 8787 → the forwarded URL. On first use set the port
to **Public** to reach it from outside your session. The panel's Host allowlist
auto-accepts `*.app.github.dev` when `CODESPACES` is set. First person to sign
up owns the panel, or bootstrap one first:

```bash
SPEEXX_PANEL_ADMIN_USERNAME=admin SPEEXX_PANEL_ADMIN_PASSWORD='one you choose' ./run.sh 8787
```

Optional public URL via ngrok instead of the port forward (works in codespaces):

```bash
export NGROK_AUTHTOKEN='<your token>'    # Codespaces secret works too
./run.sh 8787 --with-tunnel              # prints the URL, writes data/tunnel-url.txt
```

### Panel data on a codespace

Codespace disk dies with the codespace. Saved Speexx logins are sealed with the
vault key (`data/vault.key`); to survive rebuilds set
`SPEEXX_PANEL_VAULT_KEY=<64 hex chars>` to a value you keep (Codespaces secret
`SPEEXX_PANEL_VAULT_KEY` works).

## Verification (as in the originals)

```bash
cd Speexx-sonsole-cloud
python3 .venv/bin/python -m py_compile speexx_autocycle.py check_level.py   # after cloud_setup.sh
node check.js

cd ../panel-cloud
.venv/bin/python -m py_compile server.py db.py users.py accounts.py vault.py auth.py jobs.py preflight.py runner.py
.venv/bin/python -m unittest discover -s tests -t .
node solver_check.js
bash -n run.sh stop.sh tunnel.sh
diff solver.js ../Speexx-sonsole-cloud/optimized.js    # must be identical
```

## Colab / Kaggle

Both folders also run there unchanged: open the notebook in each folder
(`sonsole_cloud.ipynb`, `panel_cloud.ipynb`) and run top to bottom — secrets
come from Kaggle Secrets / Colab userdata (`SPEEXX_ACCOUNTS`, `NGROK_AUTHTOKEN`).

## House rules

- **Never commit credentials** — not in code, docs, logs or filenames.
- `panel-cloud/solver.js` stays byte-identical to
  `Speexx-sonsole-cloud/optimized.js`.
- Timings, selectors and login-classification rules are tuned and load-bearing;
  see each project's `AGENTS.md` before changing any.
