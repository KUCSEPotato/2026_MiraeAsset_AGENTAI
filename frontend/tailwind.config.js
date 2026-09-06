/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,jsx}", "./components/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        finory: {
          bg: "#fcfbf9",
          panel: "#ffffff",
          soft: "#fff6ee",
          text: "#22221f",
          muted: "#6f706b",
          line: "#eee7df",
          accent: "#f58220",
          accentDark: "#df6e12"
        }
      },
      boxShadow: {
        finory: "0 14px 34px rgba(42, 35, 28, 0.08)",
        composer: "0 12px 36px rgba(60, 45, 35, 0.06)"
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Noto Sans KR",
          "sans-serif"
        ]
      }
    }
  },
  plugins: []
};
