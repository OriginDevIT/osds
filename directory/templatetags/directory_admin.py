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


@register.inclusion_tag("directory/admin/_storage_banner.html", takes_context=True)
def storage_banner(context):
    """The broken-storage banner. Shown on every admin page while the tenant's
    configured storage cannot be built (a missing SDK, credential or setting).
    The reason and the settings link go only to a role that can edit storage."""
    from directory.storage import storage_problem
    from tenants.models import StaffMembership

    request = context.get("request")
    membership = getattr(request, "membership", None)
    tenant = getattr(request, "tenant", None)
    if membership is None or tenant is None:
        return {"show": False}
    problem = storage_problem(tenant)
    if not problem:
        return {"show": False}
    can_edit = membership.role >= StaffMembership.Role.ADMIN
    return {"show": True, "can_edit": can_edit, "problem": problem if can_edit else ""}
