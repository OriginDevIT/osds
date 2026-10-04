"""Gunicorn's access log with secret URL paths masked (#179).

Selected with ``--logger-class osds.gunicorn_logging.MaskingLogger`` in the
container entrypoint. Every string atom is masked, not just the path: the
request line, the query and the ``Referer`` can all carry the same segment.
"""

from __future__ import annotations

from gunicorn.glogging import Logger

from osds.logging_filters import mask_secret_paths


class MaskingLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {
            key: mask_secret_paths(value) if isinstance(value, str) else value
            for key, value in atoms.items()
        }
