# OSDS — Decisions

Durable record of architectural decisions and current project state. Written
for a future contributor, or for pasting into a fresh AI session instead of
replaying prior conversations.

**Rule: decisions recorded here are settled.** Reopening one requires new
information, not a fresh opinion. If you disagree, open an issue arguing the
new information — do not relitigate in code or in a chat session.

Last updated: 2026-09-05

---

## 0. The reset

OSDS was first built in TypeScript — a pnpm monorepo of five packages on
Next.js, Kysely and Postgres RLS. That implementation is archived at tag
`v0-node`. It reached 444 passing tests, a working admin login, and a public
site, and it was several weeks from a shippable product.

**The build had diverged from the goal.** The goal is a system that is easy to
deploy and easy to manage, that hosts directory sites. The specification grew
to describe a mature product — premium slot concurrency, dunning windows,
agent escalation triggers — and the implementation chased the specification
rather than the goal. Correctness work on layers nobody could yet use is the
symptom.

The rebuild is Django 5 on Postgres 16, server-rendered, four containers.

**What carries forward:** every product, legal and behavioural decision below.
The specification's rules still bind.

**What is superseded:** everything about how those rules were implemented.
Kysely, the adapter-kit type union, `packages/*` boundaries, Postgres
row-level security as the tenancy mechanism, composite `(tenant_id, id)`
foreign keys, the `osds_app` non-owner role, scrypt session handling, the
`operator_login_attempts` design, and the five admin-surface routing rulings.
They were correct for that implementation. They are not decisions about this
one, and they are in git history if the reasoning is ever needed.

---

## 1. What OSDS is

A self-hostable, multi-tenant directory website system. An operator installs
it, defines one or more directories, populates listings, and sells upgraded
placements. Integrations with CRMs, payment providers, mail and SMS happen
through adapters. The system runs fully with none of them beyond the bundled
defaults.

Apache-2.0. Steward: Origin Development & IT, Inc. Repo: `OriginDevIT/osds`.

---

## 2. Product and legal posture

Unchanged by the reset. These are why the project exists in this shape.

| Decision                                         | Reasoning                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No data-source connectors, ever**              | No importers, scrapers, hooks or plugin interfaces for external listing datasets. A system that ships an ingestion connector can be argued to have induced whatever the operator did with the data. A system that provides a database and a form cannot. Population paths are manual entry, CSV upload, owner submission and the write API. Spec §4.1.1. Contributions adding one are declined on sight. |
| **Link to external reviews, never display them** | A "Leave a review on Google" button is a hyperlink — always core, no adapter. Fetching and rendering a provider's ratings is governed by that provider's API terms. Optional adapter, operator's own credentials, operator accepts the terms.                                                                                                                                                            |
| **Consent is a required field**                  | On claim submission and lead capture. Records granted, timestamp, IP, plus a version pointer to an immutable copy of the exact wording shown. Evidentiary, for TCPA. Business phone numbers are not exempt.                                                                                                                                                                                              |
| **Apache-2.0, not AGPL**                         | Maximum adoption, clean for adapter authors, explicit patent grant. Accepted consequence: someone may host a competitor. Relicensing after outside contributions requires every contributor's consent, so this is effectively permanent.                                                                                                                                                                 |
| **Name is OSDS**                                 | Open Source Directory Site. Weak as a brand, precise as a description. Treat as closed.                                                                                                                                                                                                                                                                                                                  |

---

## 3. Functional decisions

The product rules. Framework-independent, and the reason the rebuild is a
rebuild rather than a restart.

### Tenancy and principals

| Decision                                                        | Reasoning                                                                                                                                                                                                                                                                                                                      |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Multi-tenant schema always; single-directory is a UI toggle** | Every tenant-scoped model carries a tenant reference from the first migration. Retrofitting tenancy is brutal; hiding a selector is trivial.                                                                                                                                                                                   |
| **All tenant data is tenant-specific**                          | No crossover between tenants, including users. A directory user belongs to exactly one tenant.                                                                                                                                                                                                                                 |
| **Two kinds of person**                                         | A **user** owns or seeks to own a listing and belongs to one tenant. An **operator** administers directories, belongs to no tenant, and is one login across the installation. Both have an email address, which is not a reason to merge them. Spec §4.3, §4.4.                                                                |
| **Staff membership is the link**                                | `StaffMembership(operator, tenant, role, status)`. No row means no access to that tenant, whatever the operator holds elsewhere. Admins may therefore be tenant-specific or multi-tenant, with no separate concept for either.                                                                                                 |
| **Two axes, not one ladder**                                    | `is_superadmin` on the operator; `role` on the membership. Creating or suspending a tenant happens outside any tenant, so no membership can authorise it. Everything else is per-tenant.                                                                                                                                       |
| **Superadmin holds no implicit tenant access**                  | They add themselves a membership at a stated role. Unpreventable in principle — anyone who can assign memberships can assign their own. The point is evidence: a grant leaves a row and an event, so hosting-side access to a client's directory is a fact on the record rather than an invisible capability.                  |
| **Five ordered role ranks**                                     | `admin` 4, `manager` 3, `editor` 2, `moderator` 1, `support` 0. Authorization is `rank >= n`. The load-bearing cuts are 3/2 (money and PII) and 2/1 (authority over other people's listings). A capability matrix is the right shape only when roles are configurable, and these are not.                                      |
| **Memberships on an existing operator start pending**           | Otherwise an admin of one tenant can silently attach a directory to the account of an admin of another. A membership minted together with its operator activates on password set — accepting the invitation and the membership are one act.                                                                                    |
| **Invitation never writes to an existing operator row**         | Membership only, and the response is identical whether the email matched. Match-and-update on an invite form is cross-tenant account takeover. A differing response is an oracle for which clients administer other directories on the installation.                                                                           |
| **Tenant admin lives at `/admin` on the tenant's own domain**   | The DNS record and certificate already exist for the public site. A second hostname per tenant is another record the operator must get right before they can log in at all, and a lockout with a DNS error is the worst first experience available.                                                                            |
| **The console is a separate host and is not a tenant**          | Installation-level operations and a directory picker. Every operator reaches it, because accepting a pending invitation happens there. Clicking a tenant opens that tenant's `/admin` and does not carry the session — a cross-host handoff token is a new credential type and the piece most likely to be built subtly wrong. |

### Listings

| Decision                                    | Reasoning                                                                                                                                                                                                                           |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Directories have a type**                 | Businesses, locations, people, services, software and others. A listing type carries a field schema; type-specific columns would be a rewrite per type. Spec §4.6.                                                                  |
| **Tiers are tenant-configured**             | An ordered list with a `rank`. Rank 0 is the fallback tier. Core hardcodes no tier names. A tenant may define no rank-0 tier, which changes downgrade behaviour.                                                                    |
| **Provenance is recorded on every listing** | Source, import batch, submitter. Four operational reasons: dedupe across population routes, undo of an import batch, removal that sticks via a suppression key, and trust display on the public page. Not about upstream licensing. |
| **Removal sticks**                          | A deleted listing records a suppression key — normalised name, address and phone hash — that subsequent imports check against, so a removed business does not reappear on the next CSV upload.                                      |
| **Search is core**                          | Postgres full-text plus trigram matching, working on a fresh install with no configuration and no extra container. External engines are an optional upgrade. A default deployment must never produce a directory nobody can search. |
| **Sitemap index from day one**              | URL count grows faster than listing count — categories × locations × pagination crosses 50,000 URLs on a 5,000-listing directory. Decide deliberately which facet combinations are indexable.                                       |

### Claims

| Decision                                                      | Reasoning                                                                                                                                                                                                                                        |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Manual admin review is the default and always available**   | Records the method used, who verified, when, and mandatory notes — an admin who cannot articulate how they verified has not verified.                                                                                                            |
| **Email OTP ships alongside manual at launch**                | Phone OTP, postcard and Google Business Profile OAuth are planned and must not be designed out. Phone OTP is the eventual workhorse. GBP requires a separate approved Google Cloud project per self-hoster, so it is optional and never default. |
| **Core computes code expiry, never the caller**               | A lifetime is a rule. Tenant-configurable within bounds core enforces; a value outside the bounds is rejected at configuration time, not silently clamped at use.                                                                                |
| **Notify every existing contact channel on successful claim** | Catches what verification misses, costs almost nothing.                                                                                                                                                                                          |
| **Mask contact details in the verification UI**               | Otherwise the claim flow is a phone-number disclosure endpoint for every listing on the site.                                                                                                                                                    |
| **Disputes go to moderation, never auto-transfer**            | A second claim on a claimed listing opens a moderation item. Verification alone never moves ownership away from a sitting owner.                                                                                                                 |

### Entitlements and money

| Decision                                      | Reasoning                                                                                                                                                                                                                     |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Core owns entitlement; adapters own money** | Payment adapters report outcomes. Core decides tier consequences. **There is no command that sets a listing's tier** — with a settable tier, any adapter becomes the source of truth.                                         |
| **Payments ship at launch**                   | Comps and paid entitlements both. The full lifecycle seam — status, period end, payment reference — is built once rather than retrofitted.                                                                                    |
| Dunning window                                | 14 days. Most failed payments are involuntary — expired cards, not churn.                                                                                                                                                     |
| Public display during dunning                 | Full perks retained. Demoting someone whose card expired is invisible to them and loses customers who intended to pay.                                                                                                        |
| Grace after dunning                           | 30 days, downgraded, restore path open.                                                                                                                                                                                       |
| At expiry                                     | Downgrade to rank 0, **never unpublish**. Unpublishing destroys an indexed page and reads as punitive. Exception: a tenant with no rank-0 tier hides the listing, and the admin UI must say so at the point of configuration. |
| Cancellation                                  | At period end, skips grace. They chose to leave; grace exists for involuntary failure.                                                                                                                                        |
| Refund                                        | Immediate downgrade.                                                                                                                                                                                                          |
| Data collected while paid                     | Retained, access gated. Never delete leads or reviews on downgrade. "You received 34 leads while on Featured" is the best renewal prompt available.                                                                           |
| Trials                                        | Card required up front. Off by default on slot-backed tiers.                                                                                                                                                                  |
| Comps                                         | Consume sellable slot capacity. Anything else makes capacity numbers lie.                                                                                                                                                     |
| Proration                                     | The payment adapter computes it; core receives a new period end. Keeps fiddly arithmetic in the system that already solves it.                                                                                                |

### Slots — first post-launch addition

Not built for launch. Rotation of unsold placements and locked capacity are
separable and may land earlier.

| Decision         | Reasoning                                                                                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Waitlist notice  | T-10 days, worded "_may_ become available". The incumbent holds right of first refusal until the moment of expiry. Overstating availability generates complaints.                                  |
| Unsold slot fill | Locked → default featured → random rotation. Rotation means a new directory never shows empty slots, and free listings get intermittent premium placement — the best upgrade pitch available.      |
| Allocation       | One row per capacity unit, locked with `FOR UPDATE SKIP LOCKED`. The row _is_ the lock. Over-sell is impossible by construction. Rejected: counter columns, advisory locks, materialize-on-demand. |

### Adapters

| Decision                                                       | Reasoning                                                                                                                                                                                                                                                        |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Core never imports adapter code**                            | No vendor name appears in core. Core knows capability names only. This is what keeps any single vendor from becoming a dependency of the project.                                                                                                                |
| **Adapters never write core listings directly**                | They send commands; core validates, applies and emits. An adapter that writes the database bypasses event emission and the command log.                                                                                                                          |
| **Postgres outbox, no message broker**                         | Events written in the same transaction as the state change, drained by a worker. Zero extra containers keeps one-click self-hosting viable. A directory does not generate broker-scale traffic.                                                                  |
| **Delivery is at-least-once**                                  | Adapters dedupe on event id. Ordering is guaranteed per subject, never globally.                                                                                                                                                                                 |
| **Loop prevention is structural**                              | Core stamps the originating adapter id on the resulting event; an adapter ignores any event it originated.                                                                                                                                                       |
| **PII is opt-in per adapter**                                  | Redaction is the default.                                                                                                                                                                                                                                        |
| **Agent restrictions are scopes, not prompts**                 | No entitlement commands, no compliance commands, no deletion, mandatory transcript reference, global kill switch. A restriction that exists only in prompt text is not implemented. Agents are post-launch; the scope model must not be designed out.            |
| **Three logs, three retentions**                               | Event log (envelope forever, payload nulled at 90 days), command log (forever, including rejected and blocked), access log (2 years, separate store). The payload is a second copy of personal data — the copy people forget when processing a deletion request. |
| **The command log is written outside the command transaction** | Recorded before the transaction opens, concluded after it settles. A log written inside the transaction it is logging disappears when that transaction rolls back — which is exactly the case the log exists for. A concluded row is never rewritten.            |

### Deployment

| Decision                                  | Reasoning                                                                                                                                                                                         |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **First-run wizard, never config files**  | The target operator can rent a server but should never edit YAML to set an admin password. The wizard sets the superadmin, the first tenant, its domain, storage, SMTP and enabled claim methods. |
| **The wizard assists with DNS**           | It shows the record to create and verifies it, rather than assuming the operator knows. A lockout behind a DNS error is the worst first experience available.                                     |
| **Setup token printed to container logs** | Plus the route disappearing once an operator exists. The gap between bringing the stack up and opening a browser is a real window on a public IP, and the count check alone does not cover it.    |
| **Four containers**                       | App, worker, Postgres, object storage — the last two replaceable by managed services through configuration alone. A fifth container to support any single feature is a decision, not a detail.    |

---

## 4. Implementation decisions — Django

New. These are decisions about the current implementation only.

| Decision                                                                         | Reasoning                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Django 5, Postgres 16, templates plus HTMX**                                   | Server-rendered fits a directory. No SPA framework, no build step for the public site.                                                                                                                                                                                                                                                                                                                         |
| **Row-level tenancy via a scoped default manager**, not RLS or schema-per-tenant | Schema-per-tenant makes the cross-tenant console query N queries. Postgres RLS remains the correct defence in depth and is planned, but app-level scoping ships first. **The consequence is accepted and named: one unscoped query is a cross-tenant leak.** A test asserts every tenant-scoped model's default manager is the scoped one.                                                                     |
| **Writes go through a service layer**                                            | The ORM does not emit events or write the command log. Django admin is disabled for tenant data for the same reason — it writes tables directly. Django admin is used for the installation console only.                                                                                                                                                                                                       |
| **No Celery, no Redis, no broker**                                               | Celery has no supported Postgres broker; the SQLAlchemy and Django transports were dropped. Adding it means a fifth container and breaks one-click self-hosting. Scheduled work — dunning transitions, grace expiry, term expiry, renewal notices, payload nulling, sitemap regeneration — runs in the worker's tick loop. If a real queue abstraction is later needed it must use the database as its broker. |
| **Long work is queued, never in-request**                                        | CSV import, media processing and mail run on the worker. A several-hundred-row import times out a browser request. The upload stores the file and creates a batch row; the worker processes it; the admin page polls status.                                                                                                                                                                                   |
| **No `post_save` audit package**                                                 | `django-auditlog` and `django-simple-history` record what succeeded, inside the transaction that succeeded. The command log's purpose is rejected and blocked attempts, written outside the command transaction. A signal-based package structurally cannot see the rows that matter most. Both logs are hand-rolled.                                                                                          |
| **Storage is per-tenant, resolved at runtime**                                   | `django-storages` reads settings at import time, and wizard-entered credentials live in the database. A thin backend resolves the target from tenant configuration per request. Local disk is the default; S3, Azure and GCP are configurable in the wizard.                                                                                                                                                   |
| **Geo is `lat`/`lon` plus raw-SQL distance**                                     | GeoDjango needs GDAL and GEOS binaries the development machine does not have. Radius search works without them. PostGIS is planned server-side.                                                                                                                                                                                                                                                                |
| **A devcontainer is planned, not deferred indefinitely**                         | Anything that genuinely requires Linux-only native binaries pulls it forward immediately.                                                                                                                                                                                                                                                                                                                      |
| **Event names live in a constants module**                                       | With a test asserting every emitted type appears in it. This replaces the TypeScript union that made a typo a compile error.                                                                                                                                                                                                                                                                                   |

---

## 5. Working conventions

- Branch, PR, squash-merge. Never commit to `main` — branch protection refuses it.
- `git commit -s` always. DCO is required by `CONTRIBUTING.md`.
- Conventional Commits. One concern per PR.
- `python manage.py check` and `python manage.py test` before opening a PR.
- `gh issue create` takes `--label`; labels are not applied otherwise, and a nonexistent label fails the whole command.
- Windows / PowerShell 7. Here-strings for file creation — `>` produces UTF-16 and breaks everything downstream.
- Migrations are forward-only. **A recorded migration is never edited.** A correction is a new forward migration, written to be idempotent so a from-zero database is unaffected. CI cannot catch an edited migration — it migrates from zero on every run, so an edited migration and a correct one are indistinguishable.
- `git checkout main; git pull` before every `git checkout -b`.
- Every entitlement state transition needs a test. That table is where this system rots if it rots.

### Prompting Claude Code

Prompts must be short — long ones fail to paste. Give it the task, the spec
section, and the constraint; let it read the rest.

**Ask "report, do not change" before accepting any layer that touches roles,
locks, or event emission.** Three real bugs in the Node build — a phantom
patch operation, a missing row lock, and a persistence layer that silently
required the database owner role — all surfaced from narrow read-only
questions, not from reviewing the diff. None would have been caught by a green
test run.

**When it flags a deviation from the spec, take it seriously.** It was right
six times in the Node build where the spec or the instruction was wrong, and it
correctly refuses to invent event names not present in the spec.

**Push back on generated bulk-rewrite scripts.** When it proposed a regex
rewrite of imports across 25 files, the right answer was to fix the compiler
setting so each site was identified individually.

**Unit tests that mock the database will not catch a missing table.** Run the
browser pass.

Approve reads individually. Do not whitelist `git push`, `git add *`,
`docker exec *`, or anything that writes outside the repository.
