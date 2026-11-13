"""Conformance checks against the supplied style reference.

The bundled Impeccable detector ran in degraded (regex-only) mode because its
parser modules resolve from the skill's own install directory. Rather than report
a clean bill of health from an undercounting check, these assert the reference's
explicit rules directly, so they can be re-run after any styling change.

Run:  .venv/bin/python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "tailwind.css").read_text()
BUILT = (ROOT / "static" / "app.css").read_text()
INDEX = (ROOT / "static" / "index.html").read_text()
ADMIN = (ROOT / "static" / "admin.html").read_text()
SERVER = (ROOT / "server.py").read_text()


class TokenTests(unittest.TestCase):
    REQUIRED = {
        "--color-carbon": "#000000",
        "--color-paper-white": "#ffffff",
        "--color-sky-wash": "#dceeff",
        "--color-concrete-gray": "#cccccc",
        "--color-soft-mist": "#e9e9e9",
        "--color-electric-blue": "#4da2ff",
        "--color-mint-pop": "#55db9c",
        "--color-lavender": "#e9ccff",
        "--color-ember": "#fb4903",
        "--color-sunburst": "#ffd731",
        "--color-voltage-violet": "#5c4ade",
    }

    def test_every_brand_colour_is_transcribed_verbatim(self):
        for token, value in self.REQUIRED.items():
            self.assertRegex(CSS, r"%s:\s*%s\s*;" % (re.escape(token), re.escape(value)),
                             "%s should be %s" % (token, value))

    def test_pill_radii_are_1600px(self):
        for token in ("--radius-nav", "--radius-pills", "--radius-buttons"):
            self.assertRegex(CSS, r"%s:\s*1600px\s*;" % re.escape(token), token)

    def test_card_radius_floor_is_respected(self):
        """The reference forbids a radius under 16px on cards."""
        for name in ("--radius-cards", "--radius-card", "--radius-cards-elevated"):
            found = re.search(r"%s:\s*(\d+)px" % re.escape(name), CSS)
            self.assertIsNotNone(found, name)
            self.assertGreaterEqual(int(found.group(1)), 16, name)


class BannedPatternTests(unittest.TestCase):
    def test_no_gradients(self):
        for label, text in (("tailwind.css", CSS), ("app.css", BUILT)):
            offenders = re.findall(r"[a-z-]+gradient\s*[:(]", text)
            self.assertEqual(offenders, [], "%s uses a gradient: %s" % (label, offenders))

    def test_no_box_shadows(self):
        """Elevation is colour bands plus the 1px black outline, never a shadow.

        `box-shadow: none` is exempt: that is Tailwind's preflight actively
        *removing* inherited shadows, which is the rule being upheld.
        """
        for label, text in (("tailwind.css", CSS), ("app.css", BUILT)):
            offenders = [
                m for m in re.findall(r"box-shadow\s*:\s*([^;}]+)", text)
                if m.strip() not in ("none", "0", "initial")
            ]
            self.assertEqual(offenders, [], "%s casts a shadow: %s" % (label, offenders))

    def test_electric_blue_is_never_a_cta_fill(self):
        block = re.search(r"\.btn-filled\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(block)
        self.assertNotIn("electric-blue", block.group(1))
        self.assertIn("carbon", block.group(1))

    def test_no_external_font_or_asset_requests(self):
        """Fonts are self-hosted so the CSP can stay at 'self'."""
        for label, html in (("index.html", INDEX), ("admin.html", ADMIN)):
            self.assertNotIn("fonts.googleapis.com", html, label)
            self.assertNotIn("fonts.gstatic.com", html, label)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""), label)


class TypographyTests(unittest.TestCase):
    def test_display_leading_is_crushed(self):
        """The reference forbids line-height above 0.85 on display type.

        Matched on the rule that actually declares the face, because the classes
        also appear in a shared text-wrap rule.
        """
        tokens = dict(re.findall(r"(--leading-[\w-]+):\s*([0-9.]+)", CSS))
        rules = re.findall(r"(?<![\w-])\.display(?:-lg|-sm)?\s*\{([^}]*)\}", CSS)
        checked = 0
        for body in rules:
            if "--font-lateral" not in body:
                continue  # the shared text-wrap rule
            checked += 1
            raw = re.search(r"line-height:\s*([^;]+)", body)
            self.assertIsNotNone(raw, body[:60])
            value = raw.group(1).strip()
            # The rules point at the reference's own tokens, so resolve them
            # rather than only accepting literal numbers.
            ref = re.match(r"var\((--leading-[\w-]+)\)", value)
            if ref:
                self.assertIn(ref.group(1), tokens, ref.group(1))
                value = tokens[ref.group(1)]
            self.assertLessEqual(float(value), 0.85,
                                 "display leading %s exceeds the 0.85 ceiling" % value)
        self.assertEqual(checked, 3, "expected three display rules, found %d" % checked)

    def test_display_size_is_capped_by_the_reference_token(self):
        block = re.search(r"\.display\s*\{([^}]*)\}", CSS).group(1)
        self.assertIn("clamp(", block, "display must scale rather than overflow")
        self.assertIn("--text-display", block)

    def test_commercial_faces_are_swappable_by_name(self):
        """--font-lateral keeps its real name so a licensed face is one edit."""
        self.assertIn("--font-lateral", CSS)
        self.assertIn("Lateral", CSS)
        self.assertIn("Anton", CSS)


class InteractionTests(unittest.TestCase):
    def test_focus_ring_is_an_outline_not_a_shadow(self):
        block = re.search(r":focus-visible\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(block)
        self.assertIn("outline", block.group(1))
        self.assertNotIn("box-shadow", block.group(1))

    def test_reduced_motion_is_handled(self):
        self.assertIn("prefers-reduced-motion", CSS)
        block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", CSS, re.S)
        self.assertIsNotNone(block)
        self.assertIn("animation: none", block.group(1))

    def test_browser_surfaces_are_themed(self):
        for surface in ("::selection", "::-webkit-scrollbar", "caret-color"):
            self.assertIn(surface, CSS, surface)

    def test_status_is_never_colour_alone(self):
        """The reference bans green as a success state, so every pill has a word."""
        for page in ("static/dashboard.js", "static/admin.js"):
            js = (ROOT / page).read_text()
            self.assertIn("label", js, page)
        self.assertRegex(CSS, r"\.pill-state[^{]*\{[^}]*\}")

    def test_placeholder_text_meets_contrast(self):
        block = re.search(r"\.field::placeholder\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(block)
        value = re.search(r"color:\s*(#[0-9a-f]{6})", block.group(1))
        self.assertIsNotNone(value)
        # 4.5:1 against white.
        self.assertGreaterEqual(relative_luminance(value.group(1)), 4.5)


class CspTests(unittest.TestCase):
    def test_policy_has_no_unsafe_inline(self):
        self.assertIn("Content-Security-Policy", SERVER)
        self.assertNotIn("unsafe-inline", SERVER)
        self.assertNotIn("unsafe-eval", SERVER)

    def test_policy_allows_only_self(self):
        policy = re.search(r'"default-src (.*?); ', SERVER).group(1)
        self.assertEqual(policy, "'self'")

    def test_pages_carry_no_inline_script_or_style(self):
        """CSP is script-src 'self' with no unsafe-inline, so this must hold."""
        for page in sorted((ROOT / "static").glob("*.html")):
            html = page.read_text()
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)",
                                "%s has an inline script" % page.name)
            self.assertNotRegex(html, r"\sstyle=", "%s has an inline style" % page.name)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""),
                             "%s reaches out over the network" % page.name)


def relative_luminance(hex_colour: str) -> float:
    """WCAG contrast of `hex_colour` against white."""
    hex_colour = hex_colour.lstrip("#")
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 1.05 / (luminance + 0.05)


if __name__ == "__main__":
    unittest.main()
