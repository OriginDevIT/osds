"""The worker.

The outbox drain lives here (this PR); the tick loop and the ``run_worker``
command follow. Nothing in this package imports adapter code -- it reads the
registry seam in ``osds.adapters``, which is empty until mvp-plan block 5 --
and nothing uses ``.objects`` on a tenant-scoped model: the drain iterates
with ``all_tenants`` and enters each delivery's tenant explicitly.
"""
