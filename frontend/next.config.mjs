/** @type {import('next').NextConfig} */
// NOTE: do NOT add an `env: {...}` block here. Next inlines `env` values into the *client* bundle, so
// TRACELY_KEY/TRACELY_API would leak to the browser. Server code reads them via process.env directly.

// The old .xyz domain stays attached to this same service so its backlinks keep resolving; every hit
// on it is 308'd (method-preserving 301) to the same path on tracely-ai.com, which is what passes the
// link equity on. `redirects()` runs BEFORE middleware in Next's routing order, so these never reach
// the auth wall. Keep `tracely-studio.xyz` renewing for as long as those backlinks matter.
// ponytail: a config redirect on the service that already owns the hostname — no proxy service.
const OLD_HOSTS = ["tracely-studio.xyz", "www.tracely-studio.xyz", "www.tracely-ai.com"];

const nextConfig = {
  // PostHog reverse proxy: the browser talks to OUR origin (/ingest), which ad blockers leave
  // alone; Next forwards to PostHog Cloud US. skipTrailingSlashRedirect keeps /ingest/e/ intact.
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return [
      { source: "/ingest/static/:path*", destination: "https://us-assets.i.posthog.com/static/:path*" },
      { source: "/ingest/:path*", destination: "https://us.i.posthog.com/:path*" },
    ];
  },
  async redirects() {
    return OLD_HOSTS.map((host) => ({
      source: "/:path*",
      has: [{ type: "host", value: host }],
      destination: "https://tracely-ai.com/:path*",
      permanent: true,
    }));
  },
};

export default nextConfig;
