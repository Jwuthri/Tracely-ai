import type { Metadata } from "next";
import localFont from "next/font/local";

import "./globals.css";
import { AuthRootProvider } from "./_providers/AuthRootProvider";
import { PostHogProvider } from "./_providers/PostHogProvider";
import { SITE_DESCRIPTION, SITE_TITLE, SITE_URL } from "./lib/site";

// Site-wide defaults. `metadataBase` is what turns the relative OG/canonical paths every other
// route emits into the absolute URLs Google and Slack require — without it Next warns and falls
// back to localhost. Individual routes override `title`/`description`; the (app), (auth) and
// /share routes additionally override `robots` to noindex.
// ponytail: no `keywords` meta — Google has ignored it since 2009, and Bing treats it as spam.
export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: SITE_TITLE, template: "%s — Tracely" },
  description: SITE_DESCRIPTION,
  applicationName: "Tracely",
  openGraph: {
    type: "website",
    siteName: "Tracely",
    url: SITE_URL,
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
  twitter: { card: "summary_large_image", title: SITE_TITLE, description: SITE_DESCRIPTION },
  robots: { index: true, follow: true },
};

// Self-hosted variable woff2 files (latin subset, from @fontsource-variable) served from our own
// origin: no third-party round-trip at runtime, and — unlike next/font/google — no fetch to
// fonts.googleapis.com at BUILD time, which fails on Railway's builder ("Cannot read properties of
// null"). The `variable` names must match the CSS custom properties tailwind.config.ts reads.
const fontSans = localFont({
  src: "./fonts/hanken-grotesk-latin-wght-normal.woff2",
  weight: "100 900",
  variable: "--font-sans",
  display: "swap",
});
const fontDisplay = localFont({
  src: "./fonts/bricolage-grotesque-latin-wght-normal.woff2",
  weight: "200 800",
  variable: "--font-display",
  display: "swap",
});
const fontMono = localFont({
  src: "./fonts/jetbrains-mono-latin-wght-normal.woff2",
  weight: "100 800",
  variable: "--font-mono",
  display: "swap",
});

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${fontSans.variable} ${fontDisplay.variable} ${fontMono.variable}`}
      // The theme script below writes data-theme onto this very element before React hydrates,
      // so the server HTML and the client DOM legitimately differ here. suppressHydrationWarning
      // is one level deep — it silences the <html> tag's own attributes, nothing inside.
      suppressHydrationWarning
    >
      <head>
        {/* Paint the stored theme onto <html> BEFORE the first paint. A React effect runs after
            hydration, which on a light-theme reload means a full dark page flashes first. Dark is
            the default, so no stored preference = no work. Keep the key in sync with
            components/ThemeToggle.tsx. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(localStorage.getItem("tracely-theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}`,
          }}
        />
      </head>
      <body className="min-h-screen bg-ink font-sans text-fg antialiased">
        {/* The dashboard shell (sidebar/topbar) lives in the (app) route group; (auth) pages render bare. */}
        <PostHogProvider>
          <AuthRootProvider>{children}</AuthRootProvider>
        </PostHogProvider>
      </body>
    </html>
  );
}
