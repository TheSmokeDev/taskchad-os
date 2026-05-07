/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx,js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Theme tokens are CSS variables defined in src/styles/main.css —
        // tailwind classes use bracketed `bg-[var(--color-bg)]` etc.
      },
    },
  },
  plugins: [],
};
