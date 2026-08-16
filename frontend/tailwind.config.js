/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0E1512",
        graphite: "#161D1A",
        slate: "#1F2A26",
        mist: "#8FA39C",
        paper: "#EDEFEC",
        signal: {
          buy: "#3FD97F",
          prepare: "#4FB8E0",
          hold: "#E0B84F",
          avoid: "#5C6864",
          sell: "#E05C4F",
        },
      },
      fontFamily: {
        display: ["'Fraunces'", "serif"],
        mono: ["'IBM Plex Mono'", "monospace"],
        body: ["'Inter'", "sans-serif"],
      },
      boxShadow: {
        'glow-sm': '0 0 12px rgba(79, 184, 224, 0.15)',
        'glow': '0 0 20px rgba(79, 184, 224, 0.25)',
      },
    },
  },
  plugins: [],
};