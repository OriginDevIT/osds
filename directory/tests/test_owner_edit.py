"""PR B: what an owner may change on their listing (spec §15.4 as ruled in
decisions.md §4.9), the edit form and media routes, and the leads placeholder.
"""

from __future__ import annotations

import shutil
import tempfile

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboxEvent
from directory import owner_edit
from directory.field_schema import SchemaError
from directory.models import DirectoryUser, Listing, ListingType, MediaAsset
from directory.tests.test_claim_review import DANA, HOST, Role
from directory.tests.test_media import _upload
from directory.tests.test_owner_auth import OWNER_EMAIL, _OwnerBase
from osds.tenancy import tenant_context

SCHEMA = [
    {"key": "hours", "label": "Hours", "type": "text", "required": False,
     "public": True, "searchable": False},
    {"key": "license", "label": "License number", "type": "text", "required": False,
     "public": False, "searchable": False},
    {"key": "specialty", "label": "Specialty", "type": "text", "required": True,
     "public": True, "searchable": False},
]


class _EditBase(_OwnerBase):
    def setUp(self):
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="osds-owner-media-")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        ctx = override_settings(OSDS_MEDIA_ROOT=self._media_root)
        ctx.enable()
        self.addCleanup(ctx.disable)
        ListingType.all_tenants.filter(pk=self.lt.pk).update(fields=SCHEMA)
        self.listing = Listing.all_tenants.get(slug="hoffman-plumbing")
        Listing.all_tenants.filter(pk=self.listing.pk).update(
            custom_fields={"specialty": "Drains"}, phone_e164="+17735550142",
            description="Old description",
        )
        self.listing.refresh_from_db()

    def edit(self, changes, *, user=None, listing=None):
        with tenant_context(self.tenant):
            listing = Listing.objects.get(pk=(listing or self.listing).pk)
            return owner_edit.owner_update_listing(
                self.tenant, user=user or self.owner, listing=listing, changes=changes
            )

    def fresh_listing(self):
        return Listing.all_tenants.get(pk=self.listing.pk)

    def updated_events(self):
        return OutboxEvent.all_tenants.filter(
            type="listing.updated", subject=self.listing.public_id
        ).order_by("id")

    def upsert_logs(self):
        return CommandLog.objects.filter(command="listing.upsert").order_by("id")


class FreeFieldTests(_EditBase):
    def test_description_phone_and_public_custom_fields_are_the_owners(self):
        before = self.updated_events().count()
        result = self.edit({
            "description": "New description",
            "phone_e164": "+13125550188",
            "custom_fields": {"hours": "9-5", "specialty": "Boilers"},
        })
        self.assertEqual(result.outcome, "updated")
        listing = self.fresh_listing()
        self.assertEqual(listing.description, "New description")
        self.assertEqual(listing.phone_e164, "+13125550188")
        self.assertEqual(listing.custom_fields, {"hours": "9-5", "specialty": "Boilers"})
        self.assertEqual(self.updated_events().count(), before + 1)
        event = self.updated_events().last()
        self.assertEqual(event.actor, {"type": "owner", "id": self.owner.public_id})
        row = self.upsert_logs().last()
        self.assertEqual((row.outcome, row.actor["type"]), ("applied", "owner"))

    def test_an_unchanged_save_emits_nothing(self):
        before = self.updated_events().count()
        result = self.edit({"description": "Old description"})
        self.assertEqual(result.outcome, "unchanged")
        self.assertEqual(self.updated_events().count(), before)

    def test_blank_and_none_clear_explicitly(self):
        self.edit({"description": "", "phone_e164": None,
                   "custom_fields": {"hours": "9-5"}})
        self.edit({"custom_fields": {"hours": ""}})
        listing = self.fresh_listing()
        self.assertEqual((listing.description, listing.phone_e164), ("", ""))
        self.assertNotIn("hours", listing.custom_fields)

    def test_omitted_free_fields_are_left_alone(self):
        self.edit({"phone_e164": "+13125550188"})
        listing = self.fresh_listing()
        self.assertEqual(listing.description, "Old description")
        self.assertEqual(listing.custom_fields, {"specialty": "Drains"})

    def test_a_required_public_field_cannot_be_cleared(self):
        with self.assertRaises(SchemaError):
            self.edit({"custom_fields": {"specialty": ""}})
        self.assertEqual(self.fresh_listing().custom_fields, {"specialty": "Drains"})

    def test_a_bad_phone_is_a_schema_error_and_nothing_changes(self):
        with self.assertRaises(SchemaError):
            self.edit({"description": "x", "phone_e164": "not a phone"})
        self.assertEqual(self.fresh_listing().description, "Old description")

    def test_provenance_source_is_not_rewritten_by_an_edit(self):
        before = self.fresh_listing().source
        self.edit({"description": "Changed"})
        self.assertEqual(self.fresh_listing().source, before)


class RefusalTests(_EditBase):
    OPERATOR_ONLY = {
        "name": "Hacked", "slug": "hacked", "categories": ["x"],
        "website": "https://evil.example", "email": "evil@evil.example",
        "address_line1": "1 Evil St", "address_line2": "x", "locality": "x",
        "region": "x", "postal_code": "1", "country": "US", "lat": 1, "lon": 1,
        "geo_precision": "rooftop", "location": {"locality": "x"},
        "contact": {"website": "https://evil.example"},
    }

    def test_every_operator_only_key_is_refused_by_name(self):
        for key, value in self.OPERATOR_ONLY.items():
            with self.subTest(key=key):
                with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
                    self.edit({key: value})
                self.assertEqual((cm.exception.reason, cm.exception.fields), ("operator_only", [key]))
        listing = self.fresh_listing()
        self.assertEqual((listing.name, listing.slug, listing.website, listing.email),
                         ("Hoffman Plumbing", "hoffman-plumbing", "https://hoffmanplumbing.example",
                          "info@hoffmanplumbing.example"))

    def test_refused_not_dropped_even_beside_a_valid_free_change(self):
        with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
            self.edit({"description": "Sneaky", "website": "https://evil.example"})
        self.assertEqual(cm.exception.fields, ["website"])
        self.assertEqual(self.fresh_listing().description, "Old description")

    def test_keys_the_write_path_rejects_are_not_accepted_from_an_owner(self):
        for key in ("tier", "status", "visibility", "media", "id", "owner", "owner_id", "anything"):
            with self.subTest(key=key):
                with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
                    self.edit({key: "x"})
                self.assertEqual(cm.exception.reason, "not_accepted")
        self.assertEqual(self.fresh_listing().status, "claimed")

    def test_admin_only_and_unknown_custom_fields_are_refused(self):
        for key in ("license", "no_such_field"):
            with self.subTest(key=key):
                with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
                    self.edit({"custom_fields": {key: "x"}})
                self.assertEqual((cm.exception.reason, cm.exception.fields), ("custom_field", [key]))

    def test_a_refusal_is_a_rejected_upsert_row_with_keys_and_no_values(self):
        before = self.updated_events().count()
        with self.assertRaises(owner_edit.OwnerEditRefused):
            self.edit({"name": "SECRET-VALUE-NAME", "description": "SECRET-VALUE-DESC"})
        row = self.upsert_logs().last()
        self.assertEqual(row.outcome, "rejected")
        self.assertEqual(row.problem, {"reason": "operator_only", "fields": ["name"]})
        self.assertEqual(row.actor["type"], "owner")
        self.assertEqual(row.payload["keys"], ["description", "name"])
        self.assertNotIn("SECRET-VALUE", str(row.payload) + str(row.problem))
        self.assertEqual(self.updated_events().count(), before)

    def test_someone_who_does_not_own_the_listing_is_blocked(self):
        stranger = DirectoryUser.all_tenants.create(tenant=self.tenant, email="nobody@x.example")
        with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
            self.edit({"description": "mine now"}, user=stranger)
        self.assertEqual(cm.exception.reason, "not_owner")
        self.assertEqual(self.upsert_logs().last().outcome, "blocked")
        self.assertEqual(self.fresh_listing().description, "Old description")

    def test_a_former_owner_is_blocked_even_with_a_stale_listing_object(self):
        stale = self.listing
        dispute = self.submit(claimant=DANA)
        self.approve(dispute, self.op(Role.EDITOR), transfer=True)
        with tenant_context(self.tenant):
            with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
                owner_edit.owner_update_listing(
                    self.tenant, user=self.owner, listing=stale, changes={"description": "x"}
                )
        self.assertEqual(cm.exception.reason, "not_owner")

    def test_a_suspended_listing_cannot_be_edited(self):
        Listing.all_tenants.filter(pk=self.listing.pk).update(status=Listing.Status.SUSPENDED)
        with self.assertRaises(owner_edit.OwnerEditRefused) as cm:
            self.edit({"description": "x"})
        self.assertEqual(cm.exception.reason, "listing_suspended")

    def test_the_scope_constants_match_the_ruling(self):
        self.assertEqual(owner_edit.FREE_KEYS, {"description", "phone_e164", "custom_fields"})
        for key in ("name", "slug", "categories", "website", "email", "address_line1"):
            self.assertIn(key, owner_edit.OPERATOR_ONLY_KEYS)
        self.assertFalse(owner_edit.FREE_KEYS & owner_edit.OPERATOR_ONLY_KEYS)


class EditPageTests(_EditBase):
    def page(self, client=None):
        return self.get(f"/owner/listings/{self.listing.public_id}/", client=client)

    def edit_post(self, data, client=None):
        return self.post(f"/owner/listings/{self.listing.public_id}/", data, client=client)

    def test_the_form_holds_only_what_the_owner_may_change(self):
        self.sign_in()
        r = self.page()
        for name in ("description", "phone_e164", "cf_hours", "cf_specialty"):
            self.assertContains(r, f'name="{name}"')
        for name in ("name", "slug", "email", "website", "categories", "address_line1", "cf_license"):
            self.assertNotContains(r, f'name="{name}"')
        self.assertContains(r, "contact the directory")
        self.assertContains(r, "Old description")

    def test_saving_updates_the_listing_and_says_so(self):
        self.sign_in()
        r = self.edit_post({"description": "Fresh", "phone_e164": "+13125550188",
                            "cf_hours": "24h", "cf_specialty": "Boilers"})
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.page(), "Saved.")
        self.assertEqual(self.fresh_listing().description, "Fresh")

    def test_an_unchanged_save_says_no_changes(self):
        self.sign_in()
        self.edit_post({"description": "Old description", "phone_e164": "+17735550142",
                        "cf_hours": "", "cf_specialty": "Drains"})
        self.assertContains(self.page(), "No changes.")

    def test_a_blank_input_clears_the_field(self):
        self.sign_in()
        self.edit_post({"description": "", "phone_e164": "", "cf_hours": "",
                        "cf_specialty": "Drains"})
        listing = self.fresh_listing()
        self.assertEqual((listing.description, listing.phone_e164), ("", ""))

    def test_a_required_field_left_blank_shows_an_error_and_saves_nothing(self):
        self.sign_in()
        r = self.edit_post({"description": "Changed", "phone_e164": "", "cf_hours": "",
                            "cf_specialty": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh_listing().description, "Old description")

    def test_a_crafted_post_naming_an_operator_only_field_is_refused_whole(self):
        self.sign_in()
        for stray in ("name", "website", "email", "address_line1", "slug", "status", "visibility"):
            with self.subTest(stray=stray):
                r = self.client.post(
                    f"/owner/listings/{self.listing.public_id}/",
                    {"description": "Slipped in", "phone_e164": "", "cf_hours": "",
                     "cf_specialty": "Drains", stray: "evil"},
                    HTTP_HOST=HOST, follow=True,
                )
                self.assertContains(r, "can only be changed by the directory" if stray in owner_edit.OPERATOR_ONLY_KEYS else "cannot be changed here")
                listing = self.fresh_listing()
                self.assertEqual(listing.description, "Old description")
                self.assertEqual((listing.name, listing.website, listing.status),
                                 ("Hoffman Plumbing", "https://hoffmanplumbing.example", "claimed"))
        self.assertEqual(self.upsert_logs().last().outcome, "rejected")

    def test_someone_elses_listing_is_404_and_anonymous_is_sent_to_sign_in(self):
        other = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        self.assertEqual(self.get(f"/owner/listings/{other.public_id}/").status_code, 302)
        self.sign_in()
        self.assertEqual(self.get(f"/owner/listings/{other.public_id}/").status_code, 404)
        self.assertEqual(self.post(f"/owner/listings/{other.public_id}/", {"description": "x"}).status_code, 404)
        self.assertEqual(Listing.all_tenants.get(pk=other.pk).description, "")

    def test_a_suspended_listing_shows_no_form_and_refuses_a_post(self):
        self.sign_in()
        Listing.all_tenants.filter(pk=self.listing.pk).update(status=Listing.Status.SUSPENDED)
        page = self.page()
        self.assertContains(page, "This listing is suspended")
        self.assertNotContains(page, 'name="description"')
        self.edit_post({"description": "x", "phone_e164": "", "cf_hours": "", "cf_specialty": "Drains"})
        self.assertEqual(self.fresh_listing().description, "Old description")

    def test_the_post_needs_csrf(self):
        self.sign_in()
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        self.assertEqual(self.edit_post({"description": "x"}, client=strict).status_code, 403)

    def test_an_operator_session_cannot_edit(self):
        operator = Client()
        operator.force_login(self.op(Role.ADMIN))
        r = self.edit_post({"description": "x"}, client=operator)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fresh_listing().description, "Old description")


class MediaTests(_EditBase):
    def media_url(self, suffix="add/"):
        return f"/owner/listings/{self.listing.public_id}/media/{suffix}"

    def add(self, role="gallery", upload=None, alt="Front door", client=None):
        return self.post(
            self.media_url(),
            {"role": role, "image": upload or _upload(), "alt_text": alt},
            client=client,
        )

    def test_an_image_goes_through_the_media_service_as_the_owner(self):
        self.sign_in()
        before = self.updated_events().count()
        r = self.add()
        self.assertEqual(r.status_code, 302)
        asset = MediaAsset.all_tenants.get(listing=self.listing)
        self.assertEqual((asset.role, asset.status, asset.alt_text), ("gallery", "ready", "Front door"))
        self.assertIsNone(asset.uploaded_by_id)
        self.assertEqual(self.updated_events().count(), before + 1)
        self.assertEqual(self.updated_events().last().actor,
                         {"type": "owner", "id": self.owner.public_id})
        self.assertEqual(len(self.fresh_listing().media["gallery"]), 1)

    def test_a_logo_replaces_the_previous_one(self):
        self.sign_in()
        self.add("logo", _upload("a.png"))
        self.add("logo", _upload("b.png", color=(1, 2, 3)))
        self.assertEqual(MediaAsset.all_tenants.filter(listing=self.listing, role="logo").count(), 1)

    def test_a_file_that_is_not_an_image_is_refused_with_the_services_message(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.sign_in()
        r = self.client.post(
            self.media_url(),
            {"role": "gallery", "image": SimpleUploadedFile("x.png", b"not an image",
                                                            content_type="image/png"),
             "alt_text": ""},
            HTTP_HOST=HOST, follow=True,
        )
        self.assertFalse(MediaAsset.all_tenants.exists())
        self.assertEqual(r.status_code, 200)

    def test_a_missing_file_or_bad_role_is_refused(self):
        self.sign_in()
        self.post(self.media_url(), {"role": "gallery"})
        self.post(self.media_url(), {"role": "banner", "image": _upload()})
        self.assertFalse(MediaAsset.all_tenants.exists())

    def test_removing_an_image(self):
        self.sign_in()
        self.add()
        asset = MediaAsset.all_tenants.get(listing=self.listing)
        r = self.post(self.media_url(f"{asset.public_id}/remove/"))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(MediaAsset.all_tenants.exists())
        self.assertEqual(self.updated_events().last().actor["type"], "owner")

    def test_an_asset_of_another_listing_is_404(self):
        other = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        Listing.all_tenants.filter(pk=other.pk).update(owner=self.owner, status="claimed")
        self.sign_in()
        self.post(f"/owner/listings/{other.public_id}/media/add/",
                  {"role": "gallery", "image": _upload(), "alt_text": ""})
        asset = MediaAsset.all_tenants.get(listing=other)
        r = self.post(self.media_url(f"{asset.public_id}/remove/"))
        self.assertEqual(r.status_code, 404)
        self.assertTrue(MediaAsset.all_tenants.filter(pk=asset.pk).exists())

    def test_routes_are_post_only_and_owner_only(self):
        self.assertEqual(self.post(self.media_url(), {"role": "gallery"}).status_code, 302)
        self.sign_in()
        self.assertEqual(self.get(self.media_url()).status_code, 405)
        self.assertEqual(self.get(self.media_url("x/remove/")).status_code, 405)

    def test_a_suspended_listing_takes_no_media(self):
        self.sign_in()
        Listing.all_tenants.filter(pk=self.listing.pk).update(status=Listing.Status.SUSPENDED)
        self.add()
        self.assertFalse(MediaAsset.all_tenants.exists())

    def test_a_former_owner_cannot_add_media(self):
        self.sign_in()
        dispute = self.submit(claimant=DANA)
        self.approve(dispute, self.op(Role.EDITOR), transfer=True)
        self.assertEqual(self.add().status_code, 404)
        self.assertFalse(MediaAsset.all_tenants.exists())


class LeadsPlaceholderTests(_EditBase):
    def test_it_is_404_for_a_signed_in_owner(self):
        self.sign_in()
        self.assertEqual(self.get("/owner/leads/").status_code, 404)

    def test_anonymous_is_sent_to_sign_in_first(self):
        r = self.get("/owner/leads/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))

    def test_nothing_links_to_it(self):
        self.sign_in()
        self.assertNotContains(self.get("/owner/"), "/owner/leads/")
        self.assertNotContains(self.get(f"/owner/listings/{self.listing.public_id}/"), "/owner/leads/")
        self.assertIsNotNone(timezone.now())
        self.assertEqual(OWNER_EMAIL, self.owner.email)
