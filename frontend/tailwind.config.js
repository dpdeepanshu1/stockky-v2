/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      // 2026-09-03 Groww-style UI upgrade: token NAMES kept identical
      // (ink/graphite/slate/mist/paper) so every existing className in the
      // 27 components keeps working. Values now point at the CSS variables
      // in index.css instead of fixed hex, so bg-ink/text-paper/etc. stay
      // correctly theme-aware — the existing dark/light toggle (html.light)
      // keeps working exactly as before, it just now switches into the new
      // Groww-styled light palette instead of the old one. Semantic note:
      // "ink"/"graphite" were originally near-black backgrounds, now
      // resolve to whichever --bg/--panel is active per theme; "paper" was
      // light text-on-dark, now resolves to --fg (dark text in light mode).
      colors: {
        ink: "var(--bg)",
        graphite: "var(--panel)",
        slate: "var(--border)",
        mist: "var(--muted)",
        paper: "var(--fg)",
        amber: {
          terminal: "var(--buy)", // brand accent, repurposed from amber to brand green
          dim: "var(--amber)",
        },
        signal: {
          buy: "#00b386",
          prepare: "#3968ef",
          hold: "#ed7a1a",
          avoid: "#93a0a8",
          sell: "#ff5b52",
        },
      },
      fontFamily: {
        display: ["'DM Sans'", "'Inter'", "system-ui", "sans-serif"],
        mono: ["'Inter'", "ui-monospace", "monospace"],
        body: ["'DM Sans'", "'Inter'", "system-ui", "sans-serif"],
      },
      boxShadow: {
        "glow-sm": "0 1px 2px rgba(16,24,32,0.04)",
        glow: "0 8px 24px rgba(16,24,32,0.08)",
        panel: "0 1px 2px rgba(16,24,32,0.04), 0 8px 24px rgba(16,24,32,0.06)",
      },
      borderRadius: {
        card: "16px",
      },
      letterSpacing: {
        terminal: "0.08em",
      },
    },
  },
  plugins: [],
};
