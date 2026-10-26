/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "Noto Sans", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      colors: {
        border: "#e6e6e3",
        borderSoft: "#eeeeeb",
        text: "#242423",
        textDim: "#90908b",
        textMid: "#676762",
        accent: "#c96b4d",
        accentWeak: "#f7e9e3",
        green: "#4d996b",
        greenWeak: "#eaf5ed",
        red: "#c85c58",
        redWeak: "#fcedec",
        yellow: "#a47c18",
        blue: "#4f79b6",
        purple: "#8060b2",
      },
      radius: {
        px: "9px",
      },
    },
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/typography"),
    require("@tailwindcss/line-clamp"),
  ],
}