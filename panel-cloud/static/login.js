/* Sign-in form. */
import { installChrome, api, setCsrf, banner } from "/common.js";

installChrome();

const form = document.getElementById("form");
const msg = document.getElementById("msg");
const submit = document.getElementById("submit");

// Only ever redirect to a path on this origin. An absolute URL here would be an
// open redirect: /login?next=https://evil.example.com would bounce a signed-in
// visitor off-site with their session fresh in hand.
function safeNext() {
  const raw = new URLSearchParams(location.search).get("next") || "/dashboard";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/dashboard";
  return raw;
}

// Already signed in? Skip the form.
api("/api/auth/me")
  .then((me) => {
    if (me.authenticated) location.replace(safeNext());
  })
  .catch(() => {});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg.textContent = "";
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    const res = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
      }),
    });
    setCsrf(res.csrf);
    // Admins go to the admin page; it has its own sign-in, so there is nothing
    // to send them via /login?next=.
    location.href = res.user.role === "admin" ? "/admin" : safeNext();
  } catch (err) {
    msg.replaceChildren(banner(err.message, err.code === "disabled" ? "warn" : "bad"));
    submit.disabled = false;
    submit.textContent = "Sign in";
    document.getElementById("password").value = "";
  }
});