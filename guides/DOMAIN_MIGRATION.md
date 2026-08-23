# Domain migration — `tracely-studio.xyz` → `tracely-ai.com`

**Date:** 2026-08-22 · **Reason:** `.xyz` ranks poorly; `tracely-ai.com` matches the PyPI package
name (`tracely-ai`) and is the domain we want to accrue authority on.

The old domain is **not** retired. It stays registered, stays attached to the same Railway services,
and 308-redirects every path to the new one — that redirect is the only thing that carries the
existing backlinks' authority across. Detaching it would throw them away.

---

## 1. Hostname map

| Old | New | Railway service | Port | Behaviour on the old host |
|---|---|---|---|---|
| `tracely-studio.xyz` | `tracely-ai.com` | `frontend` | 8080 | 308 → same path on the new host |
| — | `www.tracely-ai.com` | `frontend` | 8080 | 308 → apex (`tracely-ai.com`) |
| `doc.tracely-studio.xyz` | `doc.tracely-ai.com` | `Doc` | 8080 | 308 → same path on the new host |
| `api.tracely-studio.xyz` | `api.tracely-ai.com` | `api` | 8000 | **serves normally — no redirect** |

The API host is deliberately *not* redirected. SDKs, OTLP exporters and the MCP client post to it,
and not every HTTP client follows a redirect on a POST. Both hostnames point at the same service and
both stay live indefinitely; there is no SEO reason to move an API host.

`tracely-ai.com` was bought through Railway, so Railway's registrar (name.com) created the CNAME /
apex-ALIAS records automatically when each domain was attached. Nothing to configure by hand.

## 2. The redirect

No proxy service, no extra deploy target: the old hostnames are still attached to the services that
already serve them, and each Next app 308s anything arriving on an old host.

- [`frontend/next.config.mjs`](../frontend/next.config.mjs) — `OLD_HOSTS` → `https://tracely-ai.com/:path*`
- [`docs/next.config.mjs`](../docs/next.config.mjs) — `doc.tracely-studio.xyz` → `https://doc.tracely-ai.com/:path*`

Two things make this work and are easy to break:

1. **`redirects()` runs before middleware** in Next's routing order, so a dashboard URL on the old
   host is redirected instead of being bounced to `/login` by the auth guard first.
2. **308, not 302** (`permanent: true`). Google treats 308 like 301 and passes link equity; a 302
   passes none. 308 also preserves the request method, unlike 301.

Session cookies are host-scoped, so anyone who was signed in on `tracely-studio.xyz` lands on the
new domain signed out and logs in once. Expected, not a bug.

## 3. Railway state

Custom domains added (production environment, project `tracely`):

```
frontend  a57c06c3-…  tracely-ai.com:8080, www.tracely-ai.com:8080   (+ tracely-studio.xyz:8080 kept)
api       c239e89a-…  api.tracely-ai.com:8000                        (+ api.tracely-studio.xyz:8000 kept)
Doc       03008626-…  doc.tracely-ai.com:8080                        (+ doc.tracely-studio.xyz:8080 kept)
```

Variables changed:

| Service | Variable | New value |
|---|---|---|
| `api` | `APP_BASE_URL` | `https://tracely-ai.com` — invite / reset-password / alert / Stripe-return links |
| `api` | `FRONTEND_ORIGIN` | `https://tracely-ai.com` — the single CORS allow-origin (`api/main.py`) |
| `frontend` | `NEXT_PUBLIC_API_URL` | `https://api.tracely-ai.com` |
| `frontend` | `NEXT_PUBLIC_TRACELY_PUBLIC_API` | `https://api.tracely-ai.com` |

`TRACELY_API` stays `http://api.railway.internal:8000` — server-side calls never leave the private
network. `CORS_ORIGINS` exists on both services but is read by nothing in the repo; it is dead.

## 4. Code changed

Every occurrence of the old domain in the repo was rewritten. Notable ones:

| File | What |
|---|---|
| `frontend/app/lib/site.ts` | `SITE_URL` / `DOCS_URL` — feeds `metadataBase`, `sitemap.ts`, `robots.ts`, JSON-LD |
| `frontend/app/(marketing)/Landing.tsx` | `DOCS`, `API` constants shown in the copy-paste snippets |
| `frontend/app/(marketing)/agent-skill/page.tsx` | the `claude mcp add` command |
| `frontend/app/(app)/settings/billing/page.tsx` | hosted-plan link |
| `docs/theme.config.jsx` | `SITE` / `DOCS` — docs canonical + OG tags + the "Product ↗" pill |
| `docs/pages/*.mdx` | MCP endpoint, hosted API host, cross-links to marketing pages |
| `skills/tracely/**` | the agent skill's hosted endpoint + docs links |
| `sdk/pyproject.toml` | `Homepage` / `Documentation` (PyPI sidebar) |
| `server.json` | MCP registry entry: `websiteUrl` + the remote `/mcp` URL |
| `README.md`, `design/SEO.md`, `design/GROWTH.md` | all links |

## 5. Manual follow-ups (need accounts I don't have)

- [ ] **Turn auto-renew ON for `tracely-studio.xyz`** (Railway → Domains). It expires **2027-07-27**
      with auto-renew off. The day it lapses, every inbound backlink 404s and the migration's whole
      point is lost. Keep it for at least a year after the move.
- [ ] **Google Search Console** — add `tracely-ai.com` as a property, verify it, then run
      *Settings → Change of address* from the `tracely-studio.xyz` property. That is the signal that
      makes Google transfer rankings quickly instead of re-discovering the site. Requires both
      properties verified and the 308 live. Resubmit `https://tracely-ai.com/sitemap.xml`.
- [x] **Resend** — `tracely-ai.com` verified (DKIM/SPF live); `EMAIL_FROM` on the `api` service is
      now `Tracely <support@tracely-ai.com>` (2026-08-22).
- [ ] **PyPI** — the new `Homepage`/`Documentation` only appear after the next `tracely-ai` release.
- [ ] **MCP registry** — republish `server.json` so the listed remote URL is the new one.
- [ ] **The 2–3 existing backlinks** — the 308 is enough for SEO, but ask the linking sites to point
      at `tracely-ai.com` directly where it's cheap. A direct link is worth more than a redirected one.
- [ ] **Anywhere off-repo the old URL is pasted** — GitHub repo homepage field, PyPI project page,
      LinkedIn, Product Hunt, X bio, any launch posts.

## 6. Verify

```bash
curl -sI https://tracely-studio.xyz/llm-evaluation | head -3        # 308 → https://tracely-ai.com/llm-evaluation
curl -sI https://doc.tracely-studio.xyz/mcp | head -3               # 308 → https://doc.tracely-ai.com/mcp
curl -s https://tracely-ai.com/robots.txt                           # sitemap + host must say tracely-ai.com
curl -s https://api.tracely-ai.com/health                           # 200, no redirect
curl -s https://api.tracely-studio.xyz/health                       # 200, still no redirect
```
