"""The one check of ``SettingField.pattern`` (decisions.md §4.11).

Both settings services -- ``billing.settings_service`` and
``tenants.adapter_settings`` -- call this, so a field's declared shape means the
same thing on either page: the whole value must match, and a refusal names the
field and never the value (it may be a secret).
"""

from __future__ import annotations

import re


def pattern_error(field, value) -> "str | None":
    """The error for ``value`` typed into ``field``, or ``None`` if it is fine.

    Only a typed, non-empty value is checked: a blank means "keep" or "clear"
    and is the required-field rule's business, and a bool has no shape."""
    if not field.pattern or field.kind == "bool" or value in (None, ""):
        return None
    if re.fullmatch(field.pattern, str(value)):
        return None
    return f"{field.label} is not in the expected format."
