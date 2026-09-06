"""tenants.secrets -- Fernet round trip and the tenant/deployment/error
resolution order.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from tenants import secrets
from tenants.models import Secret, Tenant

KEY = "test-only-secret-key-material"


@override_settings(OSDS_SECRET_KEY=KEY)
class SecretResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.t1 = Tenant.objects.create(slug="t1", name="T1")
        cls.t2 = Tenant.objects.create(slug="t2", name="T2")

    def test_encrypt_decrypt_round_trip(self):
        token = secrets.encrypt("hunter2")
        self.assertNotIn("hunter2", token)
        self.assertEqual(secrets.decrypt(token), "hunter2")

    def test_missing_secret_raises_configuration_error(self):
        with self.assertRaises(secrets.ConfigurationError):
            secrets.get_secret("smtp_password")

    def test_deployment_level_resolution(self):
        secrets.set_secret("smtp_password", "deploy-pw")
        self.assertEqual(secrets.get_secret("smtp_password"), "deploy-pw")
        self.assertEqual(
            secrets.get_secret("smtp_password", tenant=self.t1), "deploy-pw"
        )

    def test_tenant_override_beats_deployment(self):
        secrets.set_secret("smtp_password", "deploy-pw")
        secrets.set_secret("smtp_password", "t1-pw", tenant=self.t1)
        self.assertEqual(
            secrets.get_secret("smtp_password", tenant=self.t1), "t1-pw"
        )
        # a different tenant with no override still falls back to deployment
        self.assertEqual(
            secrets.get_secret("smtp_password", tenant=self.t2), "deploy-pw"
        )

    def test_set_secret_replaces_in_place(self):
        secrets.set_secret("api_key", "one", tenant=self.t1)
        secrets.set_secret("api_key", "two", tenant=self.t1)
        self.assertEqual(secrets.get_secret("api_key", tenant=self.t1), "two")
        self.assertEqual(
            Secret.objects.filter(
                scope=Secret.Scope.TENANT, tenant=self.t1, key="api_key"
            ).count(),
            1,
        )

    def test_stored_value_is_not_plaintext(self):
        row = secrets.set_secret("api_key", "plaintext-value")
        self.assertNotIn("plaintext-value", row.ciphertext)


class SecretKeyMissingTests(TestCase):
    @override_settings(OSDS_SECRET_KEY="")
    def test_no_secret_key_is_a_configuration_error(self):
        with self.assertRaises(secrets.ConfigurationError):
            secrets.encrypt("x")

    @override_settings(OSDS_SECRET_KEY="a-different-key")
    def test_ciphertext_from_another_key_will_not_decrypt(self):
        with override_settings(OSDS_SECRET_KEY=KEY):
            token = secrets.encrypt("secret")
        with self.assertRaises(secrets.ConfigurationError):
            secrets.decrypt(token)
