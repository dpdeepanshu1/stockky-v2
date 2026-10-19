# Group 464 — light-theme button contrast and colour fixes

Frontend only (rebuild the `frontend` image). New file `frontend/src/light-contrast.css`, imported last in `main.tsx`
(delete that one line to roll back). Everything is scoped to `html.light`; dark mode is untouched.

## Problems found (measured with WCAG contrast)
| Problem | Before | After |
|---|---|---|
| `text-white` on pale tinted buttons/cards (`bg-signal-*/10-30`) | ~1.1-1.5:1 (invisible) | dark ink of the same hue, >= 4.8:1 |
| Plain `text-white` headings / `hover:text-white` links on the page | ~1.1:1 | normal text colour |
| Coloured label on own tint (`text-signal-buy` on `bg-signal-buy/20`) | 3.1-4.1:1 | >= 4.8:1 |
| Primary button gradient end (white label) | 4.36:1, hover lightened to 3.8:1 | 4.95:1, hover darkens (5.6:1) |
| Disabled buttons (opacity .4-.5) | white-on-green ~1.4:1, washed out | flat grey, 5:1+ (tailwind buttons .7 opacity + desaturate) |
| `hover:opacity-90` on solid buttons | faded | darken instead |
| `bg-signal-hold/80`, `bg-red-600/90` solid buttons | 4.17 / 4.37:1 | solid fill, 6.2 / 6.5:1 |

Also added: visible keyboard focus ring on buttons in light mode, and a `prefers-contrast: more` border boost.

## Tests
`npm test` now also runs `src/lightContrast.test.mjs` (5 checks: import order, primary + hover contrast, ink-on-tint AA,
disabled readability, no stray `text-white`).

## Not done in this round
The cross-device alignment audit (phone / laptop / TV) from the unfinished session. It needs a full render of the app
(Tailwind 3 + bundler), which was not available here.
