# PRODUCT.md — Speexx Panel

Durable product truth. Not a changelog; the parts an agent would otherwise have to
re-derive.

## What it is

A multi-user web panel that runs the Speexx standard-packet bot. People **sign up, sign
in**, save their Speexx login once, and press **run**. One run works through **one whole
level** — every packet in it — then stops by itself and says done.

People sign in with a **username** and a password of **8 or more** characters. There is
no email address in a panel account: not as an identifier, not stored, not shown. The
only address the panel holds is the Speexx login someone saves to run with.

It exists to remove three problems with the existing supervisors:

1. **They run forever.** `start.sh` launches every account in `Accounts.md` for a
   window, with no notion of "that one is finished, look at something else".
2. **They hold browser profiles on disk.** A profile left behind can silently run as the
   wrong account.
3. **They cannot be handed to a person.** A supervisor is an operator's tool.

## What it is not

- Not a course-completion tool. One level, then stop.
- Not a progress dashboard for the portal. It counts packets in *this* run, not lifetime
  progress.
- Not a credential store for the portal at large. A saved login exists to run this bot.

## People and permissions

| Role | Can |
|---|---|
| nobody | read the landing page, sign up, sign in |
| the first arrival | becomes the admin, immediately, and is told so |
| anyone with an account | save up to 20 Speexx logins, see only their own runs |
| anyone **with a level in hand** | run up to 2 levels at a time |
| `admin` | mint and revoke keys, disable people, watch every run. **Cannot run** |

**Sign-up is open to everybody.** No approval queue. Having an account is free.

**Running is what costs something**, and the price is a **six digit key from the admin**.
That is the whole access model: the panel is not rationed by who may register, it is
rationed by who may run. People can arrive, sign up, sign in and get comfortable; only
somebody the admin has handed a key to can spend a device slot.

**One key buys one level**, and it is spent the moment they press run — including when
they press Stop a second later. That was a deliberate choice: charging only on
completion would let somebody explore for free by stopping repeatedly. The admin console
shows how many levels each person has left and hands out one more with one click.

**The first arrival owns the panel**, because open sign-up plus a generated admin
password would leave nobody able to hand out the first key.

**The admin is deliberately not a user.** No accounts section, no run button. An admin
who wants to run mints themselves a key and redeems it on their own dashboard, like
anybody else. It keeps the credential-bearing page separate from the all-users view.

**The admin page is its own place.** `/admin` carries its own sign-in form and never
sends you to the login page; the console has no link to the user dashboard. Being sent
somewhere else to reach it read as a broken page, not a locked one.

## The unit of work: one level

A **packet** is `standard-packet/<id>` and holds many exercises. A **level** is the whole
article level and holds *many* packets — often around seventy. These were once conflated
in this project, and counting packets as "lessons" stopped a job about a fifth of the way
through. A run's headline number is **packets**, and its completion condition is **one
level**.

- Run ends when the cap of `--max-levels 1` is reached: state `done`.
- Run ends when the portal has nothing left: state `failed`, reason `no_work`. **This is a
  real outcome, not a fault** — it is what an account at the end of its level looks like.
- Run ends at the wall-clock backstop: state `failed`, reason `time_cap`. Deliberately not
  `done`, because a stalled run must never be reported as a completed level.

## What a person sees

| Page | Shows |
|---|---|
| `/` | what it does, in three steps, and the security claims it can actually back |
| `/signup` | email + password, with the approval rule stated up front |
| `/login` | sign in, or a link to sign up |
| `/dashboard` | the live run, packets this level, saved accounts, recent runs |
| `/admin` | people and their status, every run with its owner, paused addresses |

Empty states are written as sentences that say what to do next, never as a bare "No data".

## Security promises the UI may make

The landing page claims these, so each one is enforced and has a test:

- **"Your Speexx password is encrypted on disk."** AES-256-GCM, AAD bound to owner and
  address, so a copied ciphertext will not decrypt. `vault.py`, `VaultTests`.
- **"Yours stays yours."** Every read filters on `user_id`; `get_owned` returns `None`
  rather than raising, so a forgotten check still cannot leak. `OwnershipTests`.
- **"One key, one level."** The counter in the nav is the honest number, and the key
  box comes back the moment it hits zero.
- **"Six digits, and we still handle it safely."** The code is stored only as a scrypt
  hash, bound to one name, spent once, and redeem is the most tightly rate-limited
  endpoint in the panel. `test_keys.py` greps the raw database for a freshly minted
  code.
- **"A wrong password stops dead."** Classified fatal, never retried, address paused.
  `preflight.py`, verified live on a real account.
- **"Runs are kept apart."** Distinct device fingerprint per run and a 15-second stagger,
  matching the sibling `multi_run.sh` exactly.
- **"Nothing leaks into logs."** stdin for credentials, never argv; two scrubbers for what
  gets written down.

A claim on the landing page that is not enforced is a lie, so add the test first.

## Wiping it

`./run.sh --reset` clears every account, saved login, run, session, audit row, rate-limit
bucket and cooldown, and deletes the vault key — which, with nothing left to decrypt, is
the honest thing to do. Job logs stay: they are the only record of what a run did.
`./run.sh --reset-admin` is the smaller version, for when only ownership needs to change.

## Deliberate non-features

- **No MCP.** Explicitly rejected; the panel is a web app and nothing else.
- **No seven digits.** The user asked for six. It is defended by hashing, binding and
  rate limits, not by more entropy.
- **No lifetime progress tracking.** Out of scope; would need the portal's own reporting.
- **No account sharing.** One Speexx login, one person, enforced globally.