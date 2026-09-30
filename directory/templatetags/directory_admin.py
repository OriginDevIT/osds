from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def join_lines(value) -> str:
    """Render a list of option strings as newline-separated text for a
    <textarea>. Non-lists render as-is."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return value or ""


@register.inclusion_tag("directory/admin/_mail_banner.html", takes_context=True)
def mail_banner(context):
    """The unconfigured-mail banner (decisions.md §4.5). Shown to every
    active staff membership on every admin page while ``email.send`` is
    unavailable; the settings link only to a role that can edit it. An
    anonymous page (login) has no membership, so shows nothing."""
    from osds.adapters import email_available
    from tenants.models import StaffMembership

    request = context.get("request")
    membership = getattr(request, "membership", None)
    tenant = getattr(request, "tenant", None)
    if membership is None or tenant is None or email_available(tenant):
        return {"show": False}
    return {
        "show": True,
        "can_edit": membership.role >= StaffMembership.Role.ADMIN,
    }
