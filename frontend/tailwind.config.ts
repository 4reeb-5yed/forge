import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        forge: {
          bg: "#18181b",       // zinc-900
          card: "#27272a",     // zinc-800
          border: "#3f3f46",   // zinc-700
          text: "#fafafa",     // zinc-50
          muted: "#a1a1aa",    // zinc-400
          accent: "#3b82f6",   // blue-500
          success: "#22c55e",  // green-500
          warning: "#eab308",  // yellow-500
          error: "#ef4444",    // red-500
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
