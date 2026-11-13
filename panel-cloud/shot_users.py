"""Capture every page of the multi-user panel for design review.

Usage:  .venv/bin/python shot_users.py [base_url] [outdir] [admin_username] [password]

Signs in for real against a running panel so the dashboard and the admin
console render with real rows in them. Renders local pages only: nothing here
touches the Speexx portal.
"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "../.review")
ADMIN_EMAIL = sys.argv[3] if len(sys.argv) > 3 else ""
ADMIN_PASSWORD = sys.argv[4] if len(sys.argv) > 4 else ""
# The page label is a username now, so name the arguments to match.
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [("desktop", 1440, 900), ("mobile", 390, 844)]

PUBLIC = [("landing", "/"), ("login", "/login"), ("signup", "/signup")]


def shoot(page, name, url):
    """Full-page shot, and a list of anything the browser complained about."""
    problems = []
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(BASE + url, wait_until="networkidle")
    # Scroll home first: the nav is position:sticky, and a full-page screenshot
    # paints a sticky element at whatever offset the page was scrolled to, which
    # drops it into the middle of the image.
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(700)
    for label, w, h in VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(250)
        path = OUT / f"{name}-{label}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"  {path.name}")
    return problems


def main() -> int:
    bad = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=2)
            page = ctx.new_page()
            for name, url in PUBLIC:
                bad += [(name, p) for p in shoot(page, name, url)]

            if ADMIN_EMAIL and ADMIN_PASSWORD:
                # Public pages that need a signed-in cookie.
                # The admin page signs you in on the page itself.
                bad += [(name, p) for p in shoot(page, "admin-gate", "/admin")]
                page.fill("#gate-username", ADMIN_EMAIL)
                page.fill("#gate-password", ADMIN_PASSWORD)
                page.click("#gate-submit")
                page.wait_for_timeout(1100)
                if page.locator("#console").is_visible():
                    bad += [(name, p) for p in shoot(page, "admin", "/admin")]
                    # Mint a key so the one-shot code panel is reviewed too.
                    mint = page.locator("#users button", has_text="Mint key")
                    if mint.count():
                        mint.first.click()
                        page.wait_for_timeout(700)
                        page.evaluate("() => window.scrollTo(0, 0)")
                        page.wait_for_timeout(200)
                        path = OUT / f"admin-key-{name}.png"
                        page.screenshot(path=str(path), full_page=True)
                        print(f"  {path.name}")
                else:
                    bad.append(("admin", "sign-in did not open the console"))

                # Sign out, then back in on the ordinary login page to see the
                # dashboard. Logout is CSRF-protected like every other write, so
                # the token has to travel with it.
                page.evaluate(
                    "async () => {"
                    "  const me = await (await fetch('/api/auth/me',"
                    "    {credentials: 'same-origin'})).json();"
                    "  await fetch('/api/auth/logout', {method: 'POST',"
                    "    credentials: 'same-origin',"
                    "    headers: {'X-CSRF-Token': me.csrf}});"
                    "}")
                page.wait_for_timeout(400)
                page.goto(BASE + "/login", wait_until="networkidle")
                page.fill("#username", ADMIN_EMAIL)
                page.fill("#password", ADMIN_PASSWORD)
                page.click("#submit")
                page.wait_for_timeout(900)
                if page.locator("#who").count():
                    bad += [(name, p) for p in shoot(page, "dashboard", "/dashboard")]
            ctx.close()
        browser.close()

    if bad:
        print("\nbrowser said:")
        for name, problem in dict.fromkeys(bad):
            print(f"  {name}: {problem}")
        return 1
    print("\nno console errors, page errors or warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())