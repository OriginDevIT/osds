# OSDS - Open Source Directory Site
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
listings they publish. See `docs/spec/events-and-adapters.md` §4.1.1.

## Quick start
    docker compose up --build

Then open <http://localhost:8000>. The first-run wizard asks for a setup token,
which the app prints to its log:

    docker compose logs osds-app

No `.env` file is needed. On first boot the stack generates its own secrets into
a named volume (`osds-secrets`). A `.env` (see `.env.example`) is optional
overrides only, such as `OSDS_CONSOLE_HOST`, which must be the hostname you
browse to, without a port.

Four containers: `osds-app`, `osds-worker`, `postgres`, and storage. Storage is
local disk at launch, the `osds-media` volume shared by app and worker.

> **Back up the `osds-secrets` volume.** It holds `OSDS_SECRET_KEY`, which
> encrypts the credentials you enter in the wizard (SMTP password, storage
> keys). If the volume is lost, those credentials cannot be recovered and must
> be re-entered; `docker compose down -v` deletes it along with your data.
> A new `osds-secrets` volume will not work against an existing database volume.

The app is plain HTTP by default (`OSDS_SECURE_COOKIES=false`). Put TLS in front
of it and set `OSDS_SECURE_COOKIES=true` before you go public.

### Behind a reverse proxy
By default OSDS treats the connecting address as the client and ignores
`X-Forwarded-For`, because a client can send any value. If you put a reverse
proxy (Caddy, Traefik, nginx, a load balancer) in front of the app, set
`OSDS_TRUSTED_PROXIES` to the proxy's address or network, for example
`OSDS_TRUSTED_PROXIES=172.18.0.0/16`. Without it every visitor looks like the
proxy, and the per-IP claim rate limits become one limit shared by everyone; the
app logs a warning when it sees that shape. Docker Desktop does the same thing
to a direct install: it shows every request as coming from the Docker gateway.

## Documentation
- [Specification](docs/spec/events-and-adapters.md)
- [Receiving webhooks](docs/webhook.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License
Apache-2.0. Stewarded by Origin Development & IT, Inc.
