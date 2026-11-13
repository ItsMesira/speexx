/* The dashboard: saved accounts, the live run, and history.
 *
 * Every fetch is scoped server-side by the session's user id. Nothing here
 * sends or trusts a user id — the server derives it from the cookie, which is
 * why there is no way for this page to ask for somebody else's data. */

import {
  installChrome, api, post, requireSession, signOut,
  el, ago, statePill, banner,
} from "/common.js";

installChrome();

const $ = (id) => document.getElementById(id);
const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 10000;

let me = null;
let accounts = [];
let polling = 0;
let activeJobId = null;

/* ── Run state ─────────────────────────────────────────────────────────── */

const LIVE = new Set(["queued", "starting", "running"]);

function renderProgress(job) {
  const packets = String(Math.max(0, job.packets || 0)).padStart(2, "0");
  $("packets").textContent = packets;
  const done = job.state === "done";
  $("levels").textContent = done
    ? `Level complete · ${job.levels || 1} of 1`
    : `Level ${Math.min((job.levels || 0) + 1, 1)} of 1`;

  $("state-slot").replaceChildren(statePill(job.state));
  $("headline").textContent = done ? "DONE." : LIVE.has(job.state) ? "RUNNING." : "RUN IT.";
  $("job-email").textContent = job.email ? `Running ${job.email}` : "Nothing running.";
  $("started").textContent = job.started_at ? ago(job.started_at) : "—";
  const canStop = LIVE.has(job.state);
  $("stop").disabled = !canStop;
  $("stop").classList.toggle("hidden", !canStop);

  const notes = {
    done: "Whole level finished.",
    failed: "Stopped early.",
    stopped: "You stopped this one.",
  };
  $("log-note").textContent = LIVE.has(job.state)
    ? "Live."
    : job.state === "idle"
      ? "Nothing has run yet."
      : notes[job.state] || "";
}

function renderLog(lines) {
  const box = $("log");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  box.textContent = "";
  for (const [kind, text] of lines) {
    const div = document.createElement("div");
    div.className =
      { ok: "lg-ok", bad: "lg-bad", done: "lg-done", plain: "" }[kind] || "";
    div.textContent = text;
    box.append(div);
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function setPolling(ms) {
  if (polling === ms) return;
  if (polling) clearInterval(polling);
  polling = ms;
  polling = setInterval(refreshRun, ms);
}

/* ── Saved accounts ────────────────────────────────────────────────────── */

function renderAccounts() {
  $("account-count").textContent = accounts.length
    ? `${accounts.length} saved`
    : "";
  const host = $("accounts");
  host.textContent = "";

  if (!accounts.length) {
    const empty = el("div", "empty md:col-span-2 xl:col-span-3");
    empty.append(
      el("p", "display-sm", "Nothing saved yet."),
      el("p", "text-[14px] leading-[1.5] max-w-[46ch]",
        "Add your Speexx email and password below. It gets encrypted straight away, and you only "
          + "type it once."),
    );
    host.append(empty);
    return;
  }

  for (const account of accounts) {
    const tile = el("div", "tile");
    tile.append(
      el("span", "tag", account.label || "account"),
      el("p", "text-[15px] font-medium break-all", account.email),
    );
    const meta = el("div", "flex flex-wrap items-center gap-2");
    if (account.blocked) {
      meta.append(statePill("failed", "paused"));
      meta.append(el("span", "text-[12px] text-carbon/70",
        "unpause in a moment, then run again"));
    } else {
      meta.append(statePill(account.last_job_at ? "done" : "idle",
        account.last_job_at ? "ran before" : "never run"));
    }
    tile.append(meta);

    const actions = el("div", "tile-actions");
    const run = el("button", "btn btn-filled", account.blocked ? "Paused" : "Run one level");
    run.type = "button";
    run.disabled = !!account.blocked;
    run.addEventListener("click", () => startRun(account.id, run));
    actions.append(run);

    if (account.blocked) {
      const unblock = el("button", "btn btn-ghost", "Unpause");
      unblock.type = "button";
      unblock.addEventListener("click", async () => {
        try {
          await api(`/api/accounts/${account.id}/unblock`, { method: "POST" });
          await loadAccounts();
        } catch (err) {
          flash(banner(err.message, "bad"));
        }
      });
      actions.append(unblock);
    }

    const remove = el("button", "btn btn-ghost", "Remove");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api(`/api/accounts/${account.id}`, { method: "DELETE" });
        await loadAccounts();
      } catch (err) {
        flash(banner(err.message, "bad"));
        remove.disabled = false;
      }
    });
    actions.append(remove);

    tile.append(actions);
    host.append(tile);
  }
}

function flash(node) {
  const host = $("add-msg");
  host.textContent = "";
  if (node) host.append(node);
}

/* ── History ───────────────────────────────────────────────────────────── */

function renderHistory(jobs) {
  const host = $("history");
  host.textContent = "";
  $("history-note").textContent = jobs.length ? `${jobs.length} recent` : "";
  if (!jobs.length) {
    const empty = el("div", "empty");
    empty.append(
      el("p", "display-sm", "No runs yet."),
      el("p", "text-[14px] leading-[1.5] max-w-[46ch]",
        "Save an account above, then press run. Runs show up here with their packet count and why they stopped."),
    );
    host.append(empty);
    return;
  }
  for (const job of jobs) {
    const row = el("div", "card flex flex-wrap items-center justify-between gap-4 py-4");
    const left = el("div", "flex flex-col gap-1 min-w-0");
    left.append(el("p", "text-[15px] font-medium break-all", job.email));
    left.append(el("p", "text-[12px] text-carbon/70 tnum",
      `${job.packets || 0} packets · ${job.levels || 0} level${job.levels === 1 ? "" : "s"}`
      + ` · ${ago(job.created_at)}${job.reason ? ` · ${job.reason}` : ""}`));
    const right = el("div", "flex items-center gap-3");
    right.append(statePill(job.state));
    if (LIVE.has(job.state)) {
      const stop = el("button", "btn btn-ghost", "Stop");
      stop.type = "button";
      stop.addEventListener("click", async () => {
        stop.disabled = true;
        await api(`/api/jobs/${job.id}/stop`, { method: "POST" }).catch(() => {});
        refreshRun();
      });
      right.append(stop);
    }
    row.append(left, right);
    host.append(row);
  }
}

/* ── Data ──────────────────────────────────────────────────────────────── */

async function loadAccounts() {
  const data = await api("/api/accounts");
  accounts = data.accounts;
  renderAccounts();
}

async function refreshRun() {
  try {
    const { job } = await api("/api/jobs/current");
    if (!job) {
      renderProgress({ state: "idle", packets: 0, levels: 0 });
      renderLog([]);
      setPolling(POLL_IDLE_MS);
      return;
    }
    activeJobId = job.id;
    renderProgress(job);
    renderLog(job.log || []);
    setPolling(LIVE.has(job.state) ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  } catch (err) {
    if (err.status === 401) location.replace("/login");
  }
}

async function loadHistory() {
  const { jobs } = await api("/api/jobs");
  renderHistory(jobs);
}

/* The key box. Shown on load for anybody without one, and again whenever the
   server says `needs_key`, which is the only source of truth for the answer. */
function showKeyBox(visible, message) {
  const gate = $("gate");
  gate.classList.toggle("hidden", !visible);
  if (message) $("gate-note").textContent = message;
  if (visible && !gate.dataset.touched) gate.querySelector("#key-code").focus();
}

async function refreshKey() {
  let state;
  try {
    state = await api("/api/key");
  } catch {
    return;
  }
  const out = !state.levels;
  showKeyBox(out);
  if (out) {
    // The box reappearing means they are out of levels; a stale "that key
    // worked, 2 levels to run" left in it would contradict the heading.
    $("gate-msg").textContent = "";
  }
  const chip = document.getElementById("levels-left");
  if (chip) {
    chip.classList.toggle("hidden", state.levels === 0);
    chip.textContent = state.levels === 1 ? "1 level left" : `${state.levels} levels left`;
  }
  return state;
}

$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = $("key-code").value.trim();
  const submit = $("key-submit");
  const msg = $("gate-msg");
  msg.textContent = "";
  submit.disabled = true;
  try {
    const res = await post("/api/key/redeem", { code });
    $("key-code").value = "";
    await refreshKey();
    showKeyBox(false);
    msg.textContent = "";
    const left = res.levels === 1 ? "1 level" : `${res.levels} levels`;
    msg.append(banner(`That key worked. You have ${left} to run.`, "good"));
    flash(banner("Key added. Pick a saved login and press run.", "good"));
  } catch (err) {
    msg.append(banner(err.message, "bad"));
  } finally {
    submit.disabled = false;
  }
});

async function startRun(accountId, button) {
  if (button) {
    button.disabled = true;
    button.textContent = "Starting…";
  }
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ account_id: accountId }),
    });
    activeJobId = job.id;
    renderProgress(job);
    setPolling(POLL_ACTIVE_MS);
    await Promise.all([loadAccounts(), loadHistory(), refreshKey()]);
  } catch (err) {
    const notes = {
      account_busy: "That account already has a run going.",
      account_blocked: "That account is paused after a bad login. Unpause it first.",
      no_slot: "Every device slot is busy. Try again in a moment.",
      too_many_jobs: "You already have runs going. Stop one first.",
      undecryptable: "Those saved credentials cannot be decrypted. Remove and re-add the account.",
    };
    if (err.code === "needs_key") {
      // Not a failure to report in a corner: this is the normal state of a new
      // account, and the fix is to type the key in.
      showKeyBox(true, err.message);
      $("gate").scrollIntoView({ behavior: "smooth", block: "center" });
      refreshKey();
      return;
    }
    flash(banner(notes[err.code] || err.message, err.code === "no_slot" ? "warn" : "bad"));
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Run one level";
    }
  }
}

/* ── Wiring ────────────────────────────────────────────────────────────── */

$("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const submit = $("a-submit");
  submit.disabled = true;
  submit.textContent = "Saving…";
  try {
    await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        email: $("a-email").value.trim(),
        password: $("a-pass").value,
        label: $("a-label").value.trim(),
      }),
    });
    $("add-form").querySelectorAll("input").forEach((i) => (i.value = ""));
    flash(banner("Saved. It is encrypted on disk from here.", "ok"));
    await loadAccounts();
  } catch (err) {
    flash(banner(err.message, err.code === "limit" ? "warn" : "bad"));
  } finally {
    submit.disabled = false;
    submit.textContent = "Save account";
  }
});

$("stop").addEventListener("click", async () => {
  if (!activeJobId) return;
  $("stop").disabled = true;
  await api(`/api/jobs/${activeJobId}/stop`, { method: "POST" }).catch(() => {});
  refreshRun();
});

$("signout").addEventListener("click", () => signOut());

(async function boot() {
  me = await requireSession();
  if (!me) return;
  $("who").textContent = me.username;
  if (me.role === "admin") {
    const link = $("adminlink");
    link.hidden = false;
    link.href = "/admin";
  }
  await refreshKey();
  await loadAccounts();
  await refreshRun();
  await loadHistory();
  setInterval(loadHistory, 15000);
})();