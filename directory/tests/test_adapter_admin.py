"""The adapter pages of the tenant admin (decisions.md §4.12): settings,
deliveries, dead letters and replay. Admin only; a secret is shown once and is
in no event, log or page after that.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest import mock

from django.test import Client, override_settings
from django.utils import timezone

from audit import events
from audit.models import CommandLog, OutboxDelivery, OutboxEvent
from audit.outbox import emit
from directory.tests.lead_base import HOST, LeadBase, Role
from osds.adapter_api import HttpResponse
from osds.adapter_logging import make_adapter_logger
from tenants.models import Tenant
from tenants.secrets import get_secret, has_secret

class FakeHttp:
    """Stands in for ``ctx.http``. (Defined here, not imported from the adapter's
    tests: core tests never import adapter code.)"""

    def __init__(self, response=None):
        self.response = response or HttpResponse(200, b"ok")
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url))
        return self.response


def fake_ctx(*, http, config=None):
    secrets = {"url": "https://hooks.example.test/in/T0K3N", "signing_secret": "whsec_" + "ab" * 32}
    return SimpleNamespace(
        tenant_id="tnt_1", tenant_slug="acme", tenant_domain="acme.test",
        config={"enabled": True, "events": "*", "host": "hooks.example.test", **(config or {})},
        secret=lambda name: secrets[name], http=http, clock=timezone.now,
        logger=make_adapter_logger("webhook", set()),
    )


URL = "https://Hooks.Example.test/in/T0K3N-abc123?x=1"
SETTINGS = "/admin/settings/adapters/webhook/"
SECRET_RE = re.compile(r"whsec_[0-9a-f]{64}")


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test")
class _Base(LeadBase):
    def client_as(self, role, *, tenant=None):
        client = Client()
        client.force_login(self.op(role, tenant=tenant))
        return client

    def get(self, client, path):
        return client.get(path, HTTP_HOST=HOST)

    def post(self, client, path, data=None):
        return client.post(path, data or {}, HTTP_HOST=HOST)

    def save(self, client, **over):
        data = {"action": "save", "enabled": "on", "url": URL, "events": "claim.*, lead.captured",
                "grant_pii_contact": "on"}
        data.update(over)
        return self.post(client, SETTINGS, {k: v for k, v in data.items() if v is not None})

    def config(self):
        self.tenant.refresh_from_db()
        return (self.tenant.settings.get("adapters") or {}).get("webhook") or {}


class AccessTests(_Base):
    PATHS = [
        "/admin/settings/adapters/",
        SETTINGS,
        "/admin/settings/adapters/webhook/deliveries/",
    ]

    def test_only_an_admin_gets_in(self):
        for path in self.PATHS:
            for role in (Role.SUPPORT, Role.MODERATOR, Role.EDITOR, Role.MANAGER):
                with self.subTest(path=path, role=role):
                    self.assertEqual(self.get(self.client_as(role), path).status_code, 403)
            self.assertEqual(self.get(self.client_as(Role.ADMIN), path).status_code, 200)

    def test_anonymous_is_sent_to_login_and_a_non_member_gets_a_404(self):
        r = self.get(Client(), SETTINGS)
        self.assertEqual(r.status_code, 302)
        other = Tenant.objects.create(slug="other", name="Other")
        self.assertEqual(self.get(self.client_as(Role.ADMIN, tenant=other), SETTINGS).status_code, 404)

    def test_an_unknown_adapter_and_one_with_no_settings_are_404s(self):
        admin = self.client_as(Role.ADMIN)
        for adapter in ("nope", "smtp"):  # smtp is a capability, not a subscriber
            self.assertEqual(self.get(admin, f"/admin/settings/adapters/{adapter}/").status_code, 404)
            self.assertEqual(self.get(admin, f"/admin/settings/adapters/{adapter}/deliveries/").status_code, 404)

    def test_the_post_routes_refuse_a_manager(self):
        manager = self.client_as(Role.MANAGER)
        self.assertEqual(self.post(manager, SETTINGS, {"action": "rotate", "key": "signing_secret"}).status_code, 403)
        self.assertEqual(
            self.post(manager, "/admin/settings/adapters/webhook/deliveries/x/replay/").status_code, 403
        )

    def test_the_admin_home_links_to_integrations_for_admins_only(self):
        self.assertIn("/admin/settings/adapters/", self.get(self.client_as(Role.ADMIN), "/admin/").content.decode())
        self.assertNotIn("/admin/settings/adapters/", self.get(self.client_as(Role.MANAGER), "/admin/").content.decode())


class ListTests(_Base):
    def test_the_list_names_the_integration_and_its_state(self):
        admin = self.client_as(Role.ADMIN)
        page = self.get(admin, "/admin/settings/adapters/").content.decode()
        self.assertIn("webhook", page)
        self.assertIn("Off", page)
        self.save(admin)
        self.assertIn("On", self.get(admin, "/admin/settings/adapters/").content.decode())


class SettingsPageTests(_Base):
    def test_an_empty_page_asks_for_the_url_and_offers_both_scopes_unchecked(self):
        page = self.get(self.client_as(Role.ADMIN), SETTINGS).content.decode()
        self.assertIn("Endpoint URL", page)
        self.assertIn("Contact details: names, email addresses", page)
        self.assertIn("Free text that people wrote", page)
        self.assertNotIn("checked", page)
        self.assertIn("signing_secret: not set", page)
        self.assertIn("Delivery to private-network addresses is blocked", page)

    def test_the_private_network_note_follows_the_deployment_setting(self):
        with override_settings(OSDS_WEBHOOK_ALLOW_PRIVATE=True):
            page = self.get(self.client_as(Role.ADMIN), SETTINGS).content.decode()
        self.assertIn("allows delivery to private-network addresses", page)


class SaveTests(_Base):
    def test_the_first_save_stores_everything_and_shows_the_signing_secret_once(self):
        admin = self.client_as(Role.ADMIN)
        r = self.save(admin)
        self.assertEqual(r.status_code, 200)  # not a redirect: the secret is in this response
        [shown] = SECRET_RE.findall(r.content.decode())
        self.assertEqual(get_secret("webhook_signing_secret", tenant=self.tenant), shown)
        self.assertEqual(get_secret("webhook_url", tenant=self.tenant), URL)
        self.assertEqual(
            self.config(),
            {"enabled": True, "events": "claim.*, lead.captured", "host": "hooks.example.test",
             "scope_pii_contact": True, "scope_pii_message": False},
        )

    def test_the_secret_and_the_url_are_never_shown_again(self):
        admin = self.client_as(Role.ADMIN)
        first = self.save(admin).content.decode()
        secret = SECRET_RE.findall(first)[0]
        for path in (SETTINGS, "/admin/settings/adapters/", "/admin/settings/adapters/webhook/deliveries/"):
            page = self.get(admin, path).content.decode()
            self.assertNotIn(secret, page)
            self.assertNotIn("T0K3N", page)
            self.assertNotIn("abc123", page)
        page = self.get(admin, SETTINGS).content.decode()
        self.assertIn("hooks.example.test", page)  # the host is shown
        self.assertIn("signing_secret: set", page)
        self.assertIn("A value is stored; leave blank to keep it.", page)

    def test_the_settings_changed_event_carries_no_url_and_no_secret(self):
        admin = self.client_as(Role.ADMIN)
        secret = SECRET_RE.findall(self.save(admin).content.decode())[0]
        blob = " ".join(str(e.data) for e in OutboxEvent.all_tenants.filter(type="tenant.settings_changed"))
        self.assertIn("hooks.example.test", blob)
        self.assertIn("scope_pii_contact", blob)
        for forbidden in ("T0K3N", "abc123", secret, "whsec_"):
            self.assertNotIn(forbidden, blob)

    def test_a_scope_grant_is_an_audited_settings_change(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin, grant_pii_contact=None)
        self.save(admin, grant_pii_contact="on")
        grants = [
            op["value"]["webhook"].get("scope_pii_contact")
            for e in OutboxEvent.all_tenants.filter(type="tenant.settings_changed").order_by("id")
            for op in e.data["changes"] if op["path"] == "/adapters"
        ]
        self.assertEqual(grants[-2:], [False, True])
        self.assertEqual(OutboxEvent.all_tenants.filter(type="tenant.settings_changed").last().actor["type"], "admin")

    def test_a_grant_can_be_withdrawn(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin, grant_pii_contact="on", grant_pii_message="on")
        self.assertEqual((self.config()["scope_pii_contact"], self.config()["scope_pii_message"]), (True, True))
        self.save(admin, url="", grant_pii_contact=None, grant_pii_message=None)
        self.assertEqual((self.config()["scope_pii_contact"], self.config()["scope_pii_message"]), (False, False))

    def test_a_blank_url_keeps_the_stored_one_and_the_second_save_shows_no_secret(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        r = self.save(admin, url="", events="*")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(get_secret("webhook_url", tenant=self.tenant), URL)
        self.assertEqual(self.config()["host"], "hooks.example.test")
        self.assertEqual(self.config()["events"], "*")

    def test_a_new_url_changes_the_host_and_keeps_the_signing_secret(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        before = get_secret("webhook_signing_secret", tenant=self.tenant)
        r = self.save(admin, url="https://other.example.test/h")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.config()["host"], "other.example.test")
        self.assertEqual(get_secret("webhook_signing_secret", tenant=self.tenant), before)

    def test_removing_the_url_removes_the_host_and_the_secret(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        r = self.save(admin, url="", enabled=None, clear_url="on")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(has_secret("webhook_url", tenant=self.tenant))
        self.assertNotIn("host", self.config())

    def test_refusals_write_nothing(self):
        admin = self.client_as(Role.ADMIN)
        before = dict(self.tenant.settings)
        cases = [
            {"events": "tenant.*"},
            {"events": "bad!pattern"},
            {"url": "ftp://x.example.test/"},
            {"url": "https://user:pw@hooks.example.test/"},
        ]
        for over in cases:
            with self.subTest(over=over):
                r = self.save(admin, **over)
                self.assertEqual(r.status_code, 200)
                self.assertIn("errors", r.content.decode())
        r = self.save(admin, url="")  # enabled, no URL ever set
        self.assertContains(r, "Enter an endpoint URL before turning the endpoint on.")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.settings, before)
        self.assertFalse(has_secret("webhook_url", tenant=self.tenant))
        self.assertFalse(has_secret("webhook_signing_secret", tenant=self.tenant))

    def test_a_refused_save_does_not_leave_a_new_secret_behind(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        self.save(admin, url="https://ok.example.test/", events="tenant.*")
        self.assertEqual(get_secret("webhook_url", tenant=self.tenant), URL)

    def test_an_unknown_post_action_changes_nothing(self):
        admin = self.client_as(Role.ADMIN)
        r = self.post(admin, SETTINGS, {"action": "explode"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.config(), {})


class RotateTests(_Base):
    def test_rotating_replaces_the_secret_and_shows_the_new_one_once(self):
        admin = self.client_as(Role.ADMIN)
        old = SECRET_RE.findall(self.save(admin).content.decode())[0]
        r = self.post(admin, SETTINGS, {"action": "rotate", "key": "signing_secret"})
        [new] = SECRET_RE.findall(r.content.decode())
        self.assertNotEqual(new, old)
        self.assertEqual(get_secret("webhook_signing_secret", tenant=self.tenant), new)
        self.assertNotIn(new, self.get(admin, SETTINGS).content.decode())

    def test_the_attempt_is_in_the_command_log_and_the_value_is_not(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        r = self.post(admin, SETTINGS, {"action": "rotate", "key": "signing_secret"})
        new = SECRET_RE.findall(r.content.decode())[0]
        row = CommandLog.objects.get(command="adapter.rotate_secret")
        self.assertEqual((row.outcome, row.payload), ("applied", {"adapter_id": "webhook", "key": "signing_secret"}))
        self.assertEqual(row.actor["type"], "admin")
        blob = str(list(CommandLog.objects.values_list("payload", "problem", "actor")))
        self.assertNotIn(new, blob)
        self.assertFalse(OutboxEvent.all_tenants.filter(data__icontains=new).exists())

    def test_only_a_declared_secret_can_be_rotated(self):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        url_before = get_secret("webhook_url", tenant=self.tenant)
        r = self.post(admin, SETTINGS, {"action": "rotate", "key": "url"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(get_secret("webhook_url", tenant=self.tenant), url_before)
        self.assertEqual(CommandLog.objects.get(command="adapter.rotate_secret").outcome, "rejected")

    def test_the_service_blocks_a_manager_and_logs_it(self):
        from osds.adapters import subscriber_by_id
        from osds.tenancy import tenant_context
        from tenants import adapter_settings as svc

        manager = self.op(Role.MANAGER)
        with tenant_context(self.tenant):
            with self.assertRaises(svc.RotateRefused) as cm:
                svc.rotate_secret(self.tenant, subscriber=subscriber_by_id("webhook"), key="signing_secret", operator=manager)
        self.assertEqual(cm.exception.reason, "forbidden")
        self.assertEqual(CommandLog.objects.get(command="adapter.rotate_secret").outcome, "blocked")
        self.assertFalse(has_secret("webhook_signing_secret", tenant=self.tenant))


class TestSendTests(_Base):
    def run_test(self, http):
        admin = self.client_as(Role.ADMIN)
        self.save(admin)
        fake = fake_ctx(http=http, config={"enabled": False})
        with mock.patch("directory.adapter_admin_views.build_context", return_value=fake):
            return self.post(admin, SETTINGS, {"action": "test"}), http

    def test_a_delivered_test_says_so(self):
        r, http = self.run_test(FakeHttp())
        self.assertContains(r, "Test event: delivered")
        self.assertEqual(len(http.calls), 1)

    def test_a_failed_test_says_why(self):
        r, _ = self.run_test(FakeHttp(HttpResponse(503, b"secret-echo")))
        self.assertContains(r, "not delivered")
        self.assertContains(r, "http 503")
        self.assertNotContains(r, "secret-echo")

    def test_a_test_is_never_an_outbox_event_or_a_delivery(self):
        before = OutboxEvent.all_tenants.count()
        self.run_test(FakeHttp())
        self.assertEqual(OutboxEvent.all_tenants.filter(type="webhook.test").count(), 0)
        self.assertEqual(OutboxDelivery.all_tenants.count(), 0)
        self.assertGreaterEqual(OutboxEvent.all_tenants.count(), before)


class DeliveriesPageTests(_Base):
    def delivery(self, status, *, subject="lead_1", attempt=0, error="", tenant=None, adapter="webhook", nulled=False):
        tenant = tenant or self.tenant
        event = emit(events.LEAD_CAPTURED, subject=subject, tenant=tenant, data={"lead": {"id": subject}})
        if nulled:
            OutboxEvent.all_tenants.filter(pk=event.pk).update(data={}, payload_nulled_at=timezone.now())
        return OutboxDelivery.all_tenants.create(
            event=event, tenant=tenant, subject=subject, adapter_id=adapter, status=status,
            attempt=attempt, last_error=error, last_attempted_at=timezone.now(),
        )

    def test_it_shows_health_rows_and_errors(self):
        self.delivery("delivered", attempt=1)
        self.delivery("pending", subject="b", attempt=2, error="http 503")
        self.delivery("dead", subject="c", attempt=12, error="http 410")
        page = self.get(self.client_as(Role.ADMIN), "/admin/settings/adapters/webhook/deliveries/").content.decode()
        self.assertIn("Failing:", page)
        self.assertIn("1 retrying, 1 gave up", page)
        self.assertIn("http 503", page)
        self.assertIn("lead.captured", page)

    def test_a_healthy_adapter_says_so(self):
        self.delivery("delivered", attempt=1)
        page = self.get(self.client_as(Role.ADMIN), "/admin/settings/adapters/webhook/deliveries/").content.decode()
        self.assertIn("No failures right now.", page)

    def test_the_dead_tab_offers_replay_unless_the_details_are_gone(self):
        live = self.delivery("dead", subject="a", attempt=12, error="http 410")
        gone = self.delivery("dead", subject="b", attempt=12, error="http 410", nulled=True)
        page = self.get(self.client_as(Role.ADMIN), "/admin/settings/adapters/webhook/deliveries/?tab=dead").content.decode()
        self.assertIn(f"/deliveries/{live.event.event_id}/replay/", page)
        self.assertNotIn(f"/deliveries/{gone.event.event_id}/replay/", page)
        self.assertIn("details removed", page)

    def test_other_tenants_and_other_adapters_are_not_listed(self):
        other = Tenant.objects.create(slug="other", name="Other")
        self.delivery("dead", subject="theirs", tenant=other, error="http 999")
        self.delivery("dead", subject="elsewhere", adapter="another", error="http 998")
        page = self.get(self.client_as(Role.ADMIN), "/admin/settings/adapters/webhook/deliveries/?tab=dead").content.decode()
        self.assertNotIn("http 999", page)
        self.assertNotIn("http 998", page)


class ReplayViewTests(DeliveriesPageTests):
    def replay(self, delivery, client=None):
        client = client or self.client_as(Role.ADMIN)
        return self.post(client, f"/admin/settings/adapters/webhook/deliveries/{delivery.event.event_id}/replay/")

    def test_replay_requeues_and_lands_on_the_dead_tab(self):
        d = self.delivery("dead", attempt=12, error="http 410")
        r = self.replay(d)
        self.assertEqual((r.status_code, r["Location"]), (302, "/admin/settings/adapters/webhook/deliveries/?tab=dead"))
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempt, d.last_error), ("pending", 0, ""))
        self.assertEqual(CommandLog.objects.get(command="delivery.replay").outcome, "applied")

    def test_replay_of_a_live_delivery_is_refused_with_words(self):
        d = self.delivery("delivered", attempt=1)
        client = self.client_as(Role.ADMIN)
        r = client.post(
            f"/admin/settings/adapters/webhook/deliveries/{d.event.event_id}/replay/", HTTP_HOST=HOST, follow=True
        )
        self.assertContains(r, "Only a delivery that gave up can be replayed.")
        d.refresh_from_db()
        self.assertEqual(d.status, "delivered")

    def test_replay_of_a_nulled_payload_is_refused_with_words(self):
        d = self.delivery("dead", attempt=12, nulled=True)
        client = self.client_as(Role.ADMIN)
        r = client.post(
            f"/admin/settings/adapters/webhook/deliveries/{d.event.event_id}/replay/", HTTP_HOST=HOST, follow=True
        )
        self.assertContains(r, "removed after 90 days")

    def test_get_is_not_allowed(self):
        d = self.delivery("dead", attempt=12)
        r = self.get(self.client_as(Role.ADMIN), f"/admin/settings/adapters/webhook/deliveries/{d.event.event_id}/replay/")
        self.assertEqual(r.status_code, 405)

    def test_another_tenants_delivery_cannot_be_replayed_from_here(self):
        other = Tenant.objects.create(slug="other", name="Other")
        d = self.delivery("dead", subject="theirs", tenant=other, attempt=12)
        client = self.client_as(Role.ADMIN)
        client.post(f"/admin/settings/adapters/webhook/deliveries/{d.event.event_id}/replay/", HTTP_HOST=HOST)
        d.refresh_from_db()
        self.assertEqual(d.status, "dead")
