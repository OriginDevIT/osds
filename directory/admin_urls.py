from django.urls import path

from directory import admin_views as views

app_name = "directory_admin"

urlpatterns = [
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
]
