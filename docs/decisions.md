# OSDS — Decisions & Session Handoff

Durable record of architectural decisions and current project state. Written for a future contributor, or for pasting into a fresh AI session instead of replaying prior conversations.

**Rule: decisions recorded here are settled.** Reopening one requires new information, not a fresh opinion. If you disagree, open an issue arguing the new information — do not relitigate in code or in a chat session.

Last updated: 2026-08-30

---

## 1. What OSDS is

A self-hostable, multi-tenant directory website system. An operator installs it, defines one or more directories, populates listings, and sells upgraded placements. Integrations with CRMs, payment providers, mail, and SMS happen through adapters. The system runs fully with none of them beyond the bundled defaults.

Apache-2.0. Steward: Origin Development & IT, Inc. Repo: `OriginDevIT/osds`.

---

## 2. Settled decisions

### Product and legal posture

| Decision                                         | Reasoning                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No data-source connectors, ever**              | No importers, scrapers, hooks, or plugin interfaces for external listing datasets. A system that ships an ingestion connector can be argued to have induced whatever the operator did with the data. A system that provides a database and a form cannot. Population paths are manual entry, CSV upload, owner submission, and the write API. Spec §4.1.1. Contributions adding one are declined on sight. |
| **Link to external reviews, never display them** | A "Leave a review on Google" button is a hyperlink — always core, no adapter. Fetching and rendering a provider's ratings is governed by that provider's API terms. Optional adapter, operator's own credentials, operator accepts the terms.                                                                                                                                                              |
| **Consent is a required field**                  | On `claim.submitted` and `lead.captured`. Records granted/timestamp/IP plus `text_version` pointing at an immutable copy of the exact wording shown. Evidentiary, for TCPA. Business phone numbers are not exempt.                                                                                                                                                                                         |
| **Apache-2.0, not AGPL**                         | Maximum adoption, clean for adapter authors, explicit patent grant. Accepted consequence: someone may host a competitor. Relicensing after outside contributions requires every contributor's consent, so this is effectively permanent.                                                                                                                                                                   |
| **Name is OSDS**                                 | Open Source Directory Site. Weak as a brand, precise as a description. CLI verb is `osds`. **Treat as closed.**                                                                                                                                                                                                                                                                                            |

### Architecture

| Decision                                                        | Reasoning                                                                                                                                                                                                                                                        |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Core never imports adapter code**                             | No vendor name appears anywhere under `packages/core/`. Core knows capability names only. This is what keeps GoHighLevel — or any vendor — from becoming a dependency of the project.                                                                            |
| **Core owns entitlement; adapters own money**                   | Payment adapters report outcomes via `entitlement.reportPayment`. Core decides tier consequences. **`listing.setTier` does not exist as a command** — with a settable tier, any adapter becomes the source of truth.                                             |
| **Multi-tenant schema always; single-directory is a UI toggle** | Every table carries `tenant_id` from the first migration. Retrofitting tenancy is brutal; hiding a selector is trivial.                                                                                                                                          |
| **Postgres outbox, no message broker**                          | Events written to an `outbox` table in the same transaction as the state change. `LISTEN/NOTIFY` for latency, polling fallback. Zero extra containers keeps one-click self-hosting viable. A directory does not generate broker-scale traffic.                   |
| **Search is core, not an adapter**                              | Postgres FTS + `pg_trgm` + PostGIS, working on a fresh install with no configuration. External engines are an optional upgrade. A default deployment must never produce a directory nobody can search.                                                           |
| **Custom event envelope, not CloudEvents**                      | CloudEvents requires extension attributes to be lowercase-alphanumeric, producing `oditversion`/`odittenant`. The ugliness signalled a bad fit. Readability for adapter authors beat off-the-shelf routing tooling.                                              |
| **Agent restrictions are scopes, not prompts**                  | No `command:entitlement`, no `compliance.*`, no deletion, mandatory transcript reference, global kill switch. A restriction that exists only in prompt text is not implemented. Core enforces; the adapter implements the conversation.                          |
| **Three logs, three retentions**                                | Event log (envelope forever, payload nulled at 90 days), command log (forever, including rejected and blocked), access log (2 years, separate store). The payload is a second copy of personal data — the copy people forget when processing a deletion request. |

### Implementation

| Decision                                                                 | Reasoning                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Kysely + `pg` + hand-written SQL migrations**                          | Not Prisma (PostGIS is second-class, opinionated shadow-DB migrations), not Drizzle (codegen fights PostGIS/exclusion-constraints/FTS more than it helps), not TypeORM. The schema needs generated `tsvector` columns, GiST indexes, `FOR UPDATE SKIP LOCKED`, and RLS — all of which live in raw DDL. `kysely-codegen` derives types from the migrated database, so the database is the single source of schema truth. |
| **`packages/db` owns schema, migrations, generated types**               | The worker needs outbox tables without pulling in the entitlement engine.                                                                                                                                                                                                                                                                                                                                               |
| **`nodenext` module resolution, not `bundler`**                          | `bundler` accepted extensionless relative imports that Node's ESM loader rejects in emitted output. It only surfaced when a test first consumed a sibling package's `dist`. `nodenext` makes TypeScript resolve exactly as Node does.                                                                                                                                                                                   |
| **Slot allocation: one row per capacity unit, `FOR UPDATE SKIP LOCKED`** | The row _is_ the lock. N racers each take a distinct row lock or get zero rows and fail fast. READ COMMITTED suffices — no SERIALIZABLE, no retry loop, no deadlock, no blocking between callers. Over-sell is impossible by construction. Rejected: counter columns (serialize a whole pool on one row), advisory locks (same, plus no slot identity), materialize-on-demand (`count(*) < capacity` races).            |
| **`osds_app` non-owner role**                                            | RLS is only enforced against a role that is neither the table owner nor `BYPASSRLS`. The app and worker connect as `osds_app` (`DATABASE_URL`); migrations run as the owner (`DATABASE_URL_ADMIN`). Tests that verify RLS as the owner prove nothing.                                                                                                                                                                   |
| **Composite `(tenant_id, id)` foreign keys**                             | Cross-tenant references become structurally impossible rather than policy-dependent.                                                                                                                                                                                                                                                                                                                                    |
| **ULID text primary keys with entity prefixes**                          | `tnt_`, `cat_`, `usr_`, `listing_`, `claim_`, `ent_`, `pool_`, `slot_`, enforced by `starts_with()` CHECKs.                                                                                                                                                                                                                                                                                                             |

### Entitlement behaviour

| Decision                      | Value                                                           | Reasoning                                                                                                                                       |
| ----------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Dunning window                | 14 days                                                         | Most failed payments are involuntary — expired cards, not churn                                                                                 |
| Public display during dunning | Full perks retained                                             | Demoting someone whose card expired is invisible to them and loses customers who intended to pay                                                |
| Grace after dunning           | 30 days, downgraded, restore path open                          |                                                                                                                                                 |
| At expiry                     | Downgrade to rank-0, **never unpublish**                        | Unpublishing destroys an indexed page and reads as punitive. Exception: a tenant with no rank-0 tier hides the listing                          |
| Cancellation                  | At period end, **skips grace**                                  | They chose to leave; grace exists for involuntary failure                                                                                       |
| Refund                        | Immediate downgrade                                             |                                                                                                                                                 |
| Data collected while paid     | Retained, access gated                                          | Never delete leads or reviews on downgrade. "You received 34 leads while on Featured" is the best renewal prompt available                      |
| Trials                        | Card required up front; **off by default on slot-backed tiers** | No-card trials convert poorly. A trial consuming scarce inventory must be a deliberate choice                                                   |
| Comps                         | Consume sellable slot capacity                                  | Anything else makes capacity numbers lie. Use a locked slot to avoid it                                                                         |
| Proration                     | Payment adapter computes it; core receives a new `period_end`   | Keeps fiddly arithmetic in the system that already solves it                                                                                    |
| Waitlist notice               | T-10 days, worded _"may_ become available"                      | The incumbent holds right of first refusal until the moment of expiry. Overstating availability generates complaints                            |
| Unsold slot fill              | Locked → default featured → random rotation                     | Rotation means a new directory never shows empty slots, and free listings get intermittent premium placement — the best upgrade pitch available |

### Claim verification

- **Manual admin review is the default and always available.** Records `method_used`, `verified_by`, `verified_at`, and mandatory `notes` — an admin who cannot articulate how they verified has not verified.
- **Phone OTP is the workhorse.** GBP OAuth is strongest but requires a separate approved Google Cloud project **per self-hoster**; optional adapter, never default. Domain email is weak. Postcard is strong for the address, costs the operator ~$1–2 per piece.
- **Notify every existing contact channel on successful claim.** Catches what verification misses, costs almost nothing.
- **Mask contact details in the verification UI.** Otherwise the claim flow is a phone-number disclosure endpoint for every listing on the site.
- **Disputes go to moderation, never auto-transfer.** Verification alone never moves ownership from a sitting owner.

### Deployment

Docker-first. Four containers: `osds-app`, `osds-worker`, `postgres`, `minio` — the last two replaceable by managed services through environment variables alone. First-run browser wizard, never config files. Named Docker volumes on Windows, not bind mounts.

---

## 3. Current state

### Repository

`OriginDevIT/osds`, public, Apache-2.0. Branch protection on `main`: PR required, `build` status check required, force pushes blocked. Required approvals currently **0** — a solo maintainer cannot self-approve (issue #10 restores it to 1 when the agent account is active).

CI runs `typecheck`, `lint`, `test` on `pull_request` with `permissions: contents: read` and a `postgis/postgis:16-3.4` service container. **`pull_request_target` is never used** — combined with checking out fork code it is the known route to repository compromise.

### Packages

| Package                                           | State                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `packages/adapter-kit`                            | Complete for the current spec. Event envelope (`OsdsEvent`, `TenantEvent`, `OsdsAnyEvent`), 72 event types across 14 namespace unions, command envelope, adapter interface. Types only, zero dependencies                                                                                                                     |
| `packages/db`                                     | Migrations 0001–0014. Tables: tenants, tiers, categories, listing_categories, users, listings, claims, entitlements, slot_pools, slots, outbox. Forced RLS everywhere except `tenants`, generated `tsvector` (GIN) and `geography` (GiST) on listings, `osds_app` role, outbox with `pg_notify` trigger and idempotency index |
| `packages/core`                                   | Entitlement state machine (§6.3) and public-render resolver (§6.5) as pure functions. Command validation layer for `entitlement.reportPayment` and `entitlement.grant`                                                                                                                                                        |
| `packages/api`, `packages/web`, `packages/worker` | Not started                                                                                                                                                                                                                                                                                                                   |
| `adapters/*`                                      | Not started                                                                                                                                                                                                                                                                                                                   |

168 tests passing, including 11 against a real Postgres.

### Spec

`docs/spec/events-and-adapters.md`, currently **v0.4**. Authoritative — where code and spec disagree, the spec wins. Spec edits are a maintainer action, not an agent action.

Section map: §2 envelope · §3 events (3.3 is the canonical catalogue) · §4 core entities · §5 reviews · §6 entitlements · §7 commands · §8 adapter interface · §9 claim verification · §10 GHL reference adapter · §11 transport and logging · §12 search and SEO · §13 deployment · §14 versioning · §15 open.

### Open issues

| #   | Title                           | Notes                                                                                                                             |
| --- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| 8   | adapter-kit version constant    | `OSDS_API_VERSION` dropped during re-authoring. Decide when something imports it                                                  |
| 10  | Restore required approvals to 1 | When the Odin agent account is active                                                                                             |
| 20  | Clarify CLAUDE.md scope         | The workflow-edit prohibition is aimed at the autonomous agent, not a supervised local session. Wording does not distinguish them |

---

## 4. Next tasks, in order

1. **Slot allocator in `packages/core`.** The SQL design is approved and the tables exist. Wrap `FOR UPDATE SKIP LOCKED` in a tested function, including hold expiry and the waitlist notification trigger.
2. **Outbox consumer in `packages/worker`.** `LISTEN/NOTIFY` with polling fallback, exponential backoff (1s → 1h, 12 attempts), dead-letter queue, 30s handler timeout.
3. **Bundled `smtp` and `webhook` adapters.** Without `smtp`, no claim verification code can be sent, so nobody can claim a listing. `webhook` is the universal escape hatch. Both ship enabled by default. This is also where the adapter runtime gets exercised for the first time.
4. **Claim flow end to end** — the first vertical slice touching every layer.
5. **Public site rendering and search.**

Deferred until there is traffic: the Odin repo-watching agent (`docs/agent-operations.md` and the setup guide are written; the account and workflows are not). It triages issues and reviews PRs, of which there are currently few from anyone but the maintainer.

---

## 5. Working conventions

- Branch, PR, squash-merge. Never commit to `main` — branch protection refuses it.
- `git commit -s` always. DCO is required by `CONTRIBUTING.md`.
- Conventional Commits. One concern per PR.
- `pnpm typecheck && pnpm lint && pnpm test` before opening a PR.
- Windows/PowerShell 7. Here-strings (`@'...'@ | Set-Content -Encoding utf8`) for file creation — `>` produces UTF-16 and breaks everything downstream.
- `.prettierignore` excludes `docs/spec/` so the spec does not reformat on save and bury real changes in whitespace churn.
- Migrations are forward-only with a rollback note in each header comment.
- Every entitlement state transition needs a test. That table is where this system rots if it rots.

### Prompting Claude Code

Prompts must be short — long ones fail to paste. Give it the task, the spec section, and the constraint; let it read the rest.

It reads `CLAUDE.md` automatically. Approve reads individually at first. Do not whitelist `git push`, `docker exec *`, or anything that writes outside the repo.

**When it flags a deviation from the spec, take it seriously.** It has twice been right where the spec or the instruction was wrong: the incomplete event union in v0.3, and the claim that v0.4 renumbering left references valid. It correctly refused to invent event names not present in the spec.

**Push back on generated bulk-rewrite scripts.** When it proposed a regex rewrite of imports across 25 files, the right answer was to fix the compiler setting so `tsc` identified each site individually.
