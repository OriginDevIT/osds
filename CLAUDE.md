# CLAUDE.md

Guidance for Claude Code working in this repository. Read this before acting.

## What this is

OSDS (Open Source Directory Site) is a self-hostable, multi-tenant directory website system. An operator installs it, defines one or more directories, populates listings, and sells upgraded placements. It integrates with outside systems - CRMs, payment providers, mail, SMS - through adapters, and functions fully with none of them beyond the bundled defaults.

Apache-2.0. Steward: Origin Development & IT, Inc.

**The specification is authoritative.** `docs/spec/` holds the current spec. When code and spec disagree, the spec wins unless a maintainer says otherwise in writing. If you believe the spec is wrong, open an issue proposing a change - do not resolve the contradiction in code.

**Decisions in `docs/decisions.md` are settled.** Read it before proposing an architectural change. Reopening a decision requires new information, not a fresh opinion — argue it in an issue, don't relitigate in code.

## Non-negotiable invariants

These are architectural commitments, not preferences. A change that violates one of these gets rejected regardless of how well it is written. If you are reviewing a PR, check these first.

1. **Core never imports adapter code.** No vendor name - `stripe`, `gohighlevel`, `twilio` - may appear anywhere under `packages/core/`. Not in an import, not in a conditional, not in a type name. Core knows capability names only.
2. **Core owns entitlement; adapters own money.** Adapters report payment outcomes. Core decides tier consequences. There is no command that sets a listing's tier directly.
3. **Every table carries `tenant_id`.** Every query is tenant-scoped. Single-directory mode is a UI toggle, never a different data model. A migration that adds a table without `tenant_id` is wrong.
4. **No data-source connectors.** OSDS ships no importer, scraper, hook, or plugin interface for any external listing dataset. This is a legal position, not a missing feature. See `docs/spec/` §4.1.1 and `CONTRIBUTING.md`. Contributions adding one are declined on sight - say so politely and link the section.
5. **Events are facts, past tense, immutable.** `listing.claimed`, never `claim_listing`. Event type names are permanent; renaming means adding a new type and deprecating the old.
6. **Commands are validated at the core boundary.** Adapters never write to the database. They send commands; core validates, applies, emits.
7. **Consent is a required field** on `claim.submitted` and `lead.captured`. Core rejects the command without it. Never make it optional to simplify a test fixture.
8. **PII is opt-in per adapter.** Redaction is the default. An adapter receives contact details only with the `pii:contact` scope granted.
9. **Search works on a fresh install.** Postgres FTS + pg_trgm + PostGIS, no extra container, no configuration. External search engines are an optional upgrade and never a requirement.
10. **Agent permissions are enforced by scope, not by prompt.** If a restriction on AI agents exists only in prompt text, it is not implemented.

## Repository layout

```
packages/core/          Domain logic, entitlement engine, event emission, command validation
packages/api/           HTTP surface: public API, admin API, adapter inbound routes
packages/web/           Public site and admin UI
packages/worker/        Outbox consumer, scheduled jobs, adapter runtime
packages/adapter-kit/   Types, test harness, and helpers for adapter authors
adapters/smtp/          Bundled, enabled by default
adapters/webhook/       Bundled, enabled by default
adapters/stripe/        Bundled, opt-in
adapters/paypal/        Bundled, opt-in
adapters/gohighlevel/   Bundled, opt-in - reference CRM implementation
docs/spec/              Authoritative specification
docs/adapters/          Adapter authoring guide
```

An adapter directory may not import from `packages/core/` directly. It imports `packages/adapter-kit/` only.

## Conventions

- TypeScript, strict mode. No `any` without a comment explaining why.
- ULIDs for all IDs, prefixed by entity: `listing_`, `claim_`, `ent_`, `slot_`.
- Timestamps are RFC 3339, UTC, millisecond precision. Store as `timestamptz`.
- Phone numbers are E.164. Emails are lowercased before storage or comparison.
- Money is integer minor units plus an ISO 4217 currency code. Never a float.
- Migrations are forward-only and reversible in principle; every one includes a rollback note in its header comment.
- Tests colocate with source as `*.test.ts`. Every entitlement state transition needs a test - that table is where this system will rot if it rots.
- Conventional Commits. `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.

## Before writing code

Ask three questions:

1. **Does this belong in core or an adapter?** If it names a vendor, it is an adapter. If it defines a rule, it is core.
2. **Does this need a `tenant_id`?** Almost always yes.
3. **What event does this emit?** State changes that other systems care about emit events. If you cannot name the event, the design is probably incomplete.

## Commands

```bash
pnpm install
pnpm dev              # app + worker + postgres + minio via compose
pnpm test             # full suite
pnpm test:core        # domain logic only, fast
pnpm lint
pnpm typecheck
pnpm migrate:dev      # apply migrations to the dev database
pnpm adapter:verify   # run the conformance suite against an adapter
```

Run `pnpm typecheck` and `pnpm test` before proposing any change. Do not open a PR that fails either.

## What requires a human

Do not do these autonomously. Prepare the work, then stop and ask:

- Anything touching `.github/workflows/`, CI configuration, or repository settings
- Anything touching secrets, credentials, authentication, authorization, or cryptography
- Changes to `LICENSE`, `CLAUDE.md`, `SECURITY.md`, or `docs/agent-operations.md`
- Changes to `docs/spec/` (propose in an issue; a maintainer edits the spec)
- Database migrations that drop or rename a column
- Anything in response to a reported security vulnerability
- Releases, version bumps, publishing, tagging
- Adding a new runtime dependency

Full policy in `docs/agent-operations.md`. Read it before acting on issues or PRs.

## Tone in public

You are a visible participant in an open project. Be brief, concrete, and warm. Say what you checked and what you found. When declining something, cite the section that governs it and thank the person for the contribution. Never be curt with a first-time contributor, and never imply a maintainer decision you have not been given.
