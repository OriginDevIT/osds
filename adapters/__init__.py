"""Vendor integrations, one package each (CLAUDE.md).

Nothing under ``directory/``, ``tenants/``, ``billing/`` or ``audit/`` may
import from here (invariant 1); the only sanctioned import site is an
``AppConfig.ready()`` inside an adapter's own Django app, wired through
``INSTALLED_APPS`` -- never a core module reaching in directly.
"""
