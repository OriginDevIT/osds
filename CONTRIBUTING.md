# Contributing
Thanks for your interest. A few things worth knowing before you start.

## The specification is authoritative
`docs/spec/` defines intended behaviour. Where code and spec disagree, the spec wins.
If you think the spec is wrong, open an issue proposing a change rather than resolving
the difference in code.

## Architectural invariants
Listed in `CLAUDE.md`. A change violating one of these will be declined regardless of
quality. The short version: core never names a vendor, core owns entitlement state,
every table carries `tenant_id`, and events are immutable past-tense facts.

## No data-source connectors
OSDS will not accept contributions that import listings from external datasets -
mapping providers, scrapers, or a plugin interface enabling either. This is a legal
and design position, not a roadmap gap. Reasoning in `docs/spec/events-and-adapters.md`
§4.1.1. Supported population paths are manual entry, CSV upload, owner submission,
and the write API; improvements to those are very welcome.

## Automation
A Claude Code agent triages issues, reviews PRs, and maintains documentation on this
repository. It cannot merge, cannot modify CI, and escalates anything touching security.
Its operating policy is public: `docs/agent-operations.md`.

## Sign-off
Commits must be signed off under the Developer Certificate of Origin:

    git commit -s -m "feat: your change"

## Local test database
`docker-compose.dev.yml` runs a Postgres for development and tests:

    docker compose -f docker-compose.dev.yml up -d

then point `DATABASE_URL` at `postgres://postgres:postgres@localhost:5432/osds_test`.
It runs with `fsync`, `synchronous_commit` and `full_page_writes` off, which makes the
full suite run in about a minute instead of tens of minutes. Those flags trade crash
safety for speed, so this database can be corrupted by a crash and is for development
and tests only. Never use that file, or those flags, for an install; `docker-compose.yml`
keeps the defaults.

## Before opening a PR

    pnpm typecheck && pnpm lint && pnpm test

One concern per pull request. Conventional Commits for messages.
