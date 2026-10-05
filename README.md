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

### Running management commands
Run `manage.py` commands inside the stack with `osds-manage`, not
`python manage.py`:

    docker compose exec osds-app osds-manage issue_operator_invite --help
    docker compose exec osds-app osds-manage ensure_setup_token

`docker compose exec` does not see the secrets the container generated at boot,
so a bare `python manage.py` fails with `KeyError: 'DJANGO_SECRET_KEY'`.
`osds-manage` builds the same environment the entrypoint does, as the app
database role, and then runs `manage.py` with your arguments. It works in
`osds-app` and `osds-worker`. Anything you set in `.env` still takes precedence.

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

`OSDS_TRUSTED_PROXIES` is also what makes the app believe `X-Forwarded-Proto`.
**If TLS ends at your proxy and the proxy's address is not listed, every form
POST over HTTPS (sign-in, claims, leads) fails with a 403 CSRF error**, and the
app logs a warning naming the address. Where the proxy's address is not stable
(Railway, Render), list a broad private range such as `10.0.0.0/8`.

If the proxy issues certificates on demand (Caddy's `on_demand_tls`), point its
`ask` at `http://<app>:8000/_internal/tls-ask`. The app answers 200 only for a
verified tenant domain or the console host, and only to a listed proxy that adds
no `X-Forwarded-*` headers to that call. Your proxy must also:

- answer 404 for `/_internal/*` on its public listener, and
- serve `/.well-known/osds-challenge` over plain HTTP **without redirecting it
  to HTTPS**. Domain verification fetches that URL over HTTP; a redirect needs a
  certificate the app will not approve until the domain is verified.

### HTTPS on a server
For a server with a public address, an optional overlay adds Caddy, which owns
ports 80 and 443, gets certificates from Let's Encrypt on demand, and passes the
app only the names it approves. Everything in the section above is already set
for you. The default `docker-compose.yml` is unchanged; this is opt-in.

1. **DNS.** Point an A (and AAAA, if you have IPv6) record for the hostname at
   the server, and open ports 80 and 443. Add the same records for each tenant
   domain later, as you verify them.
2. **Name the console.** `OSDS_CONSOLE_HOST` must be a real, public hostname
   with at least one dot, such as `directory.example.com`, set in your shell or
   `.env`. Not `localhost`, not an IP address: the app will not approve a
   certificate for either, and the overlay refuses to start without it.
3. **Start it.**

       docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d --build

Then open `https://<your hostname>`. The first request for a name takes a few
seconds while its certificate is issued. The app is no longer published on port
8000; Caddy is the only way in. Certificates live in the `caddy-data` volume, so
keep it with your backups. The overlay needs Docker Compose 2.24 or later, and
uses the address range `172.28.77.0/24`; if that clashes with a network on your
host, see decisions.md §4.13.

## Documentation
- [Specification](docs/spec/events-and-adapters.md)
- [Receiving webhooks](docs/webhook.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License
Apache-2.0. Stewarded by Origin Development & IT, Inc.
