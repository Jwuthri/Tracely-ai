import { useRouter } from "next/router";
import { useConfig } from "nextra-theme-docs";

const REPO = "https://github.com/Jwuthri/Tracely-ai";
const SITE = "https://tracely-ai.com";
const DOCS = "https://doc.tracely-ai.com";

// The app's sidebar mark (frontend/app/components/Sidebar.tsx), reused so docs and product share a face.
const Logo = () => (
  <span className="tracely-logo">
    <span className="mark">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path d="M12 2 22 12 12 22 2 12Z" stroke="#22d3ee" strokeWidth="1.8" strokeLinejoin="round" />
        <circle cx="12" cy="12" r="2.7" fill="#22d3ee" />
      </svg>
    </span>
    <span>
      <span className="name">Tracely</span>
      <span className="kicker" style={{ display: "block" }}>
        sdk &amp; cli
      </span>
    </span>
  </span>
);

const config = {
  logo: <Logo />,
  logoLink: "/",
  project: { link: REPO },
  docsRepositoryBase: `${REPO}/tree/master/docs`,
  color: { hue: 187, saturation: 85, lightness: 53 }, // #22d3ee — the app's `signal`
  backgroundColor: { dark: "9,11,16" }, // ink #090b10
  // The product is dark-only; a light docs theme would be the odd one out.
  darkMode: false,
  nextThemes: { defaultTheme: "dark", forcedTheme: "dark" },
  navigation: { prev: true, next: true },
  // Docs sit on their own subdomain, so "back to the product" has to be a link — there is no
  // shared shell to climb out through. The footer already carries one; this is the visible one.
  navbar: {
    extraContent: (
      <a href={SITE} className="tracely-site-link">
        Product ↗
      </a>
    ),
  },
  footer: {
    content: (
      <span style={{ fontSize: 13 }}>
        {/* Links back to the marketing origin on every page: docs pages are the ones that earn
            inbound links, and this is how that authority reaches tracely-ai.com. */}
        <a href={SITE}>Tracely</a> — trace-native CI/CD for AI agents · the recorded run{" "}
        <em>is</em> the test.
      </span>
    ),
  },
  // Overriding `head` REPLACES Nextra's default, which is the only thing that emits <title> and
  // <meta description> — the previous static fragment silently shipped every docs page with no
  // title at all. Anything set here must therefore cover those too.
  head: function Head() {
    const { asPath } = useRouter();
    const { title: pageTitle, frontMatter } = useConfig();
    // Docs live on their own subdomain, so each page needs its own self-canonical — otherwise
    // /cli, /cli/ and /cli?x= all compete with each other as separate results.
    const url = `${DOCS}${asPath === "/" ? "" : asPath.split("?")[0].split("#")[0]}`;
    // The landing page's own H1 is already "Tracely SDK", so suffixing it would read
    // "Tracely SDK — Tracely SDK docs". Give the root a search-facing title instead.
    const title =
      !pageTitle || pageTitle.includes("Tracely")
        ? "Tracely SDK docs — OpenTelemetry tracing for AI agents"
        : `${pageTitle} — Tracely SDK docs`;
    const description =
      frontMatter.description ||
      "Instrument your AI agents and ship their traces to Tracely over OTLP.";
    return (
      <>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <meta name="theme-color" content="#090b10" />
        <title>{title}</title>
        <meta name="description" content={description} />
        <link rel="canonical" href={url} />
        <meta property="og:url" content={url} />
        <meta property="og:site_name" content="Tracely" />
        <meta property="og:title" content={title} />
        <meta property="og:description" content={description} />
        <meta name="twitter:card" content="summary_large_image" />
      </>
    );
  },
  sidebar: { defaultMenuCollapseLevel: 1, toggleButton: true },
  toc: { backToTop: true, title: "On this page" },
  editLink: { content: "Edit this page on GitHub" },
  feedback: { content: "Question? Give us feedback", labels: "documentation" },
};

export default config;
