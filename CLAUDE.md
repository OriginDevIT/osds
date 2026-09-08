# CLAUDE.md

Guidance for Claude Code working in this repository. Read this before acting.

## What this is

OSDS (Open Source Directory Site) is a self-hostable, multi-tenant directory
website system. An operator installs it, defines one or more directories,
populates listings, and sells upgraded placements. It integrates with outside
systems — CRMs, payment providers, mail, SMS — through adapters, and functions
fully with none of them beyond the bundled defaults.

Apache-2.0. Steward: Origin Development & IT, Inc.

Django 5, Postgres 16, server-rendered templates with HTMX. No Celery, no
Redis, no SPA framework. Four containers: `osds-app`, `osds-worker`,
`postgres`, and object storage.

## Read this before proposing anything

`docs/spec/events-and-adapters.md` describes the product. It was written
against an earlier TypeScript implementation, so **its behavioural rules are
authoritative and its implementation details are not.** Event names, payload
shapes, entitlement state transitions, claim rules, consent requirements and
the tier model all still bind. References to `packages/*`, Kysely, the
adapter-kit type union, and Postgres RLS policies describe an implementation
that no longer exists.

`docs/decisions.md` is the same split, and says explicitly which decisions
carry forward and which are superseded. Reopening a functional decision
requires new information, not a fresh opinion — argue it in an issue.

The Node implementation is archived at tag `v0-node`. Do not port code from it.
Read it if you want to understand why a rule exists.

## Non-negotiable invariants

Architectural commitments, not preferences. A change violating one gets
rejected regardless of how well it is written.

1. **Core never imports adapter code.** No vendor name — `stripe`,
   `gohighlevel`, `twilio` — appears anywhere in `directory/`, `tenants/`, or
   `billing/`. Not in an import, not in a conditional, not in a model field
   name. Core knows capability names only. Vendor code lives under `adapters/`.

2. **Core owns entitlement; adapters own money.** Adapters report payment
   outcomes. Core decides tier consequences. There is no code path that sets a
   listing's tier directly — tier is resolved from the entitlement record.

3. **Every tenant-scoped model carries a `tenant` FK** and is queried through
   the tenant-scoped default manager. A model holding tenant data without one
   is wrong. Single-directory mode is a UI toggle, never a different data
   model.

   Principal and structural models sit outside this: `Tenant` itself,
   `Operator`, and operator sessions. An operator spans tenants by design.
   `StaffMembership` carries a tenant because it _is_ the tenant relationship.
   This is a scope clarification, not an exception, and it does not license
   omitting the FK from anything holding tenant data.

   **Never call `.objects.all()` or an unfiltered `.objects.filter()` on a
   tenant-scoped model without the tenant in scope.** App-level isolation is
   the only isolation there is. One unscoped query is a cross-tenant leak.

4. **No data-source connectors.** OSDS ships no importer, scraper, hook, or
   plugin interface for any external listing dataset. This is a legal position,
   not a missing feature. See spec §4.1.1 and `CONTRIBUTING.md`. Contributions
   adding one are declined on sight — say so politely and link the section.

   CSV upload, manual entry, owner submission and the write API are the four
   permitted population paths. A CSV importer is not a data-source connector.

5. **Events are facts, past tense, immutable.** `listing.claimed`, never
   `claim_listing`. Event type names are permanent; renaming means adding a new
   type and deprecating the old. Every state change other systems care about
   writes an outbox row in the same transaction as the change. Every emitted
   type must appear in the event-name constants module — a test enforces this.

6. **Writes go through the service layer, not the ORM directly.** A view,
   management command, or adapter that saves a model bypasses event emission
   and the command log. Django admin is disabled for tenant data for this
   reason.

7. **Consent is a required field** on claim submission and lead capture. Reject
   the write without it. Records granted, timestamp, IP, and the version of the
   wording shown. Never make it optional to simplify a test fixture.

8. **PII is opt-in per adapter.** Redaction is the default. An adapter receives
   contact details only with the `pii:contact` scope granted.

9. **Search works on a fresh install.** Postgres full-text plus `pg_trgm`, no
   extra container, no configuration. External search engines are an optional
   upgrade and never a requirement.

10. **Agent permissions are enforced by scope, not by prompt.** A restriction on
    AI agents that exists only in prompt text is not implemented.

## Repository layout

    osds/                   Project settings, URLs, WSGI
    tenants/                Tenant, Operator, StaffMembership, host resolution
    directory/              Listings, listing types, categories, claims, leads
    billing/                Tiers, entitlements, slots
    audit/                  Command log, access log
    adapters/               Vendor integrations, one package each
    docs/spec/              Product specification
    docs/decisions.md       Settled decisions

An adapter may not import from `directory/` or `billing/` internals. It
receives events and calls the published service functions.

## Dependencies that are ruled out

Do not add these. Each was considered and rejected for a recorded reason; the
reasoning is in `docs/decisions.md`.

- **Celery, Redis, RabbitMQ.** Celery has no supported Postgres broker, so it
  adds a fifth container and breaks one-click self-hosting. Scheduled work runs
  in the existing `osds-worker` tick loop. If a real queue abstraction is ever
  needed, it must use the database as its broker.
- **`django-auditlog`, `django-simple-history`, or any `post_save` audit
  package.** They record what succeeded, inside the transaction that succeeded.
  The command log exists to capture rejected and blocked attempts, written
  outside the command transaction so it survives a rollback. A signal-based
  package structurally cannot see the rows that matter most.
- **GeoDjango, GDAL, PostGIS Python bindings.** They need native binaries the
  development machine does not have. Geo is `lat`/`lon` decimal fields plus
  raw-SQL distance. Raise it if something genuinely requires them; the answer
  may be a devcontainer rather than a refusal.
- **Any SPA framework.** Server-rendered templates plus HTMX.

Adding any runtime dependency requires a human. Say what you would add and why,
then stop.

The runtime tree is Django, psycopg 3, `cryptography` (encrypts `Secret`
values), `gunicorn` (the container server), `whitenoise` (static files) and
**Pillow**. Pillow is the only dependency added since the reset — it decodes
uploads for validation, reads dimensions, and strips EXIF by rebuilding from
raw pixels. Anything else you believe you need is a conversation, not a
`requirements.txt` edit.

## Conventions

- Python 3.12, Django 5. Type hints on service-layer functions.
- ULIDs for public-facing IDs, prefixed by entity: `listing_`, `claim_`,
  `ent_`, `slot_`. Integer PKs are acceptable internally.
- Timestamps stored with `timezone.now()`, UTC, `DateTimeField`. Serialized as
  RFC 3339.
- Phone numbers are E.164. Emails are lowercased before storage or comparison.
- Money is integer minor units plus an ISO 4217 currency code. Never a float.
- Migrations are forward-only. Never edit a migration that has been applied
  anywhere — a correction is a new migration, written to be idempotent.
- Storage is per-tenant and resolved at runtime from tenant configuration, not
  from `settings.STORAGES` at import time.
- Long-running work — CSV import, media processing, mail — is queued to the
  worker, never run inside a request.
- Tests live in `tests/` per app. Every entitlement state transition needs a
  test — that table is where this system will rot if it rots.
- Conventional Commits. `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`.

## Before writing code

1. **Does this belong in core or an adapter?** If it names a vendor, it is an
   adapter. If it defines a rule, it is core.
2. **Does this need a tenant?** Almost always yes.
3. **What event does this emit?** If you cannot name the event, the design is
   probably incomplete.
4. **Will this run longer than a request should?** If so it belongs on the
   worker.

## Commands

    python manage.py runserver
    python manage.py test
    python manage.py makemigrations
    python manage.py migrate
    python manage.py check
    python manage.py run_worker

Run `python manage.py check` and `python manage.py test` before proposing any
change. Do not open a PR that fails either.

The development machine is **Windows with PowerShell 7**. Commands you propose
must be PowerShell. Use here-strings for file creation — `>` writes UTF-16 and
breaks everything downstream.

Nine environment variables. `DJANGO_SECRET_KEY`, `OSDS_SECRET_KEY` and
`DATABASE_URL` raise at import if unset. `DATABASE_URL_ADMIN` is used by the
entrypoint to migrate as the database owner. `DJANGO_DEBUG` and
`OSDS_CONSOLE_HOST` configure the install. `OSDS_SECURE_COOKIES` defaults true
and must be false on an HTTP-only install or no login POST can pass CSRF.
`OSDS_DEV_TENANT_SLUG` is DEBUG-only. `OSDS_MEDIA_ROOT` sets the local storage
root and defaults to `BASE_DIR/media`; per-tenant media lands under
`<OSDS_MEDIA_ROOT>/<tenant.public_id>/`.

`python manage.py ensure_setup_token` mints the `InstallSetup` row and prints
the first-run token. Nothing else creates it — a fresh database 404s the wizard
until it has run.

## What requires a human

Do not do these autonomously. Prepare the work, then stop and say what you
would do.

- Anything touching `.github/workflows/`, CI configuration, or repository
  settings
- Anything touching secrets, credentials, authentication, authorization, or
  cryptography
- Changes to `LICENSE`, `CLAUDE.md`, `SECURITY.md`, or `docs/spec/`
- Migrations that drop or rename a column
- Anything in response to a reported security vulnerability
- Releases, version bumps, publishing, tagging
- Adding a new runtime dependency

## Tone in public

You are a visible participant in an open project. Be brief, concrete, and warm.
Say what you checked and what you found. When declining something, cite the
section that governs it and thank the person for the contribution. Never be
curt with a first-time contributor, and never imply a maintainer decision you
have not been given.
