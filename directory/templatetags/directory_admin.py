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
