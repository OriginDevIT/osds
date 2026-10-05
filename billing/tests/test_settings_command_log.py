"""Payment settings and tier saves in the command log (#225, decisions.md §4):
one ``settings.update`` row per successful save, naming the page and the changed
field names, never a value."""

from __future__ import annotations

from django.test import Client, override_settings
from django.utils import timezone

from audit.tests.settings_log import assert_one_save, settings_rows
from billing import tiers
from billing.tests.base import Role
from billing.tests.payments_base import PaymentTestCase
from tenants.models import InstallSetup


@override_settings(ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SettingsLogTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.admin = self.operator(Role.ADMIN)
        self.client = Client()
        self.client.force_login(self.admin)

    def post(self, path, data=None):
        return self.client.post(path, data or {}, HTTP_HOST="acme.test")

    def test_payment_settings_save(self):
        r = self.post("/admin/settings/payments/",
                      {"account": "acct_1", "api_key": "sk_live_SECRET", "webhook_secret": "whsec_x"})
        self.assertEqual(r.status_code, 302)
        assert_one_save(
            self, self.tenant, self.admin, page="payments",
            fields=["account", "api_key", "webhook_secret"],
            forbidden=["acct_1", "sk_live_SECRET", "whsec_x"],
        )

    def test_a_refused_payment_save_is_logged_rejected_and_names_nothing(self):
        r = self.post("/admin/settings/payments/", {"account": "acct_1", "api_key": ""})
        self.assertEqual(r.status_code, 200)  # the required api_key is missing
        [row] = settings_rows(self.tenant)
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "invalid"}))
        self.assertNotIn("acct_1", str([row.payload, row.problem]))

    def test_tier_create_edit_delete(self):
        body = {"key": "gold", "name": "Gold-NAME", "rank": "3", "purchasable": "on",
                "price_minor": "4900", "currency": "usd", "interval": "month"}
        self.assertEqual(self.post("/admin/tiers/new/", body).status_code, 302)
        row = assert_one_save(
            self, self.tenant, self.admin, page="tiers", op="add",
            fields=["key", "name", "rank", "purchasable", "uses_slot", "price_minor", "currency",
                    "interval", "trial_days", "badge_label", "perks"],
            forbidden=["Gold-NAME", "4900", "gold"],
        )
        self.assertEqual(row.payload["page"], "tiers")

        self.assertEqual(self.post("/admin/tiers/gold/", {**body, "name": "Gold-RENAMED"}).status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].payload, {"page": "tiers", "op": "replace", "fields": ["name"]})
        self.assertNotIn("Gold-RENAMED", str(rows[1].payload))

        self.assertEqual(self.post("/admin/tiers/gold/delete/").status_code, 302)
        rows = settings_rows(self.tenant)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].payload, {"page": "tiers", "op": "remove", "fields": []})

    def test_a_refused_tier_is_logged_rejected(self):
        with self.assertRaises(tiers.TierError):
            tiers.create_tier(self.tenant, key="paid0", name="P", rank=0, purchasable=True,
                              price_minor=100, currency="USD", interval="month", actor=self.admin)
        [row] = settings_rows(self.tenant)
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "invalid"}))
