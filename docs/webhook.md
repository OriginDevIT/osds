# The webhook adapter: receiving events

OSDS can POST a directory's events, signed, to one URL you choose. Set it up at
**Admin → Integrations → webhook**. This page is for whoever writes the receiver.
The decisions behind it are in `docs/decisions.md` §4.12.

## What you receive

A `POST` with a JSON body, one event per request:

```json
{
  "id": "01JC8Z...",
  "type": "lead.captured",
  "version": 1,
  "occurred_at": "2026-10-03T12:00:00.000Z",
  "subject": "lead_01JC8Z...",
  "actor": {"type": "visitor", "id": ""},
  "origin": null,
  "trace_id": "01JC8Z...",
  "tenant": {"id": "tnt_01...", "slug": "acme", "domain": "acme.example.com"},
  "data": {"lead": {"id": "lead_01...", "kind": "contact_form", "name": null, "email": null, "phone_e164": null, "message": null}, "listing_id": "listing_01..."},
  "redacted": ["lead.email", "lead.message", "lead.name", "lead.phone_e164", "consent.contact_by_business.ip"]
}
```

Headers:

| Header | Meaning |
| --- | --- |
| `X-OSDS-Event-Id` | The event's `id`. **Use it to deduplicate**: delivery is at-least-once. |
| `X-OSDS-Event-Type` | The event type. |
| `X-OSDS-Timestamp` | Unix seconds when the request was signed. |
| `X-OSDS-Signature` | `t=<unix>,v1=<hex>` — see below. |

## Personal data is withheld unless you allow it

By default every name, email address, phone number, IP address and piece of
free text is `null`, and `redacted` lists the paths that were withheld, so you
can tell "withheld" from "absent". On the integration's settings page you can
allow two things, separately:

- **Contact details** — names, emails, phone numbers, IP addresses, a listing's
  contact block, and custom fields marked non-public.
- **Message text** — what visitors wrote, and the notes and reasons staff type.

Allowing one releases it for every event you subscribe to. Turning a permission
on or off is recorded in the directory's event log.

## Verifying the signature

`v1` is HMAC-SHA256, hex, keyed with your signing secret, over the string
`"<t>.<raw request body>"` — the body **exactly as received**, not re-serialised.
Reject a request whose `t` is more than five minutes from your clock.

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        t, offered = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(offered, expected)
```

The signing secret is generated for you the first time you save an endpoint and
shown **once**. **Rotate** replaces it immediately; update your receiver first or
accept a short gap.

## Answering

| You reply | OSDS does |
| --- | --- |
| `2xx` | Delivered. |
| `409` | Treats it as a duplicate you already have: delivered. |
| `429` with `Retry-After: <seconds>` | Retries after at least that long (at most one hour). |
| `410` | Gives up on this event at once (it appears under *Gave up*). |
| any other `4xx`, `5xx`, a timeout, a redirect | Retries with growing, jittered delays up to one hour, 12 attempts in all, then gives up. Redirects are **not** followed. |

Answer within 25 seconds. Events for the same subject are delivered in order: a
failing event holds later ones for that subject until it succeeds or gives up.
Events that gave up can be sent again from the **Deliveries** page, unless their
details have been removed after 90 days.

## Where it can reach

HTTPS to a host that resolves to a public address. A private-network address
(your own LAN, a Docker network) is refused unless the deployment sets
`OSDS_WEBHOOK_ALLOW_PRIVATE=true`, which also allows plain HTTP to such an
address. Loopback and the cloud metadata address (`169.254.169.254`) are never
reachable.
