from django.urls import path

from billing import admin_views as views

app_name = "billing_admin"

urlpatterns = [
    path("tiers/", views.tier_list, name="tier-list"),
    path("tiers/new/", views.tier_create, name="tier-create"),
    path("tiers/<slug:key>/", views.tier_edit, name="tier-edit"),
    path("tiers/<slug:key>/delete/", views.tier_delete, name="tier-delete"),
    path("entitlements/", views.entitlement_list, name="entitlement-list"),
    path("entitlements/grant/", views.entitlement_grant, name="entitlement-grant"),
    path("entitlements/<str:public_id>/", views.entitlement_detail, name="entitlement-detail"),
    path("entitlements/<str:public_id>/revoke/", views.entitlement_revoke, name="entitlement-revoke"),
    path("entitlements/<str:public_id>/override/", views.entitlement_override, name="entitlement-override"),
]
