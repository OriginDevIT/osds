"""Plain ``forms.Form`` classes for the console admin add pages.

Never ``ModelForm`` -- a ``ModelForm`` would ``save()`` straight to the ORM and
bypass ``tenants.services`` (event emission + command log, #146). These forms
only collect and validate input; the ``ModelAdmin.add_view`` hands the cleaned
data to a service function.
"""

from __future__ import annotations

from django import forms

from tenants.models import StaffMembership, Tenant


class TenantAddForm(forms.Form):
    name = forms.CharField(max_length=200)
    slug = forms.SlugField(max_length=100)
    mode = forms.ChoiceField(
        choices=Tenant.Mode.choices, initial=Tenant.Mode.SINGLE
    )


class OperatorAddForm(forms.Form):
    # No is_superadmin field: elevation is installation-scoped authorization
    # and is out of scope for this surface (#165).
    email = forms.EmailField()
    name = forms.CharField(max_length=200, required=False)


class StaffInviteForm(forms.Form):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.all())
    email = forms.EmailField()
    role = forms.TypedChoiceField(choices=StaffMembership.Role.choices, coerce=int)
