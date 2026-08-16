/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#07090b",
        graphite: "#0e1216",
        slate: "#1a222c",
        mist: "#8b9aab",
        paper: "#e8edf2",
        amber: {
          terminal: "#f5a623",
          dim: "#c4841a",
        },
        signal: {
          buy: "#0ecb81",
          prepare: "#3b82f6",
          hold: "#f5a623",
          avoid: "#6b7280",
          sell: "#f6465d",
        },
      },
      fontFamily: {
        display: ["'Inter'", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "'IBM Plex Mono'", "ui-monospace", "monospace"],
        body: ["'Inter'", "system-ui", "sans-serif"],
      },
      boxShadow: {
        "glow-sm": "0 0 12px rgba(245, 166, 35, 0.12)",
        glow: "0 0 24px rgba(245, 166, 35, 0.18)",
        panel: "0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 32px rgba(0,0,0,0.35)",
      },
      letterSpacing: {
        terminal: "0.08em",
      },
    },
  },
  plugins: [],
};
