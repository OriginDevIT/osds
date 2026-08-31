# @osds/web

Public site. Currently one route: a read-only listing page.

```
/{category}/{slug}    e.g. /plumbers/belmont-ave-plumbing
```

Server components only, no client JS.

## How it renders

1. **Tenant** - resolved from the `Host` header against `tenants.domain`, falling
   back to `OSDS_DEV_TENANT_SLUG` (for localhost). No match -> 404.
2. **Listing** - read in one transaction that sets `app.tenant_id` first, so RLS
   is enforced for the `osds_app` role (`DATABASE_URL`, never
   `DATABASE_URL_ADMIN`). Must be in `{category}` and `visibility = 'published'`,
   else 404.
3. **Tier badge** - `@osds/core`'s `resolvePublicRender` (spec §6.5) decides
   whether the badge shows for the listing's entitlement status. Tier display is
   not re-derived here.

Rendered fields: name, categories, address, phone, email, website, tier badge.

## Develop

Needs the dev database up, migrated and seeded, and the workspace packages built:

```bash
pnpm infra:up
pnpm migrate:dev
pnpm --filter @osds/db seed
pnpm --filter @osds/core build && pnpm --filter @osds/db build
pnpm --filter @osds/web dev        # http://localhost:3000/plumbers/belmont-ave-plumbing
```
