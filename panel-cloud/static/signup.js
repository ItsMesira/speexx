/* Sign-up form. Kept external because the CSP allows script-src 'self' only. */
import { installChrome, api, banner } from "/common.js";

installChrome();

const form = document.getElementById("form");
const msg = document.getElementById("msg");
const submit = document.getElementById("submit");

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg.textContent = "";
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  const confirm = document.getElementById("confirm").value;

  if (!username) {
    msg.replaceChildren(banner("Pick a username.", "bad"));
    return;
  }
  if (password !== confirm) {
    msg.replaceChildren(banner("Those two passwords do not match.", "bad"));
    return;
  }
  if (password.length < 8) {
    msg.replaceChildren(banner("Use at least eight characters.", "bad"));
    return;
  }
  submit.disabled = true;
  submit.textContent = "Creating…";
  try {
    const res = await api("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    msg.replaceChildren(banner(res.message, res.status === "active" ? "ok" : "warn"));
    form.querySelectorAll("input").forEach((i) => (i.value = ""));
    if (res.role === "admin") {
      // First account on an empty panel: it owns the panel, so say so.
      submit.textContent = "You are the admin — sign in to open the control room";
    } else {
      submit.textContent = "Created — taking you to sign in";
      setTimeout(() => (location.href = "/login"), 1400);
    }
  } catch (err) {
    msg.replaceChildren(banner(err.message, "bad"));
    submit.disabled = false;
    submit.textContent = "Create account";
  }
});
