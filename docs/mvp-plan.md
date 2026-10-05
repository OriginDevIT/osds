# OSDS — MVP build plan

**Target:** a publishable release that hosts one or more tenant directory
sites, takes money, and can be installed by someone who rents a server.

Written 2026-09-05, after the Django reset. Supersedes the task ordering in
earlier session notes.

**Status, 2026-10-04 (main at #262):** every block is built and merged. What
remains before the v0.1 alpha tag is a maintainer's: the browser pass each
block's "Done when" calls for, the manual Stripe test-mode run
(`docs/stripe-test-mode-checklist.md`), and the tag itself. Install
documentation is `docs/install.md`. The per-block status lines below cite the
merged PRs; where a "Done when" criterion has no recorded browser pass in the
repository, the status line says so instead of asserting it.

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

**Status: complete.** Models, tenancy and the event registry #118; tenant
resolution and the wizard #120; operator login and logout #147; completion
recorded in #148. Since then the wizard gained a skippable SMTP step (#229), and
the compose install with generated secrets landed in #227.

### 2 — Directory

Listing type configuration with the field schema. Category tree per type.
Listing CRUD through the service layer, emitting to the outbox. Media upload
against the per-tenant storage backend.

Public site: home, category, listing detail, search. Full-text plus trigram,
radius filter. Sitemap index and `robots.txt`.

**Done when** an operator creates a directory type, adds listings with images,
and the public site renders and searches them. Met 2026-09-08 across six PRs,
closing with the sitemap index and `robots.txt`.

**Status: complete.** Listing types and categories #124; listing CRUD through
the service layer #126; search vectors and reindex plumbing #134; the public
site #135; media upload #152; sitemap index and `robots.txt` #160; closed in
#161. Media is local disk only (#150): S3, Azure and GCP remain wizard options
that raise a deferred-feature error.

### 3 — Worker, population and claims

Worker first (#171). `run_worker`, the tick loop, and the outbox drain with
retry and dead-letter. Pulled forward from block 5: CSV import is specified as
worker-processed, and a several-hundred-row import cannot run in a request.
The webhook adapter stays in block 5. A minimal stdlib smtp sender was pulled forward into block 3 (#216) so email OTP can ship; the block-5 adapter replaces it without core changing.

CSV import: upload stores the file and creates a batch, the worker processes
it, the admin page polls status. Column mapping, suppression-key check, batch
rollback.

Claims: submission with consent, email OTP, manual review with mandatory notes,
existing-contact notification on approval, dispute to queue. Lead capture with
consent. Owner dashboard — enough to see leads and edit the listing.

**Done when** several hundred listings import cleanly on the worker and an
owner claims one and receives a lead.

**Status: built; browser pass not recorded.** Worker: outbox drain #174,
`run_worker` and the tick loop #181. CSV import: upload and column mapping #186,
the row loop on the worker #190, batch rollback #203. Mail: queue, drain and the
bundled SMTP sender #216; the settings page and skippable wizard step #229.
Claims: submission and public form #211, `domain_email` verification #222,
review, approval, disputes and the moderation queue #233, operator notification
#235, rate limits and the trusted-proxy client IP #236, the link from the
listing page #230. Owner sign-in, dashboard and listing editing #240. Lead
capture with consent, notices and the owner inbox #243. No merged change records
the criterion itself (several hundred rows imported, then claimed, then a lead
received) being walked in a browser.

### 4 — Money

Tier configuration per tenant. Entitlement records with status and period end.
Comps. Stripe adapter reporting payment outcomes; core resolving tier. Dunning
at 14 days with full perks retained, grace at 30 days downgraded, expiry to
rank 0 without unpublishing. Scheduled transitions on the worker tick.

Every state transition in the table gets a test.

**Done when** an operator sells an upgrade, the badge appears, a declined card
runs the full dunning and grace path, and the listing lands on rank 0 still
published.

**Status: built; real-Stripe pass outstanding.** Tiers, the entitlement state
machine, comps and the clock-driven transitions #242; the payment seam,
checkout, reports and the owner's billing pages #244; the Stripe adapter #245.
The automated tests fake Stripe's responses, so the criterion is not met until a
maintainer has run `docs/stripe-test-mode-checklist.md` against test mode and
recorded the date and API version in release notes. Payments need a verified
domain over HTTPS (decisions.md §4.11).

### 5 — Integration and release

The full SMTP adapter, replacing the block-3 sender, and the webhook adapter. Scheduled tick jobs registered — payload nulling at
90 days, sitemap regeneration (#159), `SearchReindexJob` drain (#132). Command
log and access log wired to the service layer. Adapter documentation. README,
compose file, release tag.

The drain and the tick loop themselves moved to block 3 (#171).

**Status: built except the release itself.**

- Tick jobs (payload nulling at 90 days, sitemap regeneration, the
  `SearchReindexJob` drain, retention sweeps, entitlement transitions) #232 and
  #242.
- Webhook adapter, envelope redaction and adapter logging #249. The bundled SMTP
  sender moved onto `AdapterContext` in the same PR; it is still the stdlib
  sender, not a separate adapter package that replaces it. The generic adapter
  settings page was made to enforce `SettingField.pattern` in #250.
- Command log: operator settings saves #256; claimant and lead PII kept out of
  it #255; validation errors never echo the submitted value #258; claim and lead
  submissions are type-checked before normalising #262. Access log: written for
  the admin lead views only (decisions.md §4.10).
- Adapter documentation: `docs/webhook.md` (#249) and
  `docs/stripe-test-mode-checklist.md` (#245).
- Deployment: compose install #227; `X-Forwarded-Proto` believed only from
  trusted proxies and the TLS `ask` endpoint #251; the opt-in Caddy recipe #253.
- Operator invitation links #261, plus `manage.py issue_operator_invite`.
- Operator install guide: `docs/install.md`.
- **Not done:** the release tag.

---

## Deferred past the alpha

Planned, specified, and designed not to be designed out (see "Out, and
planned" above). Nothing here is in the alpha.

- **Object storage.** S3, Azure and GCP raise a deferred-feature error. Each
  needs a human-gated SDK (#150); a `minio` service joins the compose file in
  the same PR as the S3 backend.
- **Featured slots** — the first post-launch addition. A tier with `uses_slot`
  is refused today.
- **Native reviews** and the moderation queue beyond claim disputes; external
  review display.
- **Phone OTP, postcard and Google Business Profile** claim verification. The
  alpha verifies by `domain_email` and `manual` review.
- **AI agents** and their scope model.
- **Postgres row-level security** as defence in depth; app-level tenancy is the
  only isolation until then.
- **Devcontainer.**
- **Several webhook endpoints per tenant**, overlapping secret rotation, and
  `Retry-After` as an HTTP date (decisions.md §4.12, "Known edges").
- **Acceptance of an invitation by an operator who already exists.** A new
  operator sets a password from the link (§4.14); an existing one has no view.
- **In-app ACME / TLS.** HTTPS is the opt-in Caddy overlay (#253).
- **Access-log coverage beyond lead views**, and pruning expired operator
  invites and operator sessions (#88).

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
