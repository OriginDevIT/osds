"""Tenant-admin listing views: access control, dynamic custom-field form,
explicit-null clearing on edit, and the publish/unpublish buttons.
"""

from __future__ import annotations

import functools

from django.test import (
    Client,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse as _reverse
from django.utils import timezone

from audit.models import OutboxEvent
from directory import services
from directory.models import Category, Listing, ListingType
from osds.tenancy import tenant_context
from tenants.models import InstallSetup, Operator, StaffMembership, Tenant

reverse = functools.partial(_reverse, urlconf="osds.urls_tenant")
HOST = "acme.test"
# MD5 hashing keeps operator creation cheap in these DB-heavy tests.
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


class _Setup:
    def setUp(self):
        super().setUp()
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.tenant = Tenant.objects.create(
            slug="acme", name="Acme", primary_domain=HOST
        )
        self.lt = ListingType.all_tenants.create(
            tenant=self.tenant, key="business", label_singular="Business",
            label_plural="Businesses", path_segment="businesses",
            fields=[
                {"key": "licence", "label": "Licence no", "type": "text", "required": False},
            ],
        )
        Category.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="plumbers", name="Plumbers"
        )
        self.editor = self._member("editor@acme.test", StaffMembership.Role.EDITOR)
        self.support = self._member("support@acme.test", StaffMembership.Role.SUPPORT)

    def _member(self, email, role):
        op = Operator.objects.create_user(email=email, password="x")
        StaffMembership.objects.create(
            operator=op, tenant=self.tenant, role=role,
            status=StaffMembership.Status.ACTIVE,
        )
        return op

    def _client(self, operator=None):
        c = Client()
        if operator:
            c.force_login(operator)
        return c

    def _url(self, name, **kw):
        return reverse(f"directory_admin:{name}", kwargs=kw)

    def _events(self, subject):
        return set(
            OutboxEvent.all_tenants.filter(subject=subject).values_list(
                "type", flat=True
            )
        )

    def _make_listing(self, **payload):
        payload.setdefault("slug", "acme-co")
        payload.setdefault("name", "Acme Co")
        with tenant_context(self.tenant):
            return services.upsert_listing(
                self.tenant, listing_type=self.lt, payload=payload,
                actor={"type": "admin", "id": "op_x"}, source="manual",
            ).listing


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class ListingAdminAccessTests(_Setup, TestCase):
    def test_editor_can_list(self):
        resp = self._client(self.editor).get(
            self._url("listing-list", key="business"), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 200)

    def test_support_rank_is_forbidden(self):
        resp = self._client(self.support).get(
            self._url("listing-list", key="business"), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 403)

    def test_non_member_is_404(self):
        stranger = Operator.objects.create_user(email="x@y.test", password="x")
        resp = self._client(stranger).get(
            self._url("listing-list", key="business"), HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_form_renders_schema_fields(self):
        resp = self._client(self.editor).get(
            self._url("listing-create", key="business"), HTTP_HOST=HOST
        )
        self.assertContains(resp, 'name="cf_licence"')
        self.assertContains(resp, 'name="categories"')


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class ListingAdminWriteTests(_Setup, TransactionTestCase):
    # The views call upsert_listing, which refuses to run inside a transaction
    # (a plain TestCase wraps every test in one).

    def test_create_redirects_to_edit_and_emits_listing_created(self):
        resp = self._client(self.editor).post(
            self._url("listing-create", key="business"),
            {"name": "Acme Co", "slug": "acme-co", "categories": [], "cf_licence": "L-1"},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        listing = Listing.all_tenants.get(slug="acme-co")
        self.assertEqual(listing.custom_fields, {"licence": "L-1"})
        self.assertIn("listing.created", self._events(listing.public_id))

    def test_create_on_existing_slug_shows_conflict_link(self):
        self._make_listing(slug="taken", name="Already here")
        resp = self._client(self.editor).post(
            self._url("listing-create", key="business"),
            {"name": "Second", "slug": "taken", "categories": []},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertEqual(Listing.all_tenants.filter(slug="taken").count(), 1)

    def test_blanking_a_field_on_edit_clears_it_via_explicit_null(self):
        listing = self._make_listing(
            description="We do plumbing", custom_fields={"licence": "L-9"}
        )
        resp = self._client(self.editor).post(
            self._url("listing-edit", key="business", public_id=listing.public_id),
            {"name": "Acme Co", "slug": "acme-co", "description": "",
             "categories": [], "cf_licence": ""},
            HTTP_HOST=HOST,
        )
        self.assertEqual(resp.status_code, 302)
        listing.refresh_from_db()
        self.assertEqual(listing.description, "")
        self.assertNotIn("licence", listing.custom_fields)

        patch = OutboxEvent.all_tenants.filter(
            type="listing.updated", subject=listing.public_id
        ).latest("id").data["changes"]
        self.assertIn({"op": "remove", "path": "/description"}, patch)
        self.assertIn({"op": "remove", "path": "/custom_fields/licence"}, patch)

    def test_publish_then_unpublish_button(self):
        listing = self._make_listing()
        self._client(self.editor).post(
            self._url("listing-publish", key="business", public_id=listing.public_id),
            HTTP_HOST=HOST,
        )
        listing.refresh_from_db()
        self.assertEqual(listing.visibility, "published")
        self.assertIn("listing.published", self._events(listing.public_id))

        self._client(self.editor).post(
            self._url("listing-unpublish", key="business", public_id=listing.public_id),
            HTTP_HOST=HOST,
        )
        listing.refresh_from_db()
        self.assertEqual(listing.visibility, "draft")
        self.assertIn("listing.unpublished", self._events(listing.public_id))
