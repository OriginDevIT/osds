# Installing OSDS

The operator's path from an empty server to a directory that takes payments.
This is an **alpha** (v0.1): it runs end to end, but expect rough edges and read
"What is not in the alpha" at the end of this page before you plan around it.

Where this page names a file or section, that is where to look when something
behaves differently from what is written here.

## Requirements

- A machine that runs Docker with **Docker Compose**. The plain install needs
  any recent Compose; the HTTPS overlay needs **2.24 or later** (it uses `!reset`).
- Git, to clone and to upgrade.
- For a public install: a server with a public address, **two** hostnames you
  control (the console host and the directory's own domain, below), and ports
  80 and 443 open.
- Nothing else. Postgres 16 runs in a container; no Redis, no queue broker, no
  external search engine.

You will use two kinds of hostname, and they must differ. The **console host**
(`OSDS_CONSOLE_HOST`) serves the first-run wizard and the operator console. Each
**directory** is served on its own domain, set in the wizard. If a request's
host matches the console host it is the console, even if a directory claims the
same name (`osds/middleware.py`).

## Get the code

    git clone https://github.com/OriginDevIT/osds.git
    cd osds

## Configuration

**A fresh install needs no `.env` file.** On first boot the `postgres` container
generates the install's secrets (`DJANGO_SECRET_KEY`, `OSDS_SECRET_KEY` and the
two database passwords) into the `osds-secrets` volume, and the app reads them
from there (`docker/postgres-entrypoint.sh`, `docker/entrypoint.sh`). A `.env`
file is for overrides only; copy `.env.example`, which has every line commented
out.

The variables an operator is likely to set:

| Variable | What it does |
| --- | --- |
| `OSDS_CONSOLE_HOST` | The hostname you browse to for the wizard and console. A name only, no port. Defaults to `localhost`. **Required, and must be a real public name, for the HTTPS overlay.** |
| `OSDS_SECURE_COOKIES` | Defaults to `false` in `docker-compose.yml` because a fresh install is plain HTTP. The HTTPS overlay sets it `true`. If you terminate TLS yourself, set it `true`. Left `true` on an HTTP-only install, no login POST can pass CSRF. |
| `OSDS_TRUSTED_PROXIES` | Addresses or CIDRs of reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed. See "Behind another proxy". |
| `OSDS_PORT` | Host port for the app. Defaults to `8000`. |
| `GUNICORN_WORKERS` | Defaults to `3`. |
| `OSDS_WEBHOOK_ALLOW_PRIVATE` | Lets the webhook adapter reach private-network addresses. Defaults to `false`. Loopback and the cloud metadata address stay blocked either way. |
| `DJANGO_SECRET_KEY`, `OSDS_SECRET_KEY`, `DATABASE_URL`, `DATABASE_URL_ADMIN` | Bring your own secrets or database. A value set in `.env` wins over the generated one. Do not change `OSDS_SECRET_KEY` on a running install (see "Backups"). |

The full list of environment variables, and what each does, is in the
"Commands" section of [CLAUDE.md](../CLAUDE.md#commands); this page does not
restate it. `DJANGO_DEBUG`, `OSDS_DEV_TENANT_SLUG` and `OSDS_MEDIA_ROOT` are for
development and are already set correctly inside the container.

`docker compose` reads `.env` for the app and worker (`env_file` in
`docker-compose.yml`). A stale `.env` left over from another version can
override the generated `DATABASE_URL` and stop the app starting: if the app
logs `database unreachable after 30 attempts`, check `.env` first.

## Start it

    docker compose up -d --build

Four things run: `postgres`, `osds-app`, `osds-worker`, and the `osds-media`
volume that stands in for object storage (local disk is the only storage
backend in this release). The app migrates the database as the Postgres owner,
then serves as the least-privilege `osds_app` role; the worker starts after the
app reports healthy.

## The setup token

On the first start the app mints a one-time token and prints it to its log:

    docker compose logs osds-app

Look for `OSDS first-run setup token:`. It is minted once and never printed
again. The server stores only a hash. If you lose it before finishing the
wizard, there is no re-issue command; see "If you lose the setup token" below.

## The wizard

Open `http://<your host>:8000` (or `http://localhost:8000`). Every URL redirects
to `/setup/` until the wizard finishes. Paste the token, then:

1. **Superadmin.** Your email, name and password.
2. **Directory.** The first directory's name, slug, and whether it is a single
   or multi-directory install.
3. **Domain.** The hostname the directory is served on. See "DNS and domain
   verification".
4. **Storage.** Choose **Local disk**. S3, Azure and GCP appear in the list but
   are not implemented in this release; uploads on a tenant that selects one
   fail with a deferred-feature error (decisions.md §4, "No object-storage
   container at launch").
5. **Mail (SMTP).** Enter your outgoing mail server, or **Skip** and configure
   it later. See "Mail".
6. **Claim verification.** Which claim methods to enable. `manual` review is
   always available; `domain_email` needs working mail.
7. **Finish.** The route disappears once you confirm; the wizard cannot be
   reopened.

You can close the browser and return: progress is derived from what is saved,
not stored in a session (`tenants/setup_state.py`).

Afterwards, sign in to the directory's admin at
`https://<directory domain>/admin/`. Operators and tenants are managed in the
console at `/` on the console host (Django admin is mounted at `/admin/` there).
The directory admin has pages for listing types, categories, listings, CSV
imports (`/admin/imports/`), claims, leads, tiers, and settings.

## DNS and domain verification

In the wizard's domain step, create an **A** record (and **AAAA** if your server
has IPv6) for the directory's domain pointing at your server. Then click **Save
& verify**.

Verification does not read DNS records. The app fetches
`http://<domain>/.well-known/osds-challenge` and checks the response is the
token the wizard shows (`tenants/dns_check.py`). A pass proves the name resolves
**and** reaches this installation. Two things follow:

- The check is over **plain HTTP on port 80**. If a proxy in front redirects
  HTTP to HTTPS before the app is verified, verification can never succeed. The
  bundled Caddy recipe handles this; a proxy of your own must too (below).
- If the check cannot pass yet (DNS still propagating, a laptop install), click
  **Save & continue** and verify later. The directory is served on the domain
  regardless, but until it is verified: sign-in links and claim codes by email
  carry no absolute URL, owner sign-in is unavailable, payments are unavailable,
  and the sitemap is not published (decisions.md §4.9, §4.11, §4.1).

Changing a directory's domain clears its verification.

## HTTPS

An optional overlay adds Caddy, which obtains Let's Encrypt certificates on
demand, only for the console host and verified directory domains. Follow
**[HTTPS on a server](../README.md#https-on-a-server)** in the README: DNS for the
console host, `OSDS_CONSOLE_HOST` set to a real public name, then

    docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d --build

Use the same two `-f` flags on every later `docker compose` command for this
install (`ps`, `logs`, `up`, `down`). Add DNS records for each directory domain
as you create it; its certificate is issued on the first request after the
domain is verified. Certificates live in the `caddy-data` volume.

## Behind another proxy

If you terminate TLS with something other than the bundled Caddy (nginx,
Traefik, a cloud load balancer), set `OSDS_TRUSTED_PROXIES` to the proxy's
address or CIDR, for example `OSDS_TRUSTED_PROXIES=172.18.0.0/16`, and set
`OSDS_SECURE_COOKIES=true`.

Without it the app ignores `X-Forwarded-For` (every visitor looks like the
proxy, and per-IP claim rate limits become one shared limit) **and ignores
`X-Forwarded-Proto`, so every form POST over HTTPS fails with a 403 CSRF error.**
The app logs a warning naming the address it saw. Where the proxy's address is
not stable (Railway, Render), list a broad private range such as `10.0.0.0/8`.
The reasoning is in decisions.md §4.8 and §4.13; the proxy's duties (answer 404
for `/_internal/*`, serve `/.well-known/osds-challenge` over HTTP without
redirecting it, and the on-demand certificate `ask` endpoint) are listed in the
README under "Behind a reverse proxy".

## Mail

Mail is optional, but several things depend on it: email claim verification,
the owner's sign-in link, lead and claim notices to operators, billing notices,
and operator invitation links. Without mail, claims fall back to manual review
and an unconfigured-mail banner shows on every directory admin page.

Configure it in the wizard, or later at `/admin/settings/mail/` on the
directory's domain (the highest tenant role only). Choose the security mode to
match your provider's port: `starttls` (usually 587), `tls` (usually 465) or
`none` (25). Certificates are always verified, and a username is refused with
`none`, so credentials never travel in the clear. The password is stored
encrypted with `OSDS_SECRET_KEY`.

Mail is sent by the **worker** (`osds-worker`), not the web request. If mail is
configured but nothing arrives, check `docker compose logs osds-worker`.

## Payments (Stripe)

Payments need a **verified domain served over HTTPS**: Stripe will not call a
plain-HTTP webhook, and the payments settings page reports payments unavailable
until the directory has a verified domain (decisions.md §4.11). Do test mode
first.

1. In **Tiers** (`/admin/tiers/`), create at least one purchasable tier: a price
   above zero, a currency and a billing interval of `month` or `year`.
   Consider a free rank-0 tier too: without one, an expired entitlement hides
   the listing instead of dropping it to the free tier.
2. In Stripe (test mode), create a Product and a recurring Price whose amount,
   currency and interval **exactly match** the tier. OSDS refuses to check out
   if they differ.
3. In **Settings → Payments** (`/admin/settings/payments/`, admin only), enter:
   - **Secret key**: an `sk_test_…` key, or a restricted `rk_test_…` key.
   - **Price ids**: `tier-key=price_…`, comma-separated, for example
     `featured=price_1ABC, verified=price_1DEF`.
   - The page shows this directory's webhook URL,
     `https://<directory domain>/_adapters/stripe/inbound/`.
4. In Stripe, add a webhook endpoint at that URL for these events:
   `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `charge.refunded`. Copy its `whsec_…` signing secret into **Webhook signing
   secret** and save.
5. Run [docs/stripe-test-mode-checklist.md](stripe-test-mode-checklist.md) end
   to end. It is the only check against Stripe's real API shapes; the automated
   tests fake them. Do not skip it.
6. **Going live:** replace the secret key with `sk_live_…` (or `rk_live_…`),
   create live-mode Prices and a live webhook endpoint, and update **Price ids**
   and **Webhook signing secret** to the live values. Each directory configures
   its own keys and webhook; nothing is shared between them.

## Adding operators

Operators are people who administer directories. In the console (`/admin/` on
the console host), add them under **Staff memberships**, choosing the directory
and a role. A new email address gets an operator account and, if the directory
has working mail, a set-password link valid for seven days. An operator added
directly under **Operators** gets no invitation.

If mail is not configured, the link has expired, or the operator was created
without an invitation, issue a link from the host:

    docker compose exec osds-app osds-manage issue_operator_invite --email person@example.com

It prints a one-time set-password link and sends nothing. It refuses an unknown
operator, an inactive one, and one who already has a password. On an HTTP-only
console it prints only the path (`/invite/<token>/`) to open on that host.

Run every `manage.py` command in the container this way, with `osds-manage`
rather than `python manage.py`; see
[Running management commands](../README.md#running-management-commands) in the
README.

## If you lose the setup token

First look for it in the log, where it was printed at the first start:

    docker compose logs osds-app

(`osds-manage ensure_setup_token` will not help: it never re-mints or reprints a
token that already exists.) If the log no longer has it and you have **not**
finished the wizard, the install has no data worth keeping, so the fallback is
to start over with `docker compose down -v` (which deletes the database, media
and generated secrets) and `docker compose up -d --build`. Do not do this on an
install that has finished the wizard.

## Backups

Three things hold state. Back up all three, and keep the backups together.

| What | Where | Why it matters |
| --- | --- | --- |
| **`osds-secrets` volume** | Holds `OSDS_SECRET_KEY`, `DJANGO_SECRET_KEY` and the database passwords. | `OSDS_SECRET_KEY` encrypts the credentials entered through the console (SMTP password, Stripe keys, the webhook URL). Without it they cannot be recovered and must be entered again. A new `osds-secrets` volume will not work against an existing database volume. |
| **Postgres** | `osds-pgdata` volume | All directory data. |
| **`osds-media` volume** | Uploaded images and CSV files. | Shared by the app and worker. |

With the HTTPS overlay, also keep `caddy-data`: losing it means reissuing every
certificate, which runs into Let's Encrypt rate limits.

Volume names are prefixed with the Compose project name, which defaults to the
name of the directory you cloned into (`docker volume ls` shows them).

**Postgres.** Dump with `pg_dump` inside the container and copy the file out.
Do not redirect `pg_dump` output with `>` in PowerShell: it writes UTF-16 and
corrupts the dump.

    docker compose exec postgres pg_dump -U osds -d osds -Fc -f /tmp/osds.dump
    docker compose cp postgres:/tmp/osds.dump ./osds-backup.dump

Name the file with the date so a new backup does not overwrite the last. These
two commands were run against a fresh install while writing this page; the
restore was not.

**Volumes.** Archive `osds-secrets` and `osds-media` with any volume backup
method you already use, or with a throwaway container, for example:

    docker run --rm -v <project>_osds-media:/data:ro -v "$PWD":/backup alpine tar czf /backup/osds-media.tgz -C /data .

**Restoring** needs the original `osds-secrets` volume first: start only the
`postgres` service so the database role is created with the restored password,
then `pg_restore -U osds -d osds --clean --if-exists` the dump, restore the
media volume, and then start the rest. The app re-applies its table grants on
every start. Treat this as an outline and rehearse it on a spare machine before
you rely on it.

Never run `docker compose down -v` on an install you want to keep: `-v` deletes
the volumes.

## Upgrading

Back up first (above). Migrations are forward-only; going back means restoring.

    git pull
    docker compose up -d --build

(Add the `-f` flags for the HTTPS overlay if you use it.) The app container
runs `migrate` and re-applies the application role's grants before it starts
serving; the worker starts after the app is healthy. Compose rebuilds only if
the code changed. The Caddy image is pinned to an exact tag in
`docker-compose.tls.yml`; changing it is a deliberate edit.

Read the release notes before upgrading across a version. In this alpha there
is no stable upgrade promise between releases.

## What is not in the alpha

- Object storage: S3, Azure and GCP are not implemented; media is local disk.
- Featured slots, native reviews, phone OTP / postcard / Google Business Profile
  claim verification, AI agents, and Postgres row-level security.
- Any data-source connector, now or later (CLAUDE.md invariant 4).

See [mvp-plan.md](mvp-plan.md) for what shipped and what is deferred, and
[decisions.md](decisions.md) for why.
