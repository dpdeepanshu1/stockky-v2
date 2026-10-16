# UI refresh v3 (2026-10-16)

Presentation-only redesign. No API call, state, handler or business logic changed.

- `src/design-v3.css` (new) — the whole new visual layer; imported last in `src/main.tsx`.
  **Rollback:** delete that one import line.
- `index.html` — `viewport-fit=cover` (safe-area insets on notched phones), Plus Jakarta Sans for headings.
- `src/App.tsx` — mobile top bar shows brand mark + current page; drawer header gets the brand mark.
- `src/components/RealAutoTrade.tsx` — header, DEMO/REAL segmented switch, admin login card,
  session strip, inline re-login, Dhan connect form, StatCard / SectionHdr / GateStep markup (classes only).
- `src/components/trading/ManualTradeTicket.tsx` — wrapper/side-toggle classes, theme-safe colours.
- `src/components/ui/NavIcon.tsx` — added `lock` icon.

## Second pass
- `src/components/PositionStocksTab.tsx` — header, admin login card and session strip now match Auto Trade; pipeline stage cards no longer force a 150px minimum on narrow phones.
- `src/design-v3.css` §14 — all wide tables scroll inside their card on phones, bottom sheets respect the home indicator with larger close buttons, bigger touch controls, section-title accent, landscape-phone tab bar, 360px tweaks.

## All screen sizes
| Size | Behaviour |
|---|---|
| Phone < 768 | floating tab bar, drawer, 44px controls, safe-area aware |
| Tablet 768–1023 | 76px icon rail (tooltips via `title`), trimmed top bar |
| Laptop 1024–1439 | full sidebar |
| Desktop ≥ 1440 | wider content column, centred beside the sidebar |
| 4K ≥ 2200 | root font scales 112% → 125% → 156% |
| TV (`html.tv`) | 10-foot UI: 125–200% type, strong remote/D-pad focus ring, overscan-safe margins, no blur/animation, hidden scrollbars |

TV mode turns on automatically for Tizen, webOS, Android TV, Fire TV, Chromecast, etc.
Force it with `?tv=1` (off with `?tv=0`); the choice is remembered in localStorage (`stockky_tv`).
Code: `src/main.tsx` (detection) + `src/design-v3.css` §15.
