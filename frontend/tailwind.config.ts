import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
        display: ["var(--font-display)", "var(--font-sans)", "sans-serif"],
      },
      // Every colour is a CSS variable (space-separated RGB channels) so the whole palette can
      // be repainted by one [data-theme] block in globals.css. `<alpha-value>` keeps the slash
      // opacity modifiers working (bg-ink-800/70, border-signal/40, …).
      colors: {
        ink: {
          DEFAULT: "rgb(var(--c-ink) / <alpha-value>)",
          950: "rgb(var(--c-ink-950) / <alpha-value>)", // recessed: deepest in dark, greyest in light
          900: "rgb(var(--c-ink-900) / <alpha-value>)",
          800: "rgb(var(--c-ink-800) / <alpha-value>)", // the card surface
          700: "rgb(var(--c-ink-700) / <alpha-value>)",
          600: "rgb(var(--c-ink-600) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--c-line) / <alpha-value>)",
          soft: "rgb(var(--c-line-soft) / <alpha-value>)",
          bright: "rgb(var(--c-line-bright) / <alpha-value>)",
        },
        fg: {
          DEFAULT: "rgb(var(--c-fg) / <alpha-value>)",
          muted: "rgb(var(--c-fg-muted) / <alpha-value>)",
          faint: "rgb(var(--c-fg-faint) / <alpha-value>)",
        },
        signal: {
          DEFAULT: "rgb(var(--c-signal) / <alpha-value>)",
          soft: "rgb(var(--c-signal-soft) / <alpha-value>)",
          dim: "rgb(var(--c-signal-dim) / <alpha-value>)",
          deep: "rgb(var(--c-signal-deep) / <alpha-value>)",
        },
        ok: { DEFAULT: "rgb(var(--c-ok) / <alpha-value>)", dim: "rgb(var(--c-ok-dim) / <alpha-value>)" },
        fail: { DEFAULT: "rgb(var(--c-fail) / <alpha-value>)", dim: "rgb(var(--c-fail-dim) / <alpha-value>)" },
        warn: { DEFAULT: "rgb(var(--c-warn) / <alpha-value>)", dim: "rgb(var(--c-warn-dim) / <alpha-value>)" },
        info: { DEFAULT: "rgb(var(--c-info) / <alpha-value>)", dim: "rgb(var(--c-info-dim) / <alpha-value>)" },
        // The tint used for hairline highlights and glassy fills: white on a dark theme, ink on
        // a light one. `bg-white/[0.04]` would be invisible-then-wrong when the canvas flips.
        hilite: "rgb(var(--c-hilite) / <alpha-value>)",
        // span type accents
        t_agent: "rgb(var(--c-t-agent) / <alpha-value>)",
        t_llm: "rgb(var(--c-t-llm) / <alpha-value>)",
        t_tool: "rgb(var(--c-t-tool) / <alpha-value>)",
        t_retriever: "rgb(var(--c-t-retriever) / <alpha-value>)",
        t_think: "rgb(var(--c-t-think) / <alpha-value>)",
        t_delegate: "rgb(var(--c-t-delegate) / <alpha-value>)",
        t_skill: "rgb(var(--c-t-skill) / <alpha-value>)",
        t_step: "rgb(var(--c-t-step) / <alpha-value>)",
        // json syntax highlighting
        syn: {
          key: "rgb(var(--c-syn-key) / <alpha-value>)",
          str: "rgb(var(--c-syn-str) / <alpha-value>)",
          num: "rgb(var(--c-syn-num) / <alpha-value>)",
          bool: "rgb(var(--c-syn-bool) / <alpha-value>)",
        },
      },
      boxShadow: {
        glow: "var(--sh-glow)",
        panel: "var(--sh-panel)",
        frame: "var(--sh-frame)",
        pop: "var(--sh-pop)",
      },
      keyframes: {
        fadeup: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        grow: {
          "0%": { transform: "scaleX(0)" },
          "100%": { transform: "scaleX(1)" },
        },
        pulse2: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
        marquee: {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
      },
      animation: {
        // `backwards`, NOT `both`: `both` leaves the 100% keyframe applied for ever, and that
        // keyframe sets `transform: translateY(0)` — a transform, not `none`, so every .reveal
        // element stayed a permanent stacking context. That trapped any `z-50` popover inside it
        // (AgentPicker's listbox) and let the next `.reveal .card` sibling, itself a stacking
        // context via backdrop-blur, paint straight over it. `backwards` covers the stagger delay
        // with the 0% frame and then hands the element back to its natural styles — which the 100%
        // frame already matches, so nothing moves.
        fadeup: "fadeup 0.4s cubic-bezier(0.2,0.7,0.2,1) backwards",
        grow: "grow 0.5s cubic-bezier(0.2,0.7,0.2,1) both",
        pulse2: "pulse2 2s ease-in-out infinite",
        marquee: "marquee 36s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
