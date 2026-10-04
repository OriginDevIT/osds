"""The bundled ``stripe`` payment adapter (spec §8.6, decisions.md §4.11).

stdlib only -- ``urllib`` through ``AdapterContext.http`` and ``hmac`` -- with no
Stripe SDK, registered by its ``AppConfig.ready()`` (``adapters.stripe.apps``).
It imports from core only ``osds.adapter_api`` and ``osds.adapters``; a test
fails if it imports ``billing``, ``directory``, ``tenants`` or ``audit``.
"""
