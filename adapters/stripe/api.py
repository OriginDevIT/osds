"""A very small Stripe REST client over ``AdapterContext.http``.

Stripe's API takes ``application/x-www-form-urlencoded`` bodies and answers
JSON, authenticated with ``Authorization: Bearer <secret key>``. All traffic
goes through ``ctx.http``, which only reaches the hosts the provider declares
(``api.stripe.com``), over HTTPS, without following redirects.

Errors raise ``PaymentError`` carrying Stripe's error *type* and *code* and the
HTTP status, never the response body or message: those can echo request
parameters, and nothing here is worth logging a customer's detail for.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from osds.adapter_api import PaymentError

HOST = "api.stripe.com"
BASE = f"https://{HOST}/v1"


class StripeNotFound(PaymentError):
    """A 404 ``resource_missing``: the object does not exist (any more)."""


def _flatten(params: dict, prefix: str = "") -> "list[tuple[str, str]]":
    """Stripe's nested form encoding: ``a[b][0][c]=v``."""
    out: "list[tuple[str, str]]" = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", _scalar(item)))
        elif value is not None:
            out.append((name, _scalar(value)))
    return out


def _scalar(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def encode(params: dict) -> bytes:
    return urlencode(_flatten(params)).encode("ascii")


class StripeClient:
    def __init__(self, ctx):
        self._ctx = ctx

    def request(self, method: str, path: str, *, params: "dict | None" = None,
                idempotency_key: str = "") -> dict:
        headers = {
            "Authorization": f"Bearer {self._ctx.secret('secret_key')}",
            "Accept": "application/json",
        }
        body = None
        url = f"{BASE}/{path.lstrip('/')}"
        if params and method in ("GET", "DELETE"):
            url = f"{url}?{encode(params).decode('ascii')}"
        elif params:
            body = encode(params)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._ctx.http(method, url, headers=headers, body=body)
        data = _json(response.body)
        if 200 <= response.status < 300:
            if not isinstance(data, dict):
                raise PaymentError("stripe returned a body that is not an object")
            return data
        error = data.get("error", {}) if isinstance(data, dict) else {}
        kind, code = error.get("type", "unknown"), error.get("code", "")
        message = f"stripe {response.status} {kind} {code}".strip()
        if response.status == 404 and code == "resource_missing":
            raise StripeNotFound(message)
        raise PaymentError(message)

    def get(self, path: str, params: "dict | None" = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, params: dict, *, idempotency_key: str = "") -> dict:
        return self.request("POST", path, params=params, idempotency_key=idempotency_key)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def path_id(prefix: str, value: str) -> str:
    """A path segment that is an id of the given prefix and nothing else, so an
    id from a stored reference can never reach another endpoint."""
    import re

    if not re.fullmatch(rf"{re.escape(prefix)}_[A-Za-z0-9]+", value or ""):
        raise PaymentError(f"not a valid {prefix} id")
    return quote(value, safe="")
