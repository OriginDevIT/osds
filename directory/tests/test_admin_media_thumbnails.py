"""Admin listing edit page thumbnails for media on an unpublished listing.

The public ``/media/<id>`` view is visibility-gated, so the edit page must
reference an operator-authenticated serving path instead. These tests pin both
halves: the admin path serves a draft listing's image to an editor, and the
public path still 404s for it.
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase, override_settings
from PIL import Image

from directory import media
from directory.models import Listing
from directory.tests.test_listing_admin_views import (
    _FAST_HASH,
    HOST,
    _Setup,
)
from osds.tenancy import tenant_context
from tenants.models import Operator, StaffMembership, Tenant

ACTOR = {"type": "admin", "id": "op_x"}


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), (20, 40, 60)).save(buf, format="PNG")
    return SimpleUploadedFile("x.png", buf.getvalue(), content_type="image/png")


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class AdminThumbnailTests(_Setup, TransactionTestCase):
    def setUp(self):
        super().setUp()
        root = tempfile.mkdtemp(prefix="osds-admin-thumb-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        self.listing = self._make_listing()
        Listing.all_tenants.filter(pk=self.listing.pk).update(visibility="draft")
        self.listing.refresh_from_db()
        with tenant_context(self.tenant):
            self.asset = media.attach_media(
                self.listing, role="gallery", upload=_png(), actor=ACTOR
            )

    def _edit_url(self):
        return self._url("listing-edit", key="business", public_id=self.listing.public_id)

    def _thumbnail_src(self, client):
        resp = client.get(self._edit_url(), HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        match = re.search(r'<img src="([^"]+)"', resp.content.decode())
        self.assertIsNotNone(match, "edit page rendered no thumbnail")
        return match.group(1)

    def test_draft_listing_thumbnail_is_served_to_an_editor(self):
        client = self._client(self.editor)
        src = self._thumbnail_src(client)
        resp = client.get(src, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertIn("private", resp["Cache-Control"])

    def test_thumbnail_does_not_point_at_the_public_url(self):
        src = self._thumbnail_src(self._client(self.editor))
        self.assertFalse(src.startswith("/media/"), src)

    def test_public_url_still_404s_for_the_draft_asset(self):
        resp = Client().get(f"/media/{self.asset.public_id}", HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 404)

    def test_public_url_404s_even_for_a_signed_in_editor(self):
        resp = self._client(self.editor).get(
            f"/media/{self.asset.public_id}", HTTP_HOST=HOST
        )
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_request_to_the_admin_path_is_not_served(self):
        src = self._thumbnail_src(self._client(self.editor))
        resp = Client().get(src, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("image", resp.get("Content-Type", ""))

    def test_role_below_editor_is_forbidden(self):
        src = self._thumbnail_src(self._client(self.editor))
        resp = self._client(self.support).get(src, HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, 403)

    def test_operator_of_another_tenant_gets_404(self):
        src = self._thumbnail_src(self._client(self.editor))
        other = Tenant.objects.create(slug="beta", name="Beta", primary_domain="beta.test")
        op = Operator.objects.create_user(email="e@beta.test", password="x")
        StaffMembership.objects.create(
            operator=op, tenant=other, role=StaffMembership.Role.EDITOR,
            status=StaffMembership.Status.ACTIVE,
        )
        self.assertEqual(self._client(op).get(src, HTTP_HOST=HOST).status_code, 404)
        self.assertEqual(self._client(op).get(src, HTTP_HOST="beta.test").status_code, 404)

    def test_asset_of_a_different_listing_is_404_under_this_listing(self):
        other = self._make_listing(slug="other-co", name="Other Co")
        with tenant_context(self.tenant):
            foreign = media.attach_media(
                other, role="gallery", upload=_png(), actor=ACTOR
            )
        src = self._thumbnail_src(self._client(self.editor))
        wrong = src.replace(self.asset.public_id, foreign.public_id)
        self.assertNotEqual(src, wrong)
        self.assertEqual(self._client(self.editor).get(wrong, HTTP_HOST=HOST).status_code, 404)
