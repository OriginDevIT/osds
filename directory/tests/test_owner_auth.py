"""Owner sign-in and the read-only dashboard (decisions.md §4.9; spec §4.3).

TransactionTestCase throughout: the commands refuse an open transaction.
"""

from __future__ import annotations

import io
import re
from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboundMessage, OutboxEvent, RateLimitCounter
from audit.tests.window_clock import pinned_windows
from audit.worker.jobs import build_tick_registry
from directory import jobs as directory_jobs
from directory import owner_auth, services
from directory.models import (
    DirectoryUser,
    Listing,
    OwnerSession,
    OwnerSignInToken,
)
from directory.tests.test_claim_review import (
    DANA,
    HOST,
    OTHER,
    Role,
    _Base,
)
from osds.tests.mail_stub import email_send_stub
from tenants.models import Tenant

OWNER_EMAIL = OTHER["email"]
LINK_RE = re.compile(r"https://acme\.test/owner/signin/([A-Za-z0-9_\-]+)/")
_FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST="console.test", PASSWORD_HASHERS=_FAST_HASH
)
class _OwnerBase(_Base):
    def setUp(self):
        super().setUp()
        # Every flood lands in one window, however slow the runner (#245).
        self.enterContext(pinned_windows())
        self.client = Client()
        self.claim = self.own()  # OTHER becomes the sitting owner
        self.owner = DirectoryUser.all_tenants.get(email=OWNER_EMAIL)
        OutboundMessage.all_tenants.all().delete()  # drop setup mail

    # -- helpers --
    def post(self, path, data=None, client=None, **extra):
        return (client or self.client).post(path, data or {}, HTTP_HOST=HOST, **extra)

    def get(self, path, client=None, **extra):
        return (client or self.client).get(path, HTTP_HOST=HOST, **extra)

    def request_link(self, email=OWNER_EMAIL, **extra):
        return self.post("/owner/signin/", {"email": email}, **extra)

    def link_mail(self, address=OWNER_EMAIL):
        return OutboundMessage.all_tenants.filter(
            kind=owner_auth.MAIL_KIND, to_address=address
        ).order_by("id")

    def secret_from(self, message):
        return LINK_RE.search(message.body_text).group(1)

    def issue(self, email=OWNER_EMAIL):
        """Request a link and return its secret."""
        self.age_tokens()
        self.request_link(email)
        return self.secret_from(self.link_mail(email).last())

    def age_tokens(self, minutes=5):
        OwnerSignInToken.all_tenants.update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )

    def sign_in(self, client=None):
        client = client or self.client
        secret = self.issue()
        response = self.post(f"/owner/signin/{secret}/", client=client)
        assert response.status_code == 302, response.status_code
        return secret, response

    def session(self):
        return OwnerSession.all_tenants.get(user=self.owner)


class RequestLinkTests(_OwnerBase):
    def test_an_owner_gets_one_mail_with_a_twelve_hour_single_link(self):
        before = timezone.now()
        r = self.request_link()
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/sent/"))
        message = self.link_mail().get()
        self.assertEqual(message.subject, "Your sign-in link for Acme")
        self.assertIn("works once", message.body_text)
        secret = self.secret_from(message)
        self.assertGreaterEqual(len(secret), 43)
        self.assertAlmostEqual(
            (message.expires_at - before).total_seconds(),
            timedelta(hours=12).total_seconds(), delta=30,
        )
        token = OwnerSignInToken.all_tenants.get(user=self.owner)
        self.assertEqual(token.expires_at, message.expires_at)
        self.assertEqual(token.message_id, message.pk)
        self.assertIsNone(token.used_at)

    def test_the_message_names_nobody(self):
        DirectoryUser.all_tenants.filter(pk=self.owner.pk).update(name="Zed Unique-Name")
        self.request_link()
        body = self.link_mail().get().body_text
        self.assertNotIn("Zed", body)
        self.assertNotIn("Hoffman", body)  # nor the listing

    def test_only_the_digest_is_stored_and_nothing_logs_the_secret_or_the_address(self):
        self.request_link()
        secret = self.secret_from(self.link_mail().get())
        token = OwnerSignInToken.all_tenants.get()
        self.assertNotEqual(token.token_hash, secret)
        self.assertEqual(len(token.token_hash), 64)
        self.assertEqual(token.token_hash, owner_auth._digest(secret))
        owner_logs = CommandLog.objects.filter(command__startswith="owner.")
        self.assertTrue(owner_logs.exists())
        for log in owner_logs:
            blob = f"{log.payload}{log.actor}{log.problem}"
            self.assertNotIn(secret, blob)
            self.assertNotIn(OWNER_EMAIL, blob)
        self.assertEqual(CommandLog.objects.filter(command="owner.request_signin").get().outcome, "applied")

    def test_a_stranger_gets_exactly_the_same_response_and_no_mail(self):
        owner = self.request_link()
        owner_page = self.get(owner["Location"]).content
        for email in ("nobody@elsewhere.example", DANA["email"]):
            self.age_tokens()
            r = self.request_link(email)
            self.assertEqual((r.status_code, r["Location"]), (owner.status_code, owner["Location"]))
            self.assertEqual(self.get(r["Location"]).content, owner_page)
            self.assertFalse(self.link_mail(email).exists())

    def test_a_claimant_with_only_a_pending_claim_is_not_an_owner(self):
        self.submit(claimant={**DANA, "email": "pending@elsewhere.example"})
        # (DANA's listing is owned, so this is a dispute: still not an owner)
        self.request_link("pending@elsewhere.example")
        self.assertFalse(self.link_mail("pending@elsewhere.example").exists())

    def test_the_log_records_why_without_the_address(self):
        self.request_link("nobody@elsewhere.example")
        row = CommandLog.objects.filter(command="owner.request_signin").get()
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "not_an_owner"}))
        self.assertNotIn("nobody", str(row.payload) + str(row.actor))

    def test_a_second_request_inside_a_minute_is_throttled_silently(self):
        r1 = self.request_link()
        r2 = self.request_link()
        self.assertEqual(r1["Location"], r2["Location"])
        self.assertEqual(self.link_mail().count(), 1)
        self.assertEqual(
            CommandLog.objects.filter(command="owner.request_signin").order_by("id").last().problem,
            {"reason": "too_soon"},
        )

    def test_at_most_three_links_an_hour(self):
        for _ in range(3):
            self.age_tokens(minutes=2)
            self.request_link()
        self.age_tokens(minutes=2)
        r = self.request_link()
        self.assertEqual(r["Location"], "/owner/signin/sent/")
        self.assertEqual(self.link_mail().count(), 3)
        self.assertEqual(
            CommandLog.objects.filter(command="owner.request_signin").order_by("id").last().problem,
            {"reason": "hourly_cap"},
        )
        self.age_tokens(minutes=61)
        self.request_link()
        self.assertEqual(self.link_mail().count(), 4)

    def test_a_new_link_replaces_the_old_one_and_kills_its_mail(self):
        old = self.issue()
        old_message = self.link_mail().get()
        new = self.issue()
        self.assertNotEqual(old, new)
        with self._scope():
            self.assertIsNone(owner_auth.peek_token(self.tenant, old))
            self.assertIsNotNone(owner_auth.peek_token(self.tenant, new))
        old_message.refresh_from_db()
        self.assertLessEqual(old_message.expires_at, timezone.now())
        self.assertEqual(self.post(f"/owner/signin/{old}/").status_code, 400)

    def _scope(self):
        from osds.tenancy import tenant_context

        return tenant_context(self.tenant)

    def test_sign_in_emits_no_event(self):
        before = OutboxEvent.all_tenants.count()
        self.sign_in()
        self.assertEqual(OutboxEvent.all_tenants.count(), before)

    def test_unavailable_without_mail_or_an_absolute_base(self):
        with email_send_stub(available=False):
            page = self.get("/owner/signin/")
            self.assertContains(page, "not available")
            self.assertNotContains(page, 'name="email"')
            r = self.request_link()
            self.assertContains(r, "not available")
        self.assertFalse(self.link_mail().exists())
        Tenant.objects.filter(pk=self.tenant.pk).update(domain_verified_at=None)
        self.assertContains(self.get("/owner/signin/"), "not available")
        r = self.request_link()
        self.assertContains(r, "not available")
        self.assertFalse(self.link_mail().exists())

    def test_the_per_ip_limit_returns_429_and_logs_one_blocked_row(self):
        for n in range(10):
            self.assertEqual(self.request_link(f"x{n}@elsewhere.example").status_code, 302)
        for n in range(3):
            r = self.request_link(f"y{n}@elsewhere.example")
            self.assertEqual(r.status_code, 429)
            self.assertTrue(int(r["Retry-After"]) > 0)
            self.assertContains(r, "Too many attempts", status_code=429)
        blocked = CommandLog.objects.filter(command="owner.request_signin", outcome="blocked")
        self.assertEqual(blocked.count(), 1)
        self.assertTrue(RateLimitCounter.all_tenants.exists())

    def test_the_limiter_stores_no_address(self):
        self.request_link()
        for counter in RateLimitCounter.all_tenants.all():
            self.assertNotIn("127.0.0.1", counter.subject_hash)

    def test_the_form_is_reachable_ahead_of_the_public_catch_all(self):
        page = self.get("/owner/signin/")
        self.assertContains(page, "Owner sign in")
        self.assertContains(page, 'name="email"')
        self.assertIn("owner", services.RESERVED_SLUGS)
        self.assertContains(self.get("/"), 'href="/owner/signin/"')


class ConfirmAndSessionTests(_OwnerBase):
    def test_following_the_link_spends_nothing(self):
        secret = self.issue()
        for _ in range(3):
            r = self.get(f"/owner/signin/{secret}/")
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "Sign in</button>")
            self.assertEqual(r["Referrer-Policy"], "same-origin")
            self.assertIn("no-store", r["Cache-Control"])
        self.assertIsNone(OwnerSignInToken.all_tenants.get().used_at)
        self.assertFalse(OwnerSession.all_tenants.exists())
        self.assertEqual(self.post(f"/owner/signin/{secret}/").status_code, 302)

    def test_posting_spends_it_once_and_opens_a_session(self):
        secret, response = self.sign_in()
        self.assertEqual(response["Location"], "/owner/")
        self.assertIsNotNone(OwnerSignInToken.all_tenants.get().used_at)
        self.assertEqual(self.session().user_id, self.owner.pk)
        again = self.post(f"/owner/signin/{secret}/")
        self.assertEqual(again.status_code, 400)
        self.assertContains(again, "not valid", status_code=400)
        self.assertEqual(OwnerSession.all_tenants.count(), 1)
        self.assertEqual(again["Referrer-Policy"], "same-origin")

    def test_unknown_expired_and_foreign_links_are_the_same_page(self):
        secret = self.issue()
        for path_secret in ("not-a-real-token", secret[::-1]):
            r = self.post(f"/owner/signin/{path_secret}/")
            self.assertEqual(r.status_code, 400)
        OwnerSignInToken.all_tenants.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.get(f"/owner/signin/{secret}/").status_code, 400)
        self.assertEqual(self.post(f"/owner/signin/{secret}/").status_code, 400)
        self.assertEqual(
            [r.problem["reason"] for r in CommandLog.objects.filter(command="owner.sign_in").order_by("id")],
            ["unknown", "unknown", "expired"],
        )

    def test_a_link_replayed_on_another_directory_is_refused(self):
        secret = self.issue()
        Tenant.objects.create(
            slug="other", name="Other", primary_domain="other.test",
            domain_verified_at=timezone.now(),
        )
        r = Client().post(f"/owner/signin/{secret}/", HTTP_HOST="other.test")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(OwnerSession.all_tenants.exists())

    def test_a_link_whose_owner_lost_the_listing_is_refused(self):
        secret = self.issue()
        Listing.all_tenants.update(owner=None, status=Listing.Status.UNCLAIMED)
        self.assertEqual(self.get(f"/owner/signin/{secret}/").status_code, 400)
        self.assertEqual(self.post(f"/owner/signin/{secret}/").status_code, 400)

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_the_cookie_attributes(self):
        _, response = self.sign_in()
        cookie = response.cookies[owner_auth.COOKIE_NAME]
        self.assertEqual(cookie["path"], "/owner/")
        self.assertEqual(cookie["domain"], "")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["secure"])
        self.assertEqual(int(cookie["max-age"]), 30 * 24 * 3600)
        self.assertNotEqual(cookie.value, self.session().token_hash)
        self.assertEqual(owner_auth._digest(cookie.value), self.session().token_hash)

    @override_settings(SESSION_COOKIE_SECURE=False)
    def test_the_cookie_is_secure_only_when_the_install_is(self):
        _, response = self.sign_in()
        self.assertFalse(response.cookies[owner_auth.COOKIE_NAME]["secure"])

    def test_the_cookie_name_is_not_the_operator_session_cookie(self):
        from django.conf import settings

        self.assertNotEqual(owner_auth.COOKIE_NAME, settings.SESSION_COOKIE_NAME)

    def test_the_dashboard_needs_a_session(self):
        r = self.get("/owner/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))
        self.assertEqual(self.get("/owner/listings/anything/").status_code, 302)

    def test_idle_for_twelve_hours_ends_the_session(self):
        self.sign_in()
        OwnerSession.all_tenants.update(
            last_seen_at=timezone.now() - timedelta(hours=11, minutes=59)
        )
        self.assertEqual(self.get("/owner/").status_code, 200)
        OwnerSession.all_tenants.update(
            last_seen_at=timezone.now() - timedelta(hours=12, seconds=1)
        )
        r = self.get("/owner/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(OwnerSession.all_tenants.exists())

    def test_thirty_days_ends_it_however_active(self):
        self.sign_in()
        OwnerSession.all_tenants.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.get("/owner/").status_code, 302)
        self.assertFalse(OwnerSession.all_tenants.exists())

    def test_activity_is_recorded_at_most_once_a_minute(self):
        self.sign_in()
        old = timezone.now() - timedelta(minutes=5)
        OwnerSession.all_tenants.update(last_seen_at=old)
        self.get("/owner/")
        touched = self.session().last_seen_at
        self.assertGreater(touched, old)
        self.get("/owner/")
        self.assertEqual(self.session().last_seen_at, touched)

    def test_sign_out_ends_the_session_and_clears_the_cookie(self):
        self.sign_in()
        r = self.post("/owner/signout/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(OwnerSession.all_tenants.exists())
        self.assertEqual(self.client.cookies[owner_auth.COOKIE_NAME].value, "")
        self.assertEqual(self.get("/owner/").status_code, 302)
        self.assertEqual(
            CommandLog.objects.filter(command="owner.sign_out").get().outcome, "applied"
        )

    def test_sign_out_everywhere_ends_every_session_of_that_owner_only(self):
        other_owner_claim = self.submit(claimant={**OTHER, "email": "second@elsewhere.example"})
        second_listing = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        second_claim = self.submit(
            listing=second_listing, claimant={**OTHER, "email": "second@elsewhere.example"}
        )
        self.approve(second_claim, self.op(Role.EDITOR))
        elsewhere = DirectoryUser.all_tenants.get(email="second@elsewhere.example")
        OwnerSession.all_tenants.create(
            tenant=self.tenant, user=elsewhere, token_hash="z" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        first, second = Client(), Client()
        self.sign_in(first)
        self.sign_in(second)
        self.assertEqual(OwnerSession.all_tenants.filter(user=self.owner).count(), 2)
        self.post("/owner/signout/", {"everywhere": "1"}, client=first)
        self.assertFalse(OwnerSession.all_tenants.filter(user=self.owner).exists())
        self.assertEqual(self.get("/owner/", client=second).status_code, 302)
        self.assertTrue(OwnerSession.all_tenants.filter(user=elsewhere).exists())
        self.assertIsNotNone(other_owner_claim)

    def test_sign_out_is_post_only_and_needs_csrf(self):
        self.sign_in()
        self.assertEqual(self.get("/owner/signout/").status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        self.assertEqual(self.post("/owner/signout/", client=strict).status_code, 403)
        self.assertTrue(OwnerSession.all_tenants.exists())
        confirm_secret = self.issue()
        self.assertEqual(
            self.post(f"/owner/signin/{confirm_secret}/", client=Client(enforce_csrf_checks=True)).status_code,
            403,
        )

    def test_a_session_cookie_from_one_directory_means_nothing_on_another(self):
        self.sign_in()
        other = Tenant.objects.create(
            slug="other", name="Other", primary_domain="other.test",
            domain_verified_at=timezone.now(),
        )
        c = Client()
        c.cookies = self.client.cookies
        r = c.get("/owner/", HTTP_HOST="other.test")
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))
        self.assertIsNotNone(other)


class SeparationTests(_OwnerBase):
    def operator_client(self, role=Role.ADMIN):
        c = Client()
        c.force_login(self.op(role))
        return c

    def test_an_operator_session_is_not_an_owner_session(self):
        r = self.get("/owner/", client=self.operator_client())
        self.assertEqual((r.status_code, r["Location"]), (302, "/owner/signin/"))

    def test_an_owner_session_is_not_an_operator_session(self):
        self.sign_in()
        for path in ("/admin/claims/", "/admin/settings/mail/", "/admin/listing-types/"):
            r = self.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/admin/login/", r["Location"])

    def test_both_can_be_held_at_once_and_neither_ends_the_other(self):
        operator = self.operator_client()
        self.sign_in(operator)
        self.assertEqual(self.get("/owner/", client=operator).status_code, 200)
        self.assertEqual(self.get("/admin/", client=operator).status_code, 200)
        self.post("/admin/logout/", client=operator)
        self.assertEqual(self.get("/owner/", client=operator).status_code, 200)
        self.assertEqual(self.get("/admin/claims/", client=operator).status_code, 302)

    def test_signing_in_as_an_owner_does_not_touch_the_django_session(self):
        before = set(self.client.session.keys()) if self.client.cookies else set()
        self.sign_in()
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(before, set())


class DashboardTests(_OwnerBase):
    def test_the_dashboard_lists_only_the_owners_listings(self):
        second = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        third = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="third", name="Third Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        Listing.all_tenants.filter(pk=second.pk).update(owner=self.owner, status="claimed")
        self.sign_in()
        r = self.get("/owner/")
        self.assertContains(r, "Hoffman Plumbing")
        self.assertContains(r, "Second Co")
        self.assertNotContains(r, "Third Co")
        self.assertIsNotNone(third)

    def test_a_listing_page_shows_the_listing_and_what_the_owner_cannot_change(self):
        listing = Listing.all_tenants.get()
        self.sign_in()
        r = self.get(f"/owner/listings/{listing.public_id}/")
        self.assertContains(r, "Hoffman Plumbing")
        self.assertContains(r, "Details the directory manages")
        self.assertNotContains(r, "This listing is suspended")

    def test_someone_elses_listing_and_a_made_up_id_are_404(self):
        other = Listing.all_tenants.create(
            tenant=self.tenant, listing_type=self.lt, slug="second", name="Second Co",
            visibility=Listing.Visibility.PUBLISHED,
        )
        self.sign_in()
        self.assertEqual(self.get(f"/owner/listings/{other.public_id}/").status_code, 404)
        self.assertEqual(self.get("/owner/listings/listing_NOPE/").status_code, 404)

    def test_a_former_owner_loses_the_listing_the_moment_it_moves(self):
        self.sign_in()
        listing = Listing.all_tenants.get(slug="hoffman-plumbing")
        self.assertEqual(self.get(f"/owner/listings/{listing.public_id}/").status_code, 200)
        dispute = self.submit(claimant=DANA)
        self.approve(dispute, self.op(Role.EDITOR), transfer=True)
        self.assertEqual(self.get(f"/owner/listings/{listing.public_id}/").status_code, 404)
        self.assertNotContains(self.get("/owner/"), "Hoffman Plumbing")
        # The new owner has it.
        new_owner = Client()
        secret = self._issue_for(DANA["email"])
        self.post(f"/owner/signin/{secret}/", client=new_owner)
        self.assertEqual(
            self.get(f"/owner/listings/{listing.public_id}/", client=new_owner).status_code, 200
        )

    def _issue_for(self, email):
        self.age_tokens()
        self.request_link(email)
        return self.secret_from(self.link_mail(email).last())

    def test_a_suspended_listing_is_shown_but_marked(self):
        listing = Listing.all_tenants.get(slug="hoffman-plumbing")
        Listing.all_tenants.filter(pk=listing.pk).update(status=Listing.Status.SUSPENDED)
        self.sign_in()
        r = self.get(f"/owner/listings/{listing.public_id}/")
        self.assertContains(r, "This listing is suspended")


class PruneJobTests(_OwnerBase):
    def seed(self):
        now = timezone.now()
        user = self.owner
        def token(n, expires):
            return OwnerSignInToken.all_tenants.create(
                tenant=self.tenant, user=user, token_hash=f"{n}" * 64, expires_at=expires
            )
        def session(n, **kw):
            return OwnerSession.all_tenants.create(
                tenant=self.tenant, user=user, token_hash=f"s{n}" * 32, **kw
            )
        return now, {
            "old_token": token(1, now - timedelta(days=2)),
            "live_token": token(2, now + timedelta(hours=1)),
            "recent_dead_token": token(3, now - timedelta(hours=2)),
            "old_session": session(1, expires_at=now - timedelta(days=2)),
            "idle_session": session(
                2, expires_at=now + timedelta(days=5),
                last_seen_at=now - timedelta(days=3),
            ),
            "live_session": session(3, expires_at=now + timedelta(days=5), last_seen_at=now),
        }

    def test_it_deletes_what_can_no_longer_be_used_and_keeps_the_rest(self):
        now, rows = self.seed()
        result = directory_jobs.owner_auth_prune(now=now)
        self.assertEqual(result.done, 3)
        self.assertFalse(result.more)
        self.assertFalse(OwnerSignInToken.all_tenants.filter(pk=rows["old_token"].pk).exists())
        self.assertFalse(OwnerSession.all_tenants.filter(pk=rows["old_session"].pk).exists())
        self.assertFalse(OwnerSession.all_tenants.filter(pk=rows["idle_session"].pk).exists())
        for kept in ("live_token", "recent_dead_token"):
            self.assertTrue(OwnerSignInToken.all_tenants.filter(pk=rows[kept].pk).exists())
        self.assertTrue(OwnerSession.all_tenants.filter(pk=rows["live_session"].pk).exists())

    def test_it_is_bounded_and_reports_backlog(self):
        now, _ = self.seed()
        original = directory_jobs.OWNER_AUTH_CHUNK
        directory_jobs.OWNER_AUTH_CHUNK = 1
        try:
            first = directory_jobs.owner_auth_prune(now=now)
            self.assertTrue(first.more)
            second = directory_jobs.owner_auth_prune(now=now)
            third = directory_jobs.owner_auth_prune(now=now)
        finally:
            directory_jobs.OWNER_AUTH_CHUNK = original
        self.assertFalse(third.more)
        self.assertEqual(first.done + second.done + third.done, 3)

    def test_it_is_registered_and_idempotent(self):
        names = [job.name for job in build_tick_registry(out=io.StringIO()).jobs]
        self.assertIn("owner_auth_prune", names)
        now, _ = self.seed()
        directory_jobs.owner_auth_prune(now=now)
        self.assertEqual(directory_jobs.owner_auth_prune(now=now).done, 0)
