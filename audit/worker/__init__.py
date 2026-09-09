"""The worker.

``drain.py`` fans the outbox out and attempts deliveries; ``tick.py`` holds the
scheduled-job registry; ``loop.py`` is the single pass and the ``while True``
around it; ``listen.py`` is LISTEN/NOTIFY with a poll fallback; ``singleton.py``
is the advisory-lock single-instance guard; ``dbconn.py`` opens the raw
connections the last two hold outside Django's connection lifecycle. The
``run_worker`` management command wires them together.

Nothing in this package imports adapter code -- it reads the registry seam in
``osds.adapters``, empty until mvp-plan block 5 -- and nothing uses ``.objects``
on a tenant-scoped model: the drain iterates with ``all_tenants`` and enters
each delivery's tenant explicitly. A test enforces both, for this package and
for the command.
"""
