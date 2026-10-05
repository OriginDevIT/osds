"""Print a set-password link for an operator who has no password.

For an install whose mail cannot carry the invitation, an invite that expired,
and operators made through ``create_operator``, which mints no invite. Run on
the host: shell access is already full control, so there is no throttle and no
mail. The link is shown once and stored only as a digest (decisions.md §4.14).
The scheme is always https; an HTTP-only console has no usable link here and
the operator must type the path ``/invite/<token>/`` on it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants import operator_invites


class Command(BaseCommand):
    help = "Print a one-time set-password link for an operator with no password."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The operator's email.")

    def handle(self, *args, email: str, **options):
        try:
            issued = operator_invites.issue_invite(email=email)
        except operator_invites.IssueRefused as exc:
            raise CommandError(
                {
                    "unknown_operator": "No operator has that email.",
                    "inactive": "That operator is inactive.",
                    "has_password": "That operator already has a password; "
                    "their credential is not replaced here.",
                }[exc.reason]
            )
        url = operator_invites.invite_url(issued.secret)
        expires = issued.invite.expires_at.strftime("%Y-%m-%d %H:%M")
        self.stdout.write("")
        if url:
            self.stdout.write(f"  Set-password link:  {url}")
        else:
            self.stdout.write(
                f"  Set-password path:  /invite/{issued.secret}/  "
                "(OSDS_CONSOLE_HOST is unset; open it on the console host)"
            )
        self.stdout.write(f"  Works once; expires {expires} UTC.")
        self.stdout.write("")
