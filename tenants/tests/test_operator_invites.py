"""Operator invitation: the set-password link (decisions.md §4.14, #163).

Orchestrators refuse an open transaction, so these are ``TransactionTestCase``.
The view tests are raw requests through the whole stack with CSRF enforced: the
Django test client skips CSRF by default, and the §4.9 ``Origin: null`` bug was
invisible to it.
"""

from __future__ import annotations

import io
import json
import re
from datetime import timedelta

from django.contrib.messages import get_messages
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from audit.models import CommandLog, OutboundMessage, OutboxEvent
from osds.tests.mail_stub import email_send_stub
from tenants import operator_invites
from tenants.models import InstallSetup, Operator, OperatorInvite, StaffMembership, Tenant
from tenants.services import create_operator, invite_staff

CONSOLE = "console.test"
GOOD = "correct-horse-battery-9"
_FAST = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ROLE = StaffMembership.Role.EDITOR


def _secret_from(message: OutboundMessage) -> str:
    return re.search(r"/invite/([^/\s]+)/", message.body_text).group(1)


@override_settings(
    ALLOWED_HOSTS=["*"], OSDS_CONSOLE_HOST=CONSOLE, PASSWORD_HASHERS=_FAST
)
class _Base(TransactionTestCase):
    def setUp(self):
        InstallSetup.objects.create(token_hash="x" * 64, completed_at=timezone.now())
        self.root = Operator.objects.create_superuser(email="root@example.test", password="pw")
        self.tenant = Tenant.objects.create(slug="acme", name="Acme")
        self.mail = self.enterContext(email_send_stub(available=True))

    def invite(self, email="dana@example.test", tenant=None, role=ROLE):
        return invite_staff(
            tenant=tenant or self.tenant, email=email, role=role, invited_by=self.root
        )

    def mailed_secret(self, email="dana@example.test") -> str:
        return _secret_from(OutboundMessage.all_tenants.get(to_address=email, kind="operator.invite"))


class MintTests(_Base):
    def test_a_new_operator_gets_a_hashed_seven_day_invite_and_one_mail(self):
        membership = self.invite()

        invite = OperatorInvite.objects.get()
        message = OutboundMessage.all_tenants.get(kind="operator.invite")
        secret = _secret_from(message)
        self.assertEqual(invite.operator_id, membership.operator_id)
        self.assertEqual(invite.membership_id, membership.pk)
        self.assertEqual(invite.message_id, message.pk)
        self.assertEqual(invite.token_hash, operator_invites._digest(secret))
        self.assertNotIn(secret, invite.token_hash)
        self.assertEqual(len(invite.token_hash), 64)
        self.assertLessEqual(
            abs((invite.expires_at - invite.created_at) - timedelta(days=7)), timedelta(seconds=1)
        )
        self.assertEqual(message.expires_at, invite.expires_at)
        self.assertEqual(message.tenant_id, self.tenant.pk)  # the inviting tenant's mail
        self.assertEqual(message.to_address, "dana@example.test")
        self.assertIn(f"https://{CONSOLE}/invite/{secret}/", message.body_text)
        self.assertNotIn("Acme", message.subject + message.body_text)  # no inviter-typed text

    def test_an_existing_operator_gets_no_invite_and_no_mail(self):
        Operator.objects.create_user(email="dana@example.test", password="theirs-already-1")
        self.invite()
        self.assertEqual(OperatorInvite.objects.count(), 0)
        self.assertEqual(OutboundMessage.all_tenants.count(), 0)
        self.assertTrue(Operator.objects.get(email="dana@example.test").check_password("theirs-already-1"))

    def test_unavailable_mail_still_invites_but_mints_nothing_and_says_so(self):
        with email_send_stub(available=False):
            self.invite()
        self.assertEqual(StaffMembership.objects.count(), 1)
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.invited").count(), 1)
        self.assertEqual(OperatorInvite.objects.count(), 0)
        self.assertEqual(OutboundMessage.all_tenants.count(), 0)
        row = CommandLog.objects.get(command="staff.invite")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.problem, {"invite_mail": "unavailable"})

    @override_settings(OSDS_CONSOLE_HOST="")
    def test_no_console_host_is_unavailable_too(self):
        self.invite()
        self.assertEqual(OperatorInvite.objects.count(), 0)
        self.assertEqual(CommandLog.objects.get(command="staff.invite").problem, {"invite_mail": "unavailable"})

    def test_the_staff_invite_row_carries_ids_and_role_and_never_the_email(self):
        membership = self.invite()
        row = CommandLog.objects.get(command="staff.invite")
        self.assertEqual(
            row.payload, {"operator_id": membership.operator.public_id, "role": int(ROLE)}
        )
        self.assertIsNone(row.problem)
        self.assertNotIn("dana@example.test", json.dumps([row.payload, row.problem, row.actor]))

    def test_a_rejected_duplicate_logs_the_role_only(self):
        self.invite()
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            self.invite()
        rejected = CommandLog.objects.get(command="staff.invite", outcome="rejected")
        self.assertEqual(rejected.payload, {"role": int(ROLE)})

    def test_a_newer_invite_kills_the_older_link_and_its_unsent_mail(self):
        membership = self.invite()
        old = OperatorInvite.objects.get()
        old_message = old.message
        later = timezone.now() + timedelta(minutes=5)
        with transaction.atomic():
            new = operator_invites.mint_invite(
                operator=membership.operator, invited_by=self.root, membership=membership,
                mail_tenant=self.tenant, now=later,
            )
        old.refresh_from_db()
        old_message.refresh_from_db()
        self.assertLessEqual(old.expires_at, later)
        self.assertLessEqual(old_message.expires_at, later)
        self.assertIsNone(operator_invites.peek_invite(_secret_from(old_message), now=later))
        self.assertIsNotNone(operator_invites.peek_invite(new.secret, now=later))

    def test_the_admin_issue_path_is_throttled_per_address(self):
        membership = self.invite()
        op = membership.operator
        t0 = OperatorInvite.objects.get().created_at

        def mint(at, throttle=True):
            with transaction.atomic():
                return operator_invites.mint_invite(
                    operator=op, invited_by=self.root, membership=membership,
                    throttle=throttle, now=at,
                )

        with self.assertRaises(operator_invites.IssueThrottled) as ctx:
            mint(t0 + timedelta(seconds=30))
        self.assertEqual(ctx.exception.reason, "too_soon")
        self.assertEqual(OperatorInvite.objects.count(), 1)  # refused before any write
        mint(t0 + timedelta(seconds=61))
        mint(t0 + timedelta(seconds=125))
        with self.assertRaises(operator_invites.IssueThrottled) as ctx:
            mint(t0 + timedelta(seconds=190))
        self.assertEqual(ctx.exception.reason, "hourly_cap")
        mint(t0 + timedelta(hours=1, seconds=1))  # the window moves on

    def test_the_throttle_does_not_apply_to_the_command_line(self):
        create_operator(email="lee@example.test", created_by=self.root)
        for _ in range(5):
            operator_invites.issue_invite(email="lee@example.test")
        self.assertEqual(OperatorInvite.objects.count(), 5)


class SetPasswordTests(_Base):
    def test_spending_the_link_sets_the_password_and_activates_the_membership(self):
        self.invite()
        secret = self.mailed_secret()

        operator = operator_invites.set_password(secret=secret, password=GOOD)

        operator.refresh_from_db()
        self.assertTrue(operator.check_password(GOOD))
        membership = StaffMembership.objects.get()
        self.assertEqual(membership.status, StaffMembership.Status.ACTIVE)
        self.assertIsNotNone(membership.accepted_at)
        self.assertIsNotNone(OperatorInvite.objects.get().used_at)
        # staff.invited then staff.accepted (spec §4.4)
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.invited").count(), 1)
        accepted = OutboxEvent.all_tenants.get(type="staff.accepted")
        self.assertEqual(accepted.subject, operator.public_id)
        self.assertEqual(accepted.tenant_id, self.tenant.pk)
        self.assertEqual(accepted.actor, {"type": "staff", "id": operator.public_id})
        self.assertEqual(
            accepted.data,
            {"membership": {"operator_id": operator.public_id, "role": "editor", "status": "active"}},
        )
        row = CommandLog.objects.get(command="operator.set_password")
        self.assertEqual(row.outcome, "applied")
        self.assertEqual(row.result_event_id, accepted.event_id)
        self.assertEqual(row.payload, {})
        self.assertEqual(row.actor, {"type": "visitor", "id": ""})

    def test_an_admin_role_accepts_as_actor_type_admin(self):
        self.invite(role=StaffMembership.Role.ADMIN)
        operator_invites.set_password(secret=self.mailed_secret(), password=GOOD)
        self.assertEqual(OutboxEvent.all_tenants.get(type="staff.accepted").actor["type"], "admin")

    def test_the_link_works_once(self):
        self.invite()
        secret = self.mailed_secret()
        operator_invites.set_password(secret=secret, password=GOOD)
        with self.assertRaises(operator_invites.InviteRefused) as ctx:
            operator_invites.set_password(secret=secret, password="another-good-pass-7")
        self.assertEqual(ctx.exception.reason, "used")
        self.assertTrue(Operator.objects.get(email="dana@example.test").check_password(GOOD))
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 1)

    def _refused(self, secret, reason):
        with self.assertRaises(operator_invites.InviteRefused) as ctx:
            operator_invites.set_password(secret=secret, password=GOOD)
        self.assertEqual(ctx.exception.reason, reason)
        row = CommandLog.objects.filter(command="operator.set_password").order_by("-id").first()
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": reason}))
        self.assertEqual(StaffMembership.objects.get().status, StaffMembership.Status.PENDING)
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 0)

    def test_unknown(self):
        self.invite()
        self._refused("not-a-token", "unknown")

    def test_expired(self):
        self.invite()
        OperatorInvite.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self._refused(self.mailed_secret(), "expired")
        self.assertFalse(Operator.objects.get(email="dana@example.test").has_usable_password())

    def test_an_operator_who_has_since_gained_a_password_is_not_overwritten(self):
        self.invite()
        op = Operator.objects.get(email="dana@example.test")
        op.set_password("set-by-other-means-3")
        op.save()
        self._refused(self.mailed_secret(), "has_password")
        self.assertTrue(Operator.objects.get(pk=op.pk).check_password("set-by-other-means-3"))

    def test_an_inactive_operator(self):
        self.invite()
        Operator.objects.update(is_active=False)
        Operator.objects.filter(pk=self.root.pk).update(is_active=True)
        self._refused(self.mailed_secret(), "inactive")

    def test_a_weak_password_spends_nothing_and_is_never_logged(self):
        self.invite()
        secret = self.mailed_secret()
        with self.assertRaises(operator_invites.PasswordRejected) as ctx:
            operator_invites.set_password(secret=secret, password="12345678")
        self.assertTrue(ctx.exception.messages)
        self.assertIsNone(OperatorInvite.objects.get().used_at)
        self.assertFalse(Operator.objects.get(email="dana@example.test").has_usable_password())
        row = CommandLog.objects.get(command="operator.set_password")
        self.assertEqual((row.outcome, row.problem), ("rejected", {"reason": "password_rejected"}))
        self.assertNotIn("12345678", json.dumps([row.payload, row.problem]))
        operator_invites.set_password(secret=secret, password=GOOD)  # still spendable

    def test_only_the_membership_minted_with_the_operator_activates(self):
        other = Tenant.objects.create(slug="beta", name="Beta")
        self.invite()
        self.invite(tenant=other)  # the operator now exists: pending, no mail
        self.assertEqual(OperatorInvite.objects.count(), 1)

        operator_invites.set_password(secret=self.mailed_secret(), password=GOOD)

        self.assertEqual(
            StaffMembership.objects.get(tenant=self.tenant).status, StaffMembership.Status.ACTIVE
        )
        self.assertEqual(
            StaffMembership.objects.get(tenant=other).status, StaffMembership.Status.PENDING
        )
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 1)


class IssueCommandTests(_Base):
    def _run(self, email):
        out = io.StringIO()
        call_command("issue_operator_invite", email=email, stdout=out)
        return out.getvalue()

    def test_it_prints_an_https_link_that_works_and_sends_no_mail(self):
        create_operator(email="lee@example.test", created_by=self.root)
        out = self._run("Lee@Example.test")

        secret = re.search(rf"https://{CONSOLE}/invite/([^/\s]+)/", out).group(1)
        self.assertEqual(OutboundMessage.all_tenants.count(), 0)
        invite = OperatorInvite.objects.get()
        self.assertIsNone(invite.message_id)
        self.assertIsNone(invite.membership_id)
        row = CommandLog.objects.get(command="operator.issue_invite")
        self.assertEqual(row.actor, {"type": "system", "id": "issue_operator_invite"})
        self.assertEqual(row.outcome, "applied")
        self.assertNotIn(secret, json.dumps([row.payload, row.problem]))
        self.assertNotIn("lee@example.test", json.dumps([row.payload, row.problem, row.actor]))

        operator_invites.set_password(secret=secret, password=GOOD)  # no membership, no event
        self.assertTrue(Operator.objects.get(email="lee@example.test").check_password(GOOD))
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 0)

    def test_a_lapsed_invite_is_reissued_to_the_same_pending_membership(self):
        # An invite minted with its membership, then lapsed, is picked up again.
        self.invite(email="lee@example.test")
        OperatorInvite.objects.filter(membership__operator__email="lee@example.test").update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        out = self._run("lee@example.test")
        secret = re.search(r"/invite/([^/\s]+)/", out).group(1)
        operator_invites.set_password(secret=secret, password=GOOD)
        self.assertEqual(
            StaffMembership.objects.get(operator__email="lee@example.test").status,
            StaffMembership.Status.ACTIVE,
        )
        self.assertEqual(OutboxEvent.all_tenants.filter(type="staff.accepted").count(), 1)

    @override_settings(OSDS_CONSOLE_HOST="")
    def test_with_no_console_host_it_prints_the_path(self):
        create_operator(email="lee@example.test", created_by=self.root)
        self.assertIn("/invite/", self._run("lee@example.test"))

    def test_refusals_are_command_errors_and_rejected_rows(self):
        with self.assertRaises(CommandError):
            self._run("nobody@example.test")
        Operator.objects.create_user(email="has@example.test", password="theirs-already-1")
        with self.assertRaises(CommandError):
            self._run("has@example.test")
        reasons = [
            r.problem["reason"]
            for r in CommandLog.objects.filter(command="operator.issue_invite", outcome="rejected").order_by("id")
        ]
        self.assertEqual(reasons, ["unknown_operator", "has_password"])
        self.assertEqual(OperatorInvite.objects.count(), 0)


class AdminResponseTests(_Base):
    """The admin form says the same thing whether or not the address had an
    account, with mail available and without (spec §4.4)."""

    def _post(self, email):
        c = Client()
        c.force_login(self.root)
        r = c.post(
            "/admin/tenants/staffmembership/add/",
            {"tenant": self.tenant.pk, "email": email, "role": str(int(ROLE))},
            HTTP_HOST=CONSOLE,
        )
        # The only thing allowed to differ is the address the operator typed.
        return r, [(m.level, m.message.replace(email, "ADDR")) for m in get_messages(r.wsgi_request)]

    def _compare(self):
        Operator.objects.create_user(email="old@example.test", password="theirs-already-1")
        r1, m1 = self._post("old@example.test")
        r2, m2 = self._post("new@example.test")
        self.assertEqual((r1.status_code, r1["Location"]), (r2.status_code, r2["Location"]))
        self.assertEqual(r1.content, r2.content)
        self.assertEqual(m1, m2)
        return m1

    def test_identical_with_mail_available_and_no_warning(self):
        msgs = self._compare()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(OperatorInvite.objects.count(), 1)  # only the new address

    def test_identical_with_mail_unavailable_and_the_warning_names_the_directory_not_the_address(self):
        with email_send_stub(available=False):
            msgs = self._compare()
        self.assertEqual(len(msgs), 2)
        self.assertIn("Mail is not set up for this directory", msgs[1][1])
        self.assertNotIn("example.test", msgs[1][1])
        self.assertEqual(OperatorInvite.objects.count(), 0)


class InviteViewTests(_Base):
    """Raw requests: CSRF enforced, https, the real Origin header."""

    def setUp(self):
        super().setUp()
        self.invite()
        self.secret = self.mailed_secret()
        self.url = f"/invite/{self.secret}/"

    def _client(self):
        return Client(enforce_csrf_checks=True)

    def _get(self, c, url=None):
        return c.get(url or self.url, HTTP_HOST=CONSOLE, secure=True)

    def _post(self, c, data, origin=f"https://{CONSOLE}", url=None):
        extra = {} if origin is None else {"HTTP_ORIGIN": origin}
        return c.post(url or self.url, data, HTTP_HOST=CONSOLE, secure=True, **extra)

    def _form(self, c, p1=GOOD, p2=GOOD):
        token = c.cookies["csrftoken"].value
        return {"password1": p1, "password2": p2, "csrfmiddlewaretoken": token}

    @override_settings(CSRF_COOKIE_SECURE=True)
    def test_get_spends_nothing_and_the_page_is_private(self):
        c = self._client()
        resp = self._get(c)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertTrue(resp.cookies["csrftoken"]["secure"])
        self.assertIsNone(OperatorInvite.objects.get().used_at)
        self.assertEqual(CommandLog.objects.filter(command="operator.set_password").count(), 0)
        self._get(c)  # a scanner following it twice changes nothing
        self.assertIsNone(OperatorInvite.objects.get().used_at)

    def test_a_good_post_sets_the_password_and_opens_no_session(self):
        c = self._client()
        self._get(c)
        resp = self._post(c, self._form(c))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/login/")
        self.assertNotIn("sessionid", resp.cookies)  # no auto sign-in
        self.assertNotIn("_auth_user_id", c.session)
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertTrue(Operator.objects.get(email="dana@example.test").check_password(GOOD))
        self.assertEqual(StaffMembership.objects.get().status, StaffMembership.Status.ACTIVE)
        # and the login page tells them to sign in
        page = c.get("/login/", HTTP_HOST=CONSOLE, secure=True)
        self.assertContains(page, "Password set. Sign in to continue.")

    def test_a_post_with_origin_null_is_refused_and_spends_nothing(self):
        c = self._client()
        self._get(c)
        resp = self._post(c, self._form(c), origin="null")
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(OperatorInvite.objects.get().used_at)

    def test_a_post_with_no_csrf_token_is_refused(self):
        c = self._client()
        self._get(c)
        resp = self._post(c, {"password1": GOOD, "password2": GOOD})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(OperatorInvite.objects.get().used_at)

    def test_a_weak_or_mismatched_password_rerenders_and_the_link_stays_live(self):
        c = self._client()
        self._get(c)
        weak = self._post(c, self._form(c, "12345678", "12345678"))
        self.assertEqual(weak.status_code, 200)
        self.assertContains(weak, "errors")
        self.assertEqual(weak["Referrer-Policy"], "same-origin")
        mismatch = self._post(c, self._form(c, GOOD, GOOD + "x"))
        self.assertContains(mismatch, "do not match")
        self.assertIsNone(OperatorInvite.objects.get().used_at)
        self.assertEqual(self._post(c, self._form(c)).status_code, 302)

    def test_every_dead_link_is_the_same_400_with_the_private_headers(self):
        c = self._client()
        self._get(c)
        self._post(c, self._form(c))  # spend it
        pages = []
        for url in (self.url, "/invite/not-a-token/"):
            resp = self._get(c, url)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp["Referrer-Policy"], "same-origin")
            self.assertIn("no-store", resp["Cache-Control"])
            pages.append(resp.content)
        post = self._post(c, self._form(c), url="/invite/not-a-token/")
        self.assertEqual(post.status_code, 400)
        pages.append(post.content)
        self.assertEqual(len(set(pages)), 1)

    def test_the_route_exists_only_on_the_console_host(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(primary_domain="acme.test")
        resp = Client().get(self.url, HTTP_HOST="acme.test", secure=True)
        self.assertEqual(resp.status_code, 404)
