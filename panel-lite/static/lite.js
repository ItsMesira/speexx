/* lite.js — the lite panel's page logic. Same visual language as the full
 * panel (tile / pill-state / logbox / banner classes from app.css), minus
 * sessions: the optional access token lives in localStorage and rides on
 * every request as X-Token. */

const STATES = {
  idle: ["st-dot-gray", "Idle"],
  starting: ["st-dot-blue", "Starting"],
  running: ["st-dot-blue", "Running"],
  done: ["st-dot-sun", "Done"],
  failed: ["st-dot-ember", "Failed"],
  stopped: ["st-dot-gray", "Stopped"],
};

const TOKEN_KEY = "speexx-lite-token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let poll = null;
let logJobId = null;
let lastLogLen = 0;

function $(id) { return document.getElementById(id); }

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function statePill(state, extra = "") {
  const [dot, label] = STATES[state] || STATES.idle;
  const node = el("span", "pill-state");
  node.append(el("span", "dot " + dot));
  node.append(el("span", undefined, extra ? `${label} · ${extra}` : label));
  return node;
}

function banner(text, tone = "info") {
  const node = el("div", "banner banner-" + tone);
  node.setAttribute("role", "status");
  node.append(el("span", undefined, text));
  return node;
}

function ago(ms) {
  if (!ms) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers["X-Token"] = token;
  const res = await fetch(path, { headers, ...options });
  let body = {};
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = body.code || (res.status === 401 ? "token" : undefined);
    throw err;
  }
  return body;
}

/* ── token gate ─────────────────────────────────────────────────────────── */

function showTokenForm() {
  $("token-slot").classList.remove("hidden");
  $("access-note").textContent = "token-locked";
}

$("token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = $("token-input").value.trim();
  if (!value) return;
  const saved = token;
  token = value;
  $("token-msg").textContent = "";
  try {
    await api("/api/state");
    localStorage.setItem(TOKEN_KEY, token);
    $("token-slot").classList.add("hidden");
    refresh();
  } catch (err) {
    token = saved;
    const msg = $("token-msg");
    msg.textContent = "";
    msg.append(banner("That token was refused.", "bad"));
  }
});

/* ── accounts ───────────────────────────────────────────────────────────── */

function renderAccounts(accounts, jobs) {
  const activeByAccount = new Map();
  for (const job of jobs) {
    if (job.state === "starting" || job.state === "running") {
      activeByAccount.set(job.email, job);
    }
  }
  $("account-count").textContent = accounts.length ? `${accounts.length} saved` : "";
  const host = $("accounts");
  host.textContent = "";

  if (!accounts.length) {
    const empty = el("div", "empty md:col-span-2 xl:col-span-3");
    empty.append(
      el("p", "display-sm", "Nothing saved yet."),
      el("p", "text-[14px] leading-[1.5] max-w-[46ch]",
        "Add your Speexx email and password below. It gets encrypted straight away, and you only type it once."),
    );
    host.append(empty);
    return;
  }

  for (const account of accounts) {
    const tile = el("div", "tile");
    tile.append(
      el("span", "tag", "account"),
      el("p", "text-[15px] font-medium break-all", account.email),
    );
    const meta = el("div", "flex flex-wrap items-center gap-2");
    const active = activeByAccount.get(account.email);
    if (active) {
      meta.append(statePill(active.state));
    } else if (account.cooldown > 0) {
      meta.append(statePill("failed", `cooldown ${Math.ceil(account.cooldown / 60)}m`));
    } else {
      meta.append(statePill("idle", "ready"));
    }
    tile.append(meta);

    const actions = el("div", "tile-actions");
    const run = el("button", "btn btn-filled", "Run one level");
    run.type = "button";
    run.disabled = !!active || account.cooldown > 0;
    run.addEventListener("click", () => startRun(account.id, run));
    actions.append(run);

    if (active) {
      const stop = el("button", "btn btn-ghost", "Stop");
      stop.type = "button";
      stop.addEventListener("click", () => stopJob(active.id, stop));
      actions.append(stop);
    }

    const del = el("button", "btn btn-ghost", "Delete");
    del.type = "button";
    del.addEventListener("click", () => deleteAccount(account.id, del));
    actions.append(del);
    tile.append(actions);
    host.append(tile);
  }
}

async function startRun(accountId, button) {
  button.disabled = true;
  try {
    await api("/api/jobs/start", { method: "POST", body: JSON.stringify({ account_id: accountId }) });
    refresh();
  } catch (err) {
    const host = $("accounts");
    host.before(banner(err.message, err.status === 429 ? "info" : "bad"));
    setTimeout(() => refresh(), 1500);
  }
}

async function stopJob(jobId, button) {
  button.disabled = true;
  try { await api(`/api/jobs/${jobId}/stop`, { method: "POST" }); } catch { /* ignore */ }
  refresh();
}

async function deleteAccount(accountId, button) {
  button.disabled = true;
  try {
    await api(`/api/accounts/${accountId}`, { method: "DELETE" });
    refresh();
  } catch (err) {
    $("add-msg").textContent = "";
    $("add-msg").append(banner(err.message, "bad"));
    button.disabled = false;
  }
}

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("a-email").value.trim();
  const pass = $("a-pass").value;
  $("a-pass").value = "";
  const msg = $("add-msg");
  msg.textContent = "";
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ email, password: pass }) });
    msg.append(banner("Saved — encrypted straight away.", "ok"));
    $("a-email").value = "";
    refresh();
  } catch (err) {
    msg.append(banner(err.message, "bad"));
  }
});

/* ── jobs ───────────────────────────────────────────────────────────────── */

function renderJobs(jobs) {
  const host = $("jobs");
  host.textContent = "";
  const activeJobs = jobs.filter((j) => j.state === "starting" || j.state === "running");
  const recent = jobs.filter((j) => j.state !== "starting" && j.state !== "running").slice(0, 6);
  const shown = [...activeJobs, ...recent].slice(0, 9);
  $("job-note").textContent = activeJobs.length
    ? `${activeJobs.length} running` : "";

  if (!shown.length) {
    const empty = el("div", "empty md:col-span-2 xl:col-span-3");
    empty.append(
      el("p", "display-sm", "No runs yet."),
      el("p", "text-[14px] leading-[1.5] max-w-[46ch]",
        "Press Run on an account above. One whole level, then it stops on its own."),
    );
    host.append(empty);
    return;
  }

  for (const job of shown) {
    const tile = el("div", "tile");
    tile.style.cursor = "pointer";
    tile.addEventListener("click", () => { logJobId = job.id; lastLogLen = 0; refreshLog(); });
    tile.append(
      el("span", "tag", job.profile != null ? `device profile ${job.profile}` : "device"),
      el("p", "text-[15px] font-medium break-all", job.email),
    );
    const meta = el("div", "flex flex-wrap items-center gap-2");
    meta.append(statePill(job.state, job.reason || ""));
    meta.append(el("span", "text-[12px] text-carbon/70 tnum",
      `${job.levels} level${job.levels === 1 ? "" : "s"} · ${job.packets} packet${job.packets === 1 ? "" : "s"} · ${ago(job.started)}`));
    tile.append(meta);

    if (job.state === "starting" || job.state === "running") {
      const actions = el("div", "tile-actions");
      const stop = el("button", "btn btn-ghost", "Stop");
      stop.type = "button";
      stop.addEventListener("click", (ev) => { ev.stopPropagation(); stopJob(job.id, stop); });
      actions.append(stop);
      tile.append(actions);
    }
    host.append(tile);
  }

  if (!logJobId && activeJobs.length) {
    logJobId = activeJobs[0].id;
    lastLogLen = 0;
  }
  $("log-note").textContent = logJobId ? `run #${logJobId}` : "Nothing has run yet.";
}

/* ── log box ────────────────────────────────────────────────────────────── */

function renderLog(text) {
  const box = $("log");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.textContent = "";
  for (const line of text.split("\n")) {
    if (!line) continue;
    const div = el("div");
    if (line.startsWith("LOGIN_OK")) div.className = "lg-ok";
    else if (line.startsWith("LOGIN_FAILED")) div.className = "lg-bad";
    else if (line.startsWith("LEVEL_DONE") || line.startsWith("JOB_DONE")) div.className = "lg-done";
    div.textContent = line;
    box.append(div);
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function refreshLog() {
  if (!logJobId) return;
  try {
    const { log } = await api(`/api/jobs/${logJobId}/log`);
    if (log.length !== lastLogLen) {
      lastLogLen = log.length;
      renderLog(log);
    }
  } catch { /* job gone */ }
}

/* ── polling ────────────────────────────────────────────────────────────── */

let lastState = null;

async function refresh() {
  let data;
  try {
    data = await api("/api/state");
  } catch (err) {
    if (err.code === "token") showTokenForm();
    return;
  }
  $("token-slot").classList.add("hidden");
  lastState = data;
  renderAccounts(data.accounts, data.jobs);
  renderJobs(data.jobs);
  $("access-note").textContent = data.access.token_required ? "token-locked" : "private port";
  if (data.access.token_required && !token) showTokenForm();
  refreshLog();
  const hasActive = data.jobs.some((j) => j.state === "starting" || j.state === "running");
  setPolling(hasActive ? 2000 : 5000);
}

function setPolling(ms) {
  if (poll === ms) return;
  if (poll) clearInterval(poll);
  poll = setInterval(refresh, ms);
}

document.querySelectorAll("[data-marquee]").forEach((track) => {
  track.append(...Array.from(track.children).map((n) => n.cloneNode(true)));
});
refresh();
