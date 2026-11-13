# DESIGN.md — Speexx Panel

Written from the built world on 2026-10-06, when the panel became multi-user. Source of truth is `web/tailwind.css`; the
rules below are asserted by `tests/test_design.py`, so a violation fails the suite rather
than waiting to be noticed.

## World

The supplied style reference — "Slush", an inflatable sticker universe on pastel paper —
is the visual authority, transcribed into CSS custom properties. It is pinned, so it wins
wherever it conflicts with a general design preference.

- **Ground:** pastel paper in three full-bleed bands, `#dceeff` → `#ffffff` →
  `#cccccc`, alternating down the page. Sections are defined purely by colour. No
  dividers, no shadows.
- **Signature motif:** one inflatable electric-blue tube, drawn as inline SVG from four
  layered flat blues (`#000` outline, `#2f7fd1` underside, `#4da2ff` body, `#6bb6ff` and
  `#a8d8ff` highlights) with an `feTurbulence` + `feDisplacementMap` filter roughening the
  edge. A filter, not a gradient — gradients are banned outright. It is confined to the
  block that holds the headline it wraps behind, so it can never sit under body copy.
- **Stickers:** hand-drawn SVG symbols (rocket, coin, wallet, check, cross, bolt, lock,
  flag, key) with flat fills and a consistent 2.5px black outline, placed at rotations and
  allowed to overlap the display type. Never grid-aligned.
- **Outlines:** 1px solid `#000` on every interactive element and every card. This is the
  hand-cut sticker edge, and it is the only elevation device in the system.

## Type

| Role | Family | Treatment |
|---|---|---|
| Display | **Anton** (self-hosted) | native weight 400, `line-height` ≤ 0.85 (0.80 / 0.75 / 0.82), sized with `clamp()` |
| UI | **Inter** (self-hosted) | 500 body at 15px/1.39, 700 nav and buttons at `0.032em` tracking, 12px uppercase labels |

`Lateral` and `Aeonik Pro` are commercial and unavailable. `--font-lateral` keeps the real
name with Anton as its first available face, so licensing Lateral is a one-line change.
Anton is single-weight 400; declaring `800` would make the browser synthesise a smear, so
the token `--font-weight-extrabold: 800` is kept for the licensed face and Anton renders
at its native weight. `Bowlby One` and `Antonio` — the reference's other listed
substitutes — are also 400-only, which is why they were not used.

Display sizes are `clamp(token, vw, token)`: the reference's 200px/640px are the cap, not
a fixed size, because those are unusable at laptop width. Body copy never drops below
15px.

## Colour rules

- **Text is `#000` on every surface except Carbon and Voltage Violet**, where it is
  `#fff`. Checked per pair: Ember, Sunburst, Mint and Electric Blue are all light enough
  that white text would fail contrast, so they take black.
- **`#4da2ff` is never an action colour.** It is the ribbon and a decorative wash. Every
  CTA is black-filled or outlined-black. Links are black.
- **Mint `#55db9c` is never a success state.** Job and account status is carried by a sticker
  *dot plus a word* — running = blue dot + "Running", done = sunburst + "Done", failed =
  ember + "Failed", pending = sunburst + "Pending", disabled = ember + "Disabled".
  Colour is never the only signal, which also satisfies accessibility.
- Placeholder text is `#767676` on white, 4.6:1 — a tinted neutral, not pure gray.

## Shape

Nav, buttons and pills are `1600px`. Cards are `20px`, elevated cards `40px`, the body
radius `30px`. Nothing on a card or button is below `16px`. No `box-shadow` anywhere;
Tailwind's preflight `box-shadow: none` is left in place because it *removes* inherited
shadows.

## Components

| Component | Treatment |
|---|---|
| Marquee | full-bleed black band, white 12px uppercase at `0.032em`, flush to the edges, duplicated track for a seamless `-50%` loop |
| Nav pill | `1600px`, 1px black, white fill, 700 at `0.032em`; current page inverts to black |
| Filled CTA | 40px radius, black fill, white text — the Don'ts win over the contradictory example in the reference |
| Outlined ghost | `1600px`, 1px black, white fill |
| Field | white, 1px black, `20px` radius. Password fields are monospace so the character count is legible while typing |
| Status pill | `1600px`, sticker dot + text label |
| Log | Carbon background, mono, green/amber/sunburst for `LOGIN_OK` / `LOGIN_FAILED` / `JOB_DONE` |
| Violet card | the reference's violet split-panel treatment: violet field, white inset, headline at display size — used for "you keep the key" and for the save-account form |
| Tile | the stat block: label in 12px uppercase, value in the display face, one line of plain support text |
| Chip | identity pill (the signed-in address, an `admin` tag), `#55db9c` fill, 1px black |
| Shell | the shared page frame: marquee, nav, banded main, footer — every page uses it |

## Motion

The marquee scrolls and nothing else moves. Buttons translate 2px on press. No entrance
animation, no hover theatrics — the reference describes a printed collage, and motion is
restricted to the marquee plus minimal hover transitions.
`prefers-reduced-motion: reduce` stops the marquee and disables the press transform.

## Browser surfaces

Themed rather than left at defaults, because they are the cheapest signal that a page was
built rather than assembled: selection is Sunburst on Carbon, scrollbars are Carbon
thumbnails on a Soft Mist track, the caret is Ember, focus is a `3px` black
`outline` with `3px` offset (never a shadow), and every changing number uses
`font-variant-numeric: tabular-nums`.

## Layout

Full-bleed bands, no sidebar, no container. The nav and marquee sit at the top; each band
is self-contained. Placement is deliberately asymmetric — the landing page puts its violet
card beside the headline rather than below it, and the three-step band staggers its columns
downward. Below 1280px the nav pills wrap and the display type scales; the page never
overflows horizontally (`overflow-x: clip` on both roots).

The signed-in pages are the same frame with an app shell: a `.shell` container, a
`.nav-shell` nav, and `.tile` blocks in a responsive grid. On mobile the stat tiles stack
one per row and the page label collapses out of the nav, leaving the identity chip.

## Fidelity gaps, stated

1. **The ribbon is flat SVG, not a CGI render.** The reference's inflatable tubes have
   specular highlights and photographic grain; layered flat blues plus a displacement
   filter read as an illustrated inflated ribbon. This is the largest visual difference.
2. **Type is Anton, not Lateral.** Composition, weight impression and crushed leading
   match; the letterforms do not.
3. **It is a tool, not a poster.** The reference's layout section describes full-viewport
   poster bands and a QR download card. The visual language is kept intact, but the
   information architecture is a form, a status block and a log, because it has to be
   usable. The violet split-panel treatment is reused for the "you keep the key" card in
   place of the QR card.
4. **A display face is for digits.** `.display-sm` on a *word* ("By approval" on the admin
   sign-up tile) wrapped outside its tile, so a stat tile picks `.display-xs` for words and
   `.display-sm` for numbers. **A word in the display face is a bug.**
