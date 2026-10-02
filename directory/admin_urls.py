from django.urls import path

from directory import admin_views as views
from directory import auth_views
from directory import claim_admin_views as claim_views
from tenants import mail_views

app_name = "directory_admin"

urlpatterns = [
    path("", auth_views.index, name="index"),
    path("login/", auth_views.login_view, name="login"),
    path("logout/", auth_views.logout_view, name="logout"),
    # claims review (PR 4a)
    path("claims/", claim_views.claims_queue, name="claims-queue"),
    path("claims/<str:public_id>/", claim_views.claim_detail, name="claim-detail"),
    path(
        "claims/<str:public_id>/approve/",
        claim_views.claim_approve,
        name="claim-approve",
    ),
    path(
        "claims/<str:public_id>/reject/",
        claim_views.claim_reject,
        name="claim-reject",
    ),
    path(
        "moderation/<str:public_id>/",
        claim_views.moderation_detail,
        name="moderation-detail",
    ),
    path(
        "moderation/<str:public_id>/unlock/",
        claim_views.moderation_unlock,
        name="moderation-unlock",
    ),
    path(
        "moderation/<str:public_id>/unblock/",
        claim_views.moderation_unblock,
        name="moderation-unblock",
    ),
    path("settings/mail/", mail_views.mail_settings, name="mail-settings"),
    path("listing-types/", views.listing_type_list, name="type-list"),
    path("listing-types/new/", views.listing_type_create, name="type-create"),
    path("listing-types/<slug:key>/", views.listing_type_edit, name="type-edit"),
    path(
        "listing-types/<slug:key>/delete/",
        views.listing_type_delete,
        name="type-delete",
    ),
    path(
        "listing-types/<slug:key>/fields/",
        views.schema_builder,
        name="type-fields",
    ),
    path(
        "listing-types/<slug:key>/fields/row/",
        views.field_row,
        name="type-fields-row",
    ),
    path(
        "listing-types/<slug:key>/categories/",
        views.category_list,
        name="category-list",
    ),
    path(
        "listing-types/<slug:key>/categories/new/",
        views.category_create,
        name="category-create",
    ),
    path("categories/<str:public_id>/", views.category_edit, name="category-edit"),
    path(
        "categories/<str:public_id>/delete/",
        views.category_delete,
        name="category-delete",
    ),
    # listings
    path(
        "listing-types/<slug:key>/listings/",
        views.listing_list,
        name="listing-list",
    ),
    path(
        "listing-types/<slug:key>/listings/new/",
        views.listing_create,
        name="listing-create",
    ),
    path(
        "listing-types/<slug:key>/listings/<str:public_id>/edit/",
        views.listing_edit,
        name="listing-edit",
    ),
    path(
        "listing-types/<slug:key>/listings/<str:public_id>/publish/",
        views.listing_publish,
        name="listing-publish",
    ),
    path(
        "listing-types/<slug:key>/listings/<str:public_id>/unpublish/",
        views.listing_unpublish,
        name="listing-unpublish",
    ),
    path(
        "listing-types/<slug:key>/listings/<str:public_id>/media/add/",
        views.listing_media_add,
        name="listing-media-add",
    ),
    path(
        "listing-types/<slug:key>/listings/<str:public_id>/media/"
        "<str:asset_public_id>/remove/",
        views.listing_media_remove,
        name="listing-media-remove",
    ),
    # CSV import (upload + mapping; the worker is a later PR)
    path("imports/", views.import_list, name="import-list"),
    path("imports/new/", views.import_create, name="import-create"),
    path("imports/<str:public_id>/", views.import_detail, name="import-detail"),
    path(
        "imports/<str:public_id>/mapping/",
        views.import_mapping,
        name="import-mapping",
    ),
    path("imports/<str:public_id>/run/", views.import_run, name="import-run"),
    path(
        "imports/<str:public_id>/rollback/",
        views.import_rollback,
        name="import-rollback",
    ),
]
