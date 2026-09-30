"""Static files are served with DEBUG off (#123).

Django's test runner forces DEBUG=False, so these exercise the production path:
collectstatic into STATIC_ROOT, served by whitenoise. Before the middleware was
wired, the vendored htmx 404'd here and the admin schema builder silently lost
its JavaScript.
"""

from __future__ import annotations

import tempfile

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from tenants.models import InstallSetup

HTMX = "/static/directory/vendor/htmx-2.0.4.min.js"
CONSOLE = "console.example.test"


@override_settings(OSDS_CONSOLE_HOST=CONSOLE, ALLOWED_HOSTS=["*"])
class StaticServingTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows holds served files open
        self.addCleanup(root.cleanup)
        self.enterContext(override_settings(STATIC_ROOT=root.name))
        call_command("collectstatic", interactive=False, verbosity=0)
        # Built after collectstatic: whitenoise indexes STATIC_ROOT when the
        # client's handler first loads middleware.
        self.client = Client()

    def _release(self, response):
        # Not response.close(): it fires request_finished, which closes the
        # test database connection mid-TestCase. Closing the file is enough
        # for Windows to let the temp directory go.
        self.addCleanup(lambda: getattr(response, "file_to_stream", None) and response.file_to_stream.close())

    def test_whitenoise_follows_security_middleware(self):
        mw = settings.MIDDLEWARE
        self.assertEqual(
            mw.index("whitenoise.middleware.WhiteNoiseMiddleware"),
            mw.index("django.middleware.security.SecurityMiddleware") + 1,
        )

    def test_htmx_is_served_during_first_run_setup_on_any_host(self):
        response = self.client.get(HTMX, HTTP_HOST="203.0.113.7")
        self._release(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertIn(b"htmx", b"".join(response.streaming_content))

    def test_htmx_is_served_on_the_console_host_after_setup(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        response = self.client.get(HTMX, HTTP_HOST=CONSOLE)
        self._release(response)
        self.assertEqual(response.status_code, 200)

    def test_a_missing_static_file_is_a_404_not_a_500(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        response = self.client.get("/static/nope.js", HTTP_HOST=CONSOLE)
        self.assertEqual(response.status_code, 404)
