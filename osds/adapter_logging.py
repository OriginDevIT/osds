"""The adapter logger: ``ctx.logger`` (spec §8.1, #179).

Secrets "are redacted by ``ctx.logger`` unconditionally". That is a property of
the logger, not of the code calling it, so it holds without the adapter doing
anything: every secret the adapter has resolved through ``ctx.secret`` is
recorded here, and any occurrence of one in a message is replaced before the
record is created. An adapter cannot log a resolved secret by accident, and it
cannot opt out.

Two further rules are built in:

* **No tracebacks.** ``exception`` logs at ERROR without ``exc_info``: a
  traceback prints local variables' reprs and exception text, which for an HTTP
  or SMTP error can echo a URL, an address or a credential. Log the class.
* **Contact data is still the adapter's to keep out of messages** (spec §8.3).
  The scrubber knows secrets, not names and addresses; core cannot redact what
  it cannot recognise.
"""

from __future__ import annotations

import logging

REDACTED = "[redacted]"


class ScrubbingLogger:
    def __init__(self, logger: logging.Logger, secrets: "set[str]"):
        self._logger = logger
        self._secrets = secrets  # shared with the context, filled as secrets resolve

    def _scrub(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, REDACTED)
        return text

    def _log(self, level: int, msg, args) -> None:
        if not self._logger.isEnabledFor(level):
            return
        try:
            text = str(msg) % args if args else str(msg)
        except (TypeError, ValueError):
            text = f"{msg} {args!r}"
        self._logger.log(level, "%s", self._scrub(text))

    def debug(self, msg, *args) -> None:
        self._log(logging.DEBUG, msg, args)

    def info(self, msg, *args) -> None:
        self._log(logging.INFO, msg, args)

    def warning(self, msg, *args) -> None:
        self._log(logging.WARNING, msg, args)

    def error(self, msg, *args) -> None:
        self._log(logging.ERROR, msg, args)

    def exception(self, msg, *args) -> None:
        self._log(logging.ERROR, msg, args)

    def critical(self, msg, *args) -> None:
        self._log(logging.CRITICAL, msg, args)


def make_adapter_logger(adapter_id: str, secrets: "set[str]") -> ScrubbingLogger:
    return ScrubbingLogger(logging.getLogger(f"osds.adapters.{adapter_id}"), secrets)
