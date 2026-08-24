// The public origin, in one place: metadataBase, the sitemap, robots.txt and the JSON-LD all read
// it, and a wrong value there silently breaks every absolute URL Google and Slack resolve.
export const SITE_URL = "https://tracely-ai.com";
export const DOCS_URL = "https://doc.tracely-ai.com";
export const GITHUB_URL = "https://github.com/Jwuthri/Tracely-ai";

/** The homepage's search-facing title. Leads with what people type, not with the positioning line. */
export const SITE_TITLE = "Tracely — LLM Observability & AI Agent Evaluation in CI";

/** Under 155 chars so Google shows it whole instead of truncating mid-sentence. */
export const SITE_DESCRIPTION =
  "Trace your AI agents, grade every run, and turn production failures into regression tests that block the pull request which would ship them again.";
