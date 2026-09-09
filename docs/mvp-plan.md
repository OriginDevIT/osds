# OSDS — MVP build plan

**Target:** a publishable release that hosts one or more tenant directory
sites, takes money, and can be installed by someone who rents a server.

Written 2026-09-05, after the Django reset. Supersedes the task ordering in
earlier session notes.

---

## What "publishable" means here

An operator runs `docker compose up`, opens a browser, and is walked through
creating their first directory. They point a domain at it, import several
hundred listings from CSV, and have a searchable public site with images. A
business owner finds their listing, claims it by email, and receives leads.
The operator sells that owner an upgraded tier and gets paid. Nothing in that
sentence requires editing a config file or reading documentation.

Everything else is a later release.

---

## Scope

### In

- First-run wizard: superadmin, first tenant, domain with DNS guidance and
  verification, storage backend, SMTP, enabled claim methods
- Multi-tenant with per-tenant users and data isolation
- Operators, staff memberships, five role ranks, superadmin
- Console host with directory picker; tenant admin at `/admin` on the tenant
  domain
- Listing types with per-type field schemas — businesses, locations, people,
  services, software and anything else a tenant defines
- Categories per type, listing CRUD, publish and unpublish
- Public site: home, category browse, listing detail, search
- Postgres full-text plus trigram search; `lat`/`lon` radius search
- Media upload with per-tenant storage — local disk at launch. S3, Azure and
  GCP are wizard options that raise a deferred-feature error; each needs a
  human-gated SDK (#150)
- CSV import, worker-processed, with batch rollback
- Claims: manual review and email OTP, consent capture, anti-hijack
  notification, dispute queue
- Lead capture with consent
- Tiers, entitlements, comps, Stripe checkout, dunning and grace
- Outbox and worker, with SMTP and webhook adapters
- Command log and access log
- Sitemap index, `robots.txt`

### Out, and planned

Featured slots — **first post-launch addition**. Native reviews and the
moderation queue beyond claim disputes. Phone OTP, postcard and Google Business
Profile verification. AI agents. External review display. Postgres RLS as
defence in depth. Devcontainer.

None of these may be designed out. The slot model, the verification method
table, the agent scope model and the review events are all specified; the
schema and service boundaries must leave room for them.

---

## Order

Each block ends at a mergeable state. Later blocks assume earlier ones.

### 1 — Foundation

Project scaffold, settings from environment, compose file. Models in one migration, per app: `tenants` — `Tenant`, `Operator`,
`StaffMembership`, `OperatorInvite`, `InstallSetup`, `Secret`. `directory` —
`ListingType`, `Category`, `Listing`, `DirectoryUser`, `Claim`, `Lead`,
`Consent`, `ConsentText`, `SuppressionKey`, `ImportBatch`, `PathRedirect`,
`SearchReindexJob`. `billing` — `Tier`, `Entitlement`. `audit` —
`OutboxEvent`, `CommandLog`, `AccessLog`.

Tenant resolution from the Host header. Scoped default manager on every
tenant-scoped model, plus the test asserting each one has it. Event-name
constants module and its test.

First-run wizard: setup token to logs, superadmin, first tenant, domain with
the DNS record shown and a verification check, storage selection, SMTP.

**Done when** a fresh stack walks an operator to a logged-in admin. Met
2026-09-07 — the wizard shipped in block 1 but the login form it hands off to
did not land until #127, so this criterion was unmet for two sessions while
block 1 was marked complete.

### 2 — Directory

Listing type configuration with the field schema. Category tree per type.
Listing CRUD through the service layer, emitting to the outbox. Media upload
against the per-tenant storage backend.

Public site: home, category, listing detail, search. Full-text plus trigram,
radius filter. Sitemap index and `robots.txt`.

**Done when** an operator creates a directory type, adds listings with images,
and the public site renders and searches them. Met 2026-09-08 across six PRs,
closing with the sitemap index and `robots.txt`.

### 3 — Worker, population and claims

Worker first (#171). `run_worker`, the tick loop, and the outbox drain with
retry and dead-letter. Pulled forward from block 5: CSV import is specified as
worker-processed, and a several-hundred-row import cannot run in a request.
The SMTP and webhook adapters stay in block 5 — the worker runs with an empty
adapter registry and drains to nothing, which is all import needs from it.

CSV import: upload stores the file and creates a batch, the worker processes
it, the admin page polls status. Column mapping, suppression-key check, batch
rollback.

Claims: submission with consent, email OTP, manual review with mandatory notes,
existing-contact notification on approval, dispute to queue. Lead capture with
consent. Owner dashboard — enough to see leads and edit the listing.

**Done when** several hundred listings import cleanly on the worker and an
owner claims one and receives a lead.

### 4 — Money

Tier configuration per tenant. Entitlement records with status and period end.
Comps. Stripe adapter reporting payment outcomes; core resolving tier. Dunning
at 14 days with full perks retained, grace at 30 days downgraded, expiry to
rank 0 without unpublishing. Scheduled transitions on the worker tick.

Every state transition in the table gets a test.

**Done when** an operator sells an upgrade, the badge appears, a declined card
runs the full dunning and grace path, and the listing lands on rank 0 still
published.

### 5 — Integration and release

SMTP and webhook adapters. Scheduled tick jobs registered — payload nulling at
90 days, sitemap regeneration (#159), `SearchReindexJob` drain (#132). Command
log and access log wired to the service layer. Adapter documentation. README,
compose file, release tag.

The drain and the tick loop themselves moved to block 3 (#171).

---

## Standing risks

**App-level tenancy is the only isolation.** One unscoped query is a
cross-tenant leak. The manager test is a floor, not a ceiling; RLS is planned.

**The entitlement state table is where this rots.** Every transition needs a
test from the day it exists, not after.

**A recorded migration is never edited.** CI migrates from zero and cannot see
the drift. A correction is a new idempotent forward migration.

**Unit tests that mock the database will not catch a missing table.** Every
block ends with a browser pass, not a green suite.
