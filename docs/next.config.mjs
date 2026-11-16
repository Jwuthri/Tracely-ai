import nextra from "nextra";

const withNextra = nextra({
  theme: "nextra-theme-docs",
  themeConfig: "./theme.config.jsx",
  defaultShowCopyCode: true,
});

// doc.tracely-studio.xyz stays attached to this service and 308s here — see frontend/next.config.mjs
// for why the old domain is kept alive rather than detached.
export default withNextra({
  reactStrictMode: true,
  async redirects() {
    return [
      {
        source: "/:path*",
        has: [{ type: "host", value: "doc.tracely-studio.xyz" }],
        destination: "https://doc.tracely-ai.com/:path*",
        permanent: true,
      },
      // The Dashboard page is gone — its headline numbers live on Trends; keep inbound links alive.
      { source: "/product/dashboard", destination: "/product/trends", permanent: true },
    ];
  },
});
