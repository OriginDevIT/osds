# OSDS — Open Source Directory Site
Self-hostable, multi-tenant directory website system. Run one directory or fifty
from a single installation. Deploy with Docker.

**Status:** pre-alpha. The specification is ahead of the implementation.

## What it does
- Business listings with categories, geography, media, and search
- Owner claim and verification flows
- Paid placement tiers with a capacity-limited featured slot system
- Native reviews, or link out to Google, Facebook, or Yelp
- Adapters for CRM, payments, mail, and SMS - none of them required

## What it deliberately does not do
OSDS ships no connectors to any external listing dataset. No scrapers, no imports
from mapping providers, no plugin hook for one. The operator is responsible for the
listings they publish. See `docs/spec/events-and-adapters.md` §3.1.1.

## Documentation
- [Specification](docs/spec/events-and-adapters.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License
Apache-2.0. Stewarded by Origin Development & IT, Inc.
