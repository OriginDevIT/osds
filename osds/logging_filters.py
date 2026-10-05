"""Log filters (#179, decisions.md §4.12).

``MaskSecretPaths`` hides a credential that travels in a URL path. Today that is
the owner sign-in token (``/owner/signin/<token>/``, decisions.md §4.9) and the
operator invitation token (``/invite/<token>/``, §4.14): single use, but an
unused one would otherwise sit in every access log and every ``Not Found:
<path>`` warning. A token is long and unguessable, so a long run of URL-safe
characters after the prefix is masked; ``/owner/signin/`` and
``/owner/signin/sent/`` are left alone.

The same masking runs on gunicorn's access log (``osds.gunicorn_logging``) and,
through the handler filter in ``settings.LOGGING``, on every ``osds.*`` and
``django.*`` record.
"""

from __future__ import annotations

import logging
import re

_SECRET_PATHS = re.compile(r"(/(?:owner/signin|invite)/)[A-Za-z0-9_\-]{20,}")

MASK = "[masked]"


def mask_secret_paths(text: str) -> str:
    return _SECRET_PATHS.sub(rf"\1{MASK}", text)


class MaskSecretPaths(logging.Filter):
    """Rewrites a record's message in place; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is not ours to fail on
            return True
        masked = mask_secret_paths(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True
