/** Shared page chrome: the sticker sprite, the marquee loop, and the API client.
 *  Imported by every page so the sprite and the fetch rules live in one place. */

const SPRITE = `<svg width="0" height="0" aria-hidden="true" class="deco"><defs>
<filter id="grain" x="-12%" y="-12%" width="124%" height="124%"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="3" seed="7" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="7" xChannelSelector="R" yChannelSelector="G"/></filter>
<symbol id="st-rocket" viewBox="0 0 64 64"><path d="M32 6c9 8 13 18 13 28v10H19V34C19 24 23 14 32 6Z" fill="#fb4903" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/><circle cx="32" cy="26" r="6" fill="#dceeff" stroke="#000" stroke-width="2.5"/><path d="M19 34 9 44v10l10-6M45 34l10 10v10l-10-6" fill="#ffd731" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/><path d="M26 44h12l-6 14Z" fill="#55db9c" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/></symbol>
<symbol id="st-coin" viewBox="0 0 64 64"><circle cx="32" cy="32" r="25" fill="#ffd731" stroke="#000" stroke-width="2.5"/><circle cx="32" cy="32" r="16" fill="#e9ccff" stroke="#000" stroke-width="2.5"/><path d="M32 21v22M25 27h14M25 37h14" stroke="#000" stroke-width="3" stroke-linecap="round"/></symbol>
<symbol id="st-lock" viewBox="0 0 64 64"><rect x="12" y="28" width="40" height="28" rx="7" fill="#e9ccff" stroke="#000" stroke-width="2.5"/><path d="M21 28v-8a11 11 0 0 1 22 0v8" fill="none" stroke="#000" stroke-width="2.5"/><circle cx="32" cy="41" r="4.5" fill="#000"/></symbol>
<symbol id="st-check" viewBox="0 0 64 64"><circle cx="32" cy="32" r="25" fill="#55db9c" stroke="#000" stroke-width="2.5"/><path d="M20 33l9 9 16-18" fill="none" stroke="#000" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></symbol>
<symbol id="st-cross" viewBox="0 0 64 64"><circle cx="32" cy="32" r="25" fill="#fb4903" stroke="#000" stroke-width="2.5"/><path d="M23 23l18 18M41 23L23 41" stroke="#000" stroke-width="5" stroke-linecap="round"/></symbol>
<symbol id="st-bolt" viewBox="0 0 64 64"><rect x="8" y="8" width="48" height="48" rx="14" fill="#4da2ff" stroke="#000" stroke-width="2.5"/><path d="M36 16 22 36h10l-4 12 14-20H32Z" fill="#ffd731" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/></symbol>
<symbol id="st-wallet" viewBox="0 0 64 64"><rect x="6" y="16" width="52" height="36" rx="7" fill="#5c4ade" stroke="#000" stroke-width="2.5"/><path d="M6 26h52" stroke="#000" stroke-width="2.5"/><rect x="34" y="32" width="20" height="13" rx="4" fill="#e9ccff" stroke="#000" stroke-width="2.5"/><circle cx="41" cy="38.5" r="3" fill="#000"/></symbol>
<symbol id="st-key" viewBox="0 0 64 64"><circle cx="21" cy="32" r="13" fill="#ffd731" stroke="#000" stroke-width="2.5"/><circle cx="21" cy="32" r="4.5" fill="#fff" stroke="#000" stroke-width="2.5"/><path d="M34 32h22M48 32v9M40 32v6" fill="none" stroke="#000" stroke-width="3.5" stroke-linecap="round"/></symbol>
<symbol id="st-flag" viewBox="0 0 64 64"><path d="M18 58V8" stroke="#000" stroke-width="3" stroke-linecap="round" fill="none"/><path d="M18 10h30l-7 10 7 10H18Z" fill="#55db9c" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/></symbol>
<symbol id="st-people" viewBox="0 0 64 64"><circle cx="24" cy="22" r="10" fill="#e9ccff" stroke="#000" stroke-width="2.5"/><circle cx="44" cy="26" r="8" fill="#ffd731" stroke="#000" stroke-width="2.5"/><path d="M6 54c0-10 8-16 18-16s18 6 18 16Z" fill="#e9ccff" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/><path d="M40 54c0-8 4-13 10-13 4 0 8 3 8 13Z" fill="#ffd731" stroke="#000" stroke-width="2.5" stroke-linejoin="round"/></symbol>
</defs></svg>`;

let csrfToken = null;

export function installChrome() {
  document.body.insertAdjacentHTML("afterbegin", SPRITE);
  // translateX(-50%) only loops when the track holds two identical halves.
  document.querySelectorAll("[data-marquee]").forEach((track) => {
    track.append(...Array.from(track.children).map((n) => n.cloneNode(true)));
  });
}

export function setCsrf(token) {
  csrfToken = token || null;
}

export function getCsrf() {
  return csrfToken;
}

/** Fetch wrapper. Throws an Error carrying `status` and `code`. */
export async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (csrfToken && (options.method || "GET") !== "GET") headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, { credentials: "same-origin", headers, ...options });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail = body && (body.detail || body);
    const err = new Error((detail && detail.error) || `Failed (${res.status})`);
    err.status = res.status;
    err.code = (detail && detail.code) || "error";
    throw err;
  }
  return body;
}

/** Resolve the current session, or send the visitor to sign in. */
export async function requireSession({ redirect = true } = {}) {
  let me;
  try {
    me = await api("/api/auth/me");
  } catch {
    if (redirect) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname));
    }
    return null;
  }
  if (!me.authenticated) {
    if (redirect) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname));
    }
    return null;
  }
  setCsrf(me.csrf);
  return me.user;
}

/** POST JSON and remember the CSRF token a successful sign-in hands back. */
export async function post(path, body) {
  const out = await api(path, { method: "POST", body: JSON.stringify(body) });
  if (out && out.csrf) setCsrf(out.csrf);
  return out;
}

export function signOut() {
  return api("/api/auth/logout", { method: "POST" }).finally(() => location.replace("/"));
}

/* ── Tiny render helpers, so pages do not each reinvent them ─────────── */

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function ago(ms) {
  if (!ms) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`;
}

/* State → sticker + word. Colour is never the only signal. */
const STATES = {
  idle: ["st-dot-gray", "Idle"],
  queued: ["st-dot-gray", "Queued"],
  starting: ["st-dot-blue", "Starting"],
  running: ["st-dot-blue", "Running"],
  done: ["st-dot-sun", "Done"],
  failed: ["st-dot-ember", "Failed"],
  stopped: ["st-dot-gray", "Stopped"],
};

export function statePill(state, extra = "") {
  const [dot, label] = STATES[state] || STATES.idle;
  const node = el("span", "pill-state");
  node.append(el("span", "dot " + dot));
  const text = extra ? `${label} · ${extra}` : label;
  node.append(el("span", undefined, text));
  return node;
}

export function banner(text, tone = "info") {
  const node = el("div", "banner banner-" + tone);
  node.setAttribute("role", "status");
  node.append(el("span", undefined, text));
  return node;
}