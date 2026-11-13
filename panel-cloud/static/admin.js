/* Admin console: people, every run on the machine, and paused accounts. */

import {
  installChrome, api, requireSession, signOut, post,
  el, ago, statePill, banner,
} from "/common.js";

installChrome();

const $ = (id) => document.getElementById(id);

/* No "pending" state any more: sign-up is open, so an account is active the moment
   it exists. Disabled is the only other state, and only an admin sets it. */
const USER_STATES = {
  active: ["st-dot-mint", "Active"],
  disabled: ["st-dot-ember", "Disabled"],
};

/* How many levels somebody can still run. Shape plus a word, never colour alone,
   and zero reads as zero rather than as an empty pill. */
function keyPill(levels) {
  const node = el("span", "pill-state");
  const [dot, label] = levels > 0
    ? ["st-dot-mint", levels === 1 ? "1 level left" : `${levels} levels left`]
    : ["st-dot-gray", "none left"];
  node.append(el("span", "dot " + dot), el("span", undefined, label));
  return node;
}

/* The code exists in readable form exactly once, so it is shown once. */
function showCode(code, username, problem) {
  let host = document.getElementById("minted");
  if (!host) {
    host = el("div", "card card-lav mb-8");
    host.id = "minted";
    $("users").before(host);
  }
  host.textContent = "";
  if (problem) {
    host.append(banner(problem, "bad"));
  } else {
    host.append(
      el("p", "tag text-carbon/70", "key for " + username),
      // Monospace and letter-spaced, not the poster display face: six digits
      // have to be read aloud and typed back, so they must not run together.
      el("p", "keycode", code),
      el("p", "text-[13px] leading-[1.5] text-carbon/80",
        "One key, one level. Hand it over now — only a hash is stored, so this "
        + "cannot be shown again. Mint a fresh one if it gets lost."),
    );
  }
}

function userPill(status) {
  const [dot, label] = USER_STATES[status] || USER_STATES.active;
  const node = el("span", "pill-state");
  node.append(el("span", "dot " + dot), el("span", undefined, label));
  return node;
}

function statTile(label, value, note) {
  const tile = el("div", "tile");
  tile.append(el("span", "tag", label));
  // The display face is for digits. A word like "By approval" at display-sm
  // wrapped to two lines and pushed past the tile.
  const digits = /^\d+$/.test(value);
  tile.append(el("p", digits ? "display-sm" : "display-xs", value));
  if (note) tile.append(el("p", "text-[13px] text-carbon/75", note));
  return tile;
}

function renderStats(data) {
  const host = $("stats");
  host.textContent = "";
  const runnable = data.users.filter((u) => u.levels).length;
  const levels = data.users.reduce((n, u) => n + (u.levels || 0), 0);
  host.append(
    statTile("People", String(data.users.length),
      `${runnable} can run, ${data.users.length - runnable} out of levels`),
    statTile("Runs going", String(data.active.length),
      `${data.limits.pool.length} device slots, ${data.limits.per_user} per person`),
    statTile("In hand", String(levels),
      "levels people can still run"),
    statTile("Saved logins", String(data.users.reduce((n, u) => n + u.accounts, 0)),
      "encrypted at rest"),
    statTile("Sign-up", "Open",
      "anyone can register; a key is what lets them run"),
  );
  const total = data.users.reduce((n, u) => n + (u.levels || 0), 0);
  $("lede").textContent =
    `${data.users.length} ${data.users.length === 1 ? "person" : "people"} can sign in.`
    + ` They have ${total} level${total === 1 ? "" : "s"} in hand.`
    + ` Nobody runs without one of your codes.`;
}

function setStatus(userId, status, button) {
  if (button) button.disabled = true;
  return api(`/api/admin/users/${userId}/status`, {
    method: "POST",
    body: JSON.stringify({ status }),
  })
    .then(refresh)
    .catch((err) => {
      const host = $("users");
      host.prepend(banner(err.message, "bad"));
      if (button) button.disabled = false;
    });
}

function renderUsers(users) {
  const host = $("users");
  host.textContent = "";
  $("user-note").textContent = users.length ? `${users.length} total` : "";
  if (!users.length) {
    const empty = el("div", "empty");
    empty.append(
      el("p", "display-sm", "Nobody yet."),
      el("p", "text-[14px] leading-[1.5] max-w-[46ch]",
        "People appear here as they sign up. Each one needs a key from you "
        + "before they can run."),
    );
    host.append(empty);
    return;
  }

  for (const user of users) {
    const row = el("div", "card flex flex-wrap items-center justify-between gap-4 py-4");
    const left = el("div", "flex flex-col gap-1 min-w-0");
    const line = el("div", "flex flex-wrap items-center gap-2");
    line.append(el("span", "text-[15px] font-medium break-all", user.username));
    if (user.role === "admin") line.append(el("span", "mono-chip", "admin"));
    line.append(keyPill(user.levels || 0));
    left.append(line);
    left.append(el("p", "text-[12px] text-carbon/70 tnum",
      `${user.accounts} saved · ${user.jobs} run${user.jobs === 1 ? "" : "s"}`
      + ` · joined ${ago(user.created_at * 1000)}`
      + (user.unused_keys ? ` · ${user.unused_keys} key waiting` : "")));

    const right = el("div", "flex flex-wrap items-center gap-2");
    right.append(userPill(user.status));

    // One label for both cases: the admin is always handing out one more level,
    // whether or not this person still has some. A label that flips between
    // "give" and "add" only makes the admin wonder what changed.
    const mint = el("button", "btn btn-filled", "Give a level");
    mint.type = "button";
    mint.addEventListener("click", async () => {
      mint.disabled = true;
      try {
        const res = await api(`/api/admin/users/${user.id}/key`, {
          method: "POST", body: JSON.stringify({ note: "" }),
        });
        showCode(res.code, user.username);
        await refresh();
      } catch (err) {
        mint.disabled = false;
        showCode(null, user.username, err.message);
      }
    });
    right.append(mint);

    if (user.status === "disabled") {
      const on = el("button", "btn btn-ghost", "Re-enable");
      on.type = "button";
      on.addEventListener("click", () => setStatus(user.id, "active", on));
      right.append(on);
    } else if (user.role !== "admin") {
      const off = el("button", "btn btn-ghost", "Disable");
      off.type = "button";
      off.addEventListener("click", () => setStatus(user.id, "disabled", off));
      right.append(off);
    }

    row.append(left, right);
    host.append(row);
  }
}

function renderJobs(jobs) {
  const host = $("jobs");
  host.textContent = "";
  $("job-note").textContent = jobs.length ? `${jobs.length} recent` : "";
  if (!jobs.length) {
    const empty = el("div", "empty");
    empty.append(
      el("p", "display-sm", "No runs yet."),
      el("p", "text-[14px] leading-[1.5]", "Every run from every account shows up here."),
    );
    host.append(empty);
    return;
  }
  for (const job of jobs) {
    const row = el("div", "card flex flex-wrap items-center justify-between gap-4 py-4");
    const left = el("div", "flex flex-col gap-1 min-w-0");
    left.append(el("p", "text-[15px] font-medium break-all", job.email));
    left.append(el("p", "text-[12px] text-carbon/70 tnum",
      `${job.owner} · ${job.packets || 0} packets · slot ${job.profile}`
      + ` · ${ago(job.created_at)}${job.reason ? ` · ${job.reason}` : ""}`));
    const right = el("div", "flex items-center gap-2");
    right.append(statePill(job.state));
    row.append(left, right);
    host.append(row);
  }
}

function renderCooldowns(list) {
  const host = $("cooldowns");
  host.textContent = "";
  if (!list.length) {
    const empty = el("div", "empty");
    empty.append(
      el("p", "display-sm", "Nothing paused."),
      el("p", "text-[14px] leading-[1.5]",
        "A wrong password or a lockout pauses that address so the portal never sees a retry."),
    );
    host.append(empty);
    return;
  }
  for (const item of list) {
    const row = el("div", "card card-lav flex flex-wrap items-center justify-between gap-4 py-4");
    const left = el("div", "flex flex-col gap-1 min-w-0");
    left.append(el("p", "text-[15px] font-medium break-all", item.email));
    left.append(el("p", "text-[12px] text-carbon/80", `reason: ${item.reason}`));
    const right = el("div", "flex items-center gap-2");
    right.append(statePill("failed", "paused " + ago(item.until)));
    row.append(left, right);
    host.append(row);
  }
}

function showGate(message, bad) {
  $("gate").classList.remove("hidden");
  $("console").classList.add("hidden");
  if (message) {
    const host = $("gate-msg");
    host.textContent = "";
    host.append(banner(message, bad ? "bad" : "good"));
  }
}

function showConsole() {
  $("gate").classList.add("hidden");
  $("console").classList.remove("hidden");
}

async function refresh() {
  try {
    const data = await api("/api/admin/overview");
    renderStats(data);
    renderUsers(data.users);
    renderJobs(data.jobs);
    renderCooldowns(data.cooldowns);
    showConsole();
  } catch (err) {
    // Stay on this page and ask for a name and password instead of bouncing to
    // /login, which made the admin page look broken rather than locked.
    if (err.status === 401 || err.status === 403) showGate();
    else showGate(err.message, true);
  }
}

$("gate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("gate-submit");
  submit.disabled = true;
  try {
    await post("/api/auth/login", {
      username: $("gate-username").value.trim(),
      password: $("gate-password").value,
    });
    $("gate-password").value = "";
    const me = await api("/api/auth/me");
    if (!me.authenticated || me.user.role !== "admin") {
      showGate("That account is not an admin.", true);
      return;
    }
    $("who").textContent = me.user.username;
    await refresh();
  } catch (err) {
    showGate(err.message, true);
  } finally {
    submit.disabled = false;
  }
});

$("signout").addEventListener("click", () => signOut());

(async function boot() {
  // redirect:false — the gate on this very page is where you sign in.
  const me = await requireSession({ redirect: false });
  if (me && me.role === "admin") {
    $("who").textContent = me.username;
    await refresh();
    setInterval(refresh, 5000);
    return;
  }
  showGate();
})();