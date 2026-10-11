/** @type {import('tailwindcss').Config} */
// UI refresh (2026-10-11): every themed colour is now an RGB-channel CSS
// variable (see :root / html.light in src/index.css) wrapped with
// <alpha-value>. Before this, colours were plain `var(--x)` strings, which
// Tailwind cannot apply an opacity modifier to — so ~600 classes such as
// `text-mist/60`, `border-slate/40`, `bg-ink/40` were silently never
// generated and those elements fell back to inherited/default styling.
// Token NAMES (ink/graphite/slate/mist/paper, signal.*) are unchanged, so
// no component className needed to change.
const c = (name) => `rgb(var(--${name}-rgb) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: c("bg"),          // app background
        graphite: c("panel"),  // cards / panels
        slate: c("border"),    // hairlines / borders
        mist: c("muted"),      // secondary text
        paper: c("fg"),        // primary text
        amber: {
          terminal: c("accent"),
          dim: c("warn"),
        },
        signal: {
          buy: c("buy"),
          prepare: c("accent"), // interactive / informational blue
          hold: c("warn"),
          avoid: c("muted-2"),
          sell: c("sell"),
        },
      },
      fontFamily: {
        display: ["'Inter'", "system-ui", "sans-serif"],
        mono: ["'Inter'", "system-ui", "sans-serif"],
        body: ["'Inter'", "system-ui", "sans-serif"],
        code: ["'JetBrains Mono'", "ui-monospace", "monospace"],
      },
      boxShadow: {
        "glow-sm": "0 1px 2px rgb(var(--shadow-rgb) / 0.06)",
        glow: "0 8px 24px rgb(var(--shadow-rgb) / 0.10)",
        panel: "0 1px 2px rgb(var(--shadow-rgb) / 0.06), 0 6px 18px rgb(var(--shadow-rgb) / 0.06)",
      },
      borderRadius: {
        card: "14px",
      },
      letterSpacing: {
        terminal: "0.01em",
      },
    },
  },
  plugins: [],
};
