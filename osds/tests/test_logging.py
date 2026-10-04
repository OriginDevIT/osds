"""Logging (#179, decisions.md §4.12): secret URL paths masked, the adapter
logger's scrubbing, and the project LOGGING configuration.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from django.conf import settings
from unittest import skipUnless

from django.test import SimpleTestCase

from osds.adapter_logging import REDACTED, ScrubbingLogger, make_adapter_logger
from osds.logging_filters import MASK, MaskSecretPaths, mask_secret_paths

TOKEN = "Zx9-aBcDeFgHiJkLmNoPqRsTuVwXyZ_0123456789abc"


class MaskTests(SimpleTestCase):
    def test_a_sign_in_token_in_a_path_is_masked(self):
        self.assertEqual(
            mask_secret_paths(f"GET /owner/signin/{TOKEN}/ HTTP/1.1"),
            f"GET /owner/signin/{MASK}/ HTTP/1.1",
        )

    def test_a_token_with_a_query_or_in_a_referer_is_masked(self):
        self.assertNotIn(TOKEN, mask_secret_paths(f"/owner/signin/{TOKEN}?next=/x"))
        self.assertNotIn(TOKEN, mask_secret_paths(f"https://acme.test/owner/signin/{TOKEN}/"))

    def test_the_sign_in_pages_themselves_are_left_alone(self):
        for path in ("/owner/signin/", "/owner/signin/sent/", "/owner/leads/", "/owner/"):
            self.assertEqual(mask_secret_paths(path), path)

    def test_several_tokens_in_one_line(self):
        out = mask_secret_paths(f"/owner/signin/{TOKEN} and /owner/signin/{TOKEN}")
        self.assertNotIn(TOKEN, out)
        self.assertEqual(out.count(MASK), 2)

    def test_the_filter_rewrites_a_record_in_place_and_never_drops_one(self):
        record = logging.LogRecord(
            "django.request", logging.WARNING, __file__, 1, "Not Found: %s", (f"/owner/signin/{TOKEN}/",), None
        )
        self.assertTrue(MaskSecretPaths().filter(record))
        self.assertEqual(record.getMessage(), f"Not Found: /owner/signin/{MASK}/")

    def test_a_malformed_record_passes_through(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("not a number",), None)
        self.assertTrue(MaskSecretPaths().filter(record))

    def test_the_project_handler_applies_it(self):
        handlers = [h for h in logging.getLogger().handlers if any(isinstance(f, MaskSecretPaths) for f in h.filters)]
        self.assertTrue(handlers, "no root handler carries the masking filter")

    def test_the_logging_config_has_a_console_handler_and_an_osds_level(self):
        self.assertIn("console", settings.LOGGING["handlers"])
        self.assertEqual(settings.LOGGING["loggers"]["osds"]["level"], "INFO")
        self.assertEqual(logging.getLogger("osds").level, logging.INFO)


def _gunicorn_importable() -> bool:
    try:
        import gunicorn.config  # noqa: F401  (needs the Unix-only grp module)
    except ImportError:
        return False
    return True


@skipUnless(_gunicorn_importable(), "gunicorn needs a Unix platform to import")
class GunicornMaskingTests(SimpleTestCase):
    def test_every_string_atom_is_masked(self):
        from gunicorn.config import Config

        from osds.gunicorn_logging import MaskingLogger

        logger = MaskingLogger(Config())
        req = SimpleNamespace(
            method="GET", path=f"/owner/signin/{TOKEN}/", query="", version=(1, 1),
            headers=[("REFERER", f"https://acme.test/owner/signin/{TOKEN}/"), ("USER-AGENT", "t")],
            uri=f"/owner/signin/{TOKEN}/",
        )
        resp = SimpleNamespace(status="200 OK", status_code=200, sent=10, headers=[("Content-Type", "text/html")])
        from datetime import timedelta

        path = f"/owner/signin/{TOKEN}/"
        environ = {
            "REMOTE_ADDR": "127.0.0.1", "wsgi.url_scheme": "http", "REQUEST_METHOD": "GET",
            "RAW_URI": path, "PATH_INFO": path, "QUERY_STRING": "", "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_REFERER": f"https://acme.test{path}", "HTTP_USER_AGENT": "t",
        }
        atoms = logger.atoms(resp, req, environ, timedelta(seconds=0.01))
        self.assertNotIn(TOKEN, " ".join(str(v) for v in atoms.values()))
        self.assertIn(MASK, atoms["U"])
        self.assertIn(MASK, atoms["r"])
        self.assertIn(MASK, atoms["f"])  # the Referer


class ScrubbingLoggerTests(SimpleTestCase):
    def logger(self, secrets):
        return ScrubbingLogger(logging.getLogger("osds.adapters.test"), secrets)

    def test_a_secret_is_removed_from_the_message_and_the_arguments(self):
        log = self.logger({"s3cr3t-token"})
        with self.assertLogs("osds.adapters.test", "INFO") as logs:
            log.info("using %s then s3cr3t-token again", "s3cr3t-token")
        self.assertEqual(logs.output, [f"INFO:osds.adapters.test:using {REDACTED} then {REDACTED} again"])

    def test_the_longest_secret_goes_first(self):
        log = self.logger({"abc", "abcdef"})
        with self.assertLogs("osds.adapters.test", "INFO") as logs:
            log.info("x abcdef y")
        self.assertEqual(logs.output[0], f"INFO:osds.adapters.test:x {REDACTED} y")

    def test_a_secret_added_later_is_scrubbed_from_then_on(self):
        secrets: set = set()
        log = self.logger(secrets)
        with self.assertLogs("osds.adapters.test", "INFO") as logs:
            log.info("before late-secret")
            secrets.add("late-secret")
            log.info("after late-secret")
        self.assertIn("late-secret", logs.output[0])
        self.assertNotIn("late-secret", logs.output[1])

    def test_an_empty_secret_is_ignored(self):
        log = self.logger({""})
        with self.assertLogs("osds.adapters.test", "INFO") as logs:
            log.info("plain text")
        self.assertEqual(logs.output[0], "INFO:osds.adapters.test:plain text")

    def test_exception_logs_no_traceback(self):
        log = self.logger({"hunter2"})
        with self.assertLogs("osds.adapters.test", "ERROR") as logs:
            try:
                raise RuntimeError("boom hunter2")
            except RuntimeError:
                log.exception("it failed")
        record = logs.records[0]
        self.assertIsNone(record.exc_info)
        self.assertNotIn("hunter2", record.getMessage())
        self.assertNotIn("Traceback", logs.output[0])

    def test_a_bad_format_string_does_not_raise(self):
        log = self.logger({"x"})
        with self.assertLogs("osds.adapters.test", "INFO"):
            log.info("%d items", "many")

    def test_every_level_is_scrubbed(self):
        log = self.logger({"hunter2"})
        with self.assertLogs("osds.adapters.test", "DEBUG") as logs:
            for method in (log.debug, log.info, log.warning, log.error, log.critical):
                method("hunter2")
        self.assertTrue(all("hunter2" not in line for line in logs.output))
        self.assertEqual(len(logs.output), 5)

    def test_a_disabled_level_formats_nothing(self):
        log = self.logger({"x"})
        marker = object.__new__(type("Boom", (), {"__str__": lambda self: (_ for _ in ()).throw(AssertionError("formatted"))}))
        logging.getLogger("osds.adapters.test").setLevel(logging.ERROR)
        self.addCleanup(logging.getLogger("osds.adapters.test").setLevel, logging.NOTSET)
        log.debug(marker)  # level off: must not even stringify

    def test_the_factory_names_the_logger_after_the_adapter(self):
        with self.assertLogs("osds.adapters.hook", "INFO") as logs:
            make_adapter_logger("hook", set()).info("hi")
        self.assertEqual(logs.records[0].name, "osds.adapters.hook")
