"""Listings and everything that hangs off them.

Every model here is tenant-scoped: a ``tenant`` FK plus the scoped default
manager (``objects``) and the ``all_tenants`` escape hatch.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone

from osds.db import TenantScopedManager
from osds.ids import (
    cat_id,
    claim_id,
    mod_id,
    cns_id,
    imp_id,
    lead_id,
    listing_id,
    lt_id,
    media_id,
    usr_id,
)


class ListingType(models.Model):
    """What a directory is *of* (spec §4.5). Carries the per-type field schema
    in ``fields``; the values live on ``Listing.custom_fields``."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="listing_types"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=lt_id
    )
    key = models.SlugField(max_length=50)
    label_singular = models.CharField(max_length=100)
    label_plural = models.CharField(max_length=100)
    path_segment = models.SlugField(max_length=50)
    claimable = models.BooleanField(default=True)
    # Array of field descriptors: {key, label, type, required, public,
    # searchable, options?}. Validated against the closed type set by the
    # listing-type service (block 2).
    fields = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "listing_types"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key"], name="uniq_listingtype_tenant_key"
            ),
            models.UniqueConstraint(
                fields=["tenant", "path_segment"],
                name="uniq_listingtype_tenant_path_segment",
            ),
        ]

    def __str__(self) -> str:
        return self.key


class Category(models.Model):
    """How listings of one type are grouped for browsing (spec §4.5). A
    tenant-configured tree, scoped to a listing type."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="categories"
    )
    listing_type = models.ForeignKey(
        ListingType, on_delete=models.CASCADE, related_name="categories"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=cat_id
    )
    slug = models.SlugField(max_length=100)
    name = models.CharField(max_length=200)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "categories"
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "listing_type", "slug"],
                name="uniq_category_tenant_type_slug",
            ),
        ]

    def __str__(self) -> str:
        return self.slug


class PathRedirect(models.Model):
    """A permanent (301) prefix redirect for public URLs, written when a
    ``ListingType.path_segment`` changes or a tenant gains its second type
    (spec §4.5). The public router consumes these in a later PR.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="path_redirects"
    )
    # "" is the pre-multi-type root; otherwise "/<old-segment>".
    old_prefix = models.CharField(max_length=64)
    new_prefix = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "path_redirects"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "old_prefix"],
                name="uniq_pathredirect_tenant_old_prefix",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.old_prefix or '/'} -> {self.new_prefix}"


class DirectoryUser(models.Model):
    """A person who owns, or seeks to own, a listing (spec §4.3). Belongs to
    exactly one tenant. Not a Django auth user and holds no credential -- owner
    login is email OTP."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="directory_users"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=usr_id
    )
    email = models.EmailField()
    name = models.CharField(max_length=200, blank=True)
    phone_e164 = models.CharField(max_length=16, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "users"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "email"], name="uniq_user_tenant_email"
            ),
        ]

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)


class OwnerSignInToken(models.Model):
    """A single-use emailed sign-in link for an owner (spec §4.3, decisions.md
    §4.9). Only the SHA-256 of the token is stored: the plaintext is a
    256-bit random value that exists in the mail and nowhere else. The token
    is consumed by a POST from the confirm page, never by following the link,
    so a mail scanner's GET cannot burn it."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="owner_signin_tokens"
    )
    user = models.ForeignKey(
        DirectoryUser, on_delete=models.CASCADE, related_name="signin_tokens"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    message = models.ForeignKey(
        "audit.OutboundMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owner_signin_tokens",
    )

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "owner_signin_tokens"
        indexes = [models.Index(fields=["tenant", "user", "created_at"])]

    def __str__(self) -> str:
        return f"signin:{self.user_id}"


class OwnerSession(models.Model):
    """An owner's session, separate from the operator's Django session
    (decisions.md §4.9): its own table and its own cookie, so neither can
    satisfy the other's guard. Expires after 12 hours without activity and
    after 30 days regardless."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="owner_sessions"
    )
    user = models.ForeignKey(
        DirectoryUser, on_delete=models.CASCADE, related_name="owner_sessions"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_seen_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "owner_sessions"
        indexes = [models.Index(fields=["tenant", "user"])]

    def __str__(self) -> str:
        return f"session:{self.user_id}"


class ImportBatch(models.Model):
    """A CSV upload and its outcome (spec §4.1.1, import.* events). The pipeline
    is block 3; the model exists now so provenance FKs have a target."""

    class Status(models.TextChoices):
        MAPPING = "mapping", "Awaiting column mapping"
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        ROLLED_BACK = "rolled_back", "Rolled back"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="import_batches"
    )
    # The type every row in this file is upserted as. Nullable only so the
    # migration applies to a zero-row table; the upload form always sets it.
    listing_type = models.ForeignKey(
        ListingType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=imp_id
    )
    source = models.CharField(max_length=20, default="csv")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.MAPPING
    )
    original_filename = models.CharField(max_length=255, blank=True)
    stored_path = models.CharField(max_length=500, blank=True)
    # Header row read from the file at upload (no data rows are parsed then);
    # drives the mapping UI. ``column_mapping`` is {csv header: upsert target}.
    detected_headers = models.JSONField(default=list, blank=True)
    delimiter = models.CharField(max_length=4, default=",")
    encoding = models.CharField(max_length=32, default="utf-8-sig")
    has_header = models.BooleanField(default=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    # Resume cursor for the chunked worker row loop: data rows consumed so far.
    # A pass processes the next ``ROWS_PER_PASS`` rows and advances this.
    processed_row_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    # Per-row failures; feeds import.completed's ``errors`` array.
    errors = models.JSONField(default=list, blank=True)
    # Non-error provenance notes (e.g. a row reprocessed after a worker
    # restart). Kept out of ``errors`` so an adapter reading import.completed
    # never reads a restart note as a failure.
    notes = models.JSONField(default=list, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_batches",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    rolled_back_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_batches_rolled_back",
    )
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "import_batches"

    def __str__(self) -> str:
        return self.public_id


class ListingQuerySet(models.QuerySet):
    def published(self):
        """The only rows the public site may ever show (ruling 13)."""
        return self.filter(visibility=Listing.Visibility.PUBLISHED)


ListingManager = TenantScopedManager.from_queryset(ListingQuerySet)


class Listing(models.Model):
    """The listing record and its published state (spec §4.1). The fixed common
    core lives in columns; type-specific fields live in ``custom_fields``."""

    class Status(models.TextChoices):
        UNCLAIMED = "unclaimed", "Unclaimed"
        CLAIMED = "claimed", "Claimed"
        SUSPENDED = "suspended", "Suspended"

    class Visibility(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        HIDDEN = "hidden", "Hidden"

    class GeoPrecision(models.TextChoices):
        ROOFTOP = "rooftop", "Rooftop"
        STREET = "street", "Street"
        LOCALITY = "locality", "Locality"
        NONE = "none", "None"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual entry"
        CSV_IMPORT = "csv_import", "CSV import"
        OWNER_SUBMISSION = "owner_submission", "Owner submission"
        API = "api", "Write API"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="listings"
    )
    listing_type = models.ForeignKey(
        ListingType, on_delete=models.PROTECT, related_name="listings"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=listing_id
    )
    slug = models.SlugField(max_length=200)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.UNCLAIMED
    )
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.DRAFT
    )
    # Denormalised. Resolved from the entitlement record and written ONLY by the
    # entitlement service -- there is no code path that sets tier directly
    # (CLAUDE.md invariant 2).
    current_tier = models.ForeignKey(
        "billing.Tier",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="listings",
    )
    owner = models.ForeignKey(
        DirectoryUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_listings",
    )
    categories = models.ManyToManyField(
        Category, blank=True, related_name="listings"
    )

    # Location -- flattened decimal fields, no GeoDjango (decisions.md §4).
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    locality = models.CharField(max_length=120, blank=True)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True)  # ISO 3166-1 alpha-2
    lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    lon = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    geo_precision = models.CharField(
        max_length=10, choices=GeoPrecision.choices, default=GeoPrecision.NONE
    )

    # Contact -- redaction is a serialisation concern, not storage.
    phone_e164 = models.CharField(max_length=16, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    social = models.JSONField(default=list, blank=True)  # ordered [{platform,url,label}]

    external_profiles = models.JSONField(default=dict, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    media = models.JSONField(default=dict, blank=True)  # {logo, cover, gallery}
    reviews_disabled = models.BooleanField(default=False)

    # Provenance (spec §4.1.1).
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL
    )
    import_batch = models.ForeignKey(
        ImportBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="listings",
    )
    submitted_by = models.ForeignKey(
        DirectoryUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_listings",
    )
    provenance_notes = models.TextField(blank=True)

    # Full-text document (spec §12). Application-computed by
    # directory.search.recompute_search_vector on every write; never a
    # generated column or trigger (ruling 11).
    search_vector = SearchVectorField(null=True, editable=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ListingManager()
    all_tenants = models.Manager.from_queryset(ListingQuerySet)()

    class Meta:
        db_table = "listings"
        indexes = [
            models.Index(fields=["tenant", "visibility", "status"]),
            GinIndex(fields=["search_vector"], name="listings_search_gin"),
            GinIndex(
                name="listings_name_trgm",
                fields=["name"],
                opclasses=["gin_trgm_ops"],
            ),
            models.Index(
                fields=["lat", "lon"],
                name="listings_lat_lon",
                condition=models.Q(lat__isnull=False),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "listing_type", "slug"],
                name="uniq_listing_tenant_type_slug",
            ),
            # geo_precision is "none" iff both coordinates are null.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        geo_precision="none", lat__isnull=True, lon__isnull=True
                    )
                    | (
                        ~models.Q(geo_precision="none")
                        & models.Q(lat__isnull=False)
                        & models.Q(lon__isnull=False)
                    )
                ),
                name="listing_geo_precision_matches_coordinates",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class MediaAsset(models.Model):
    """One uploaded image attached to a listing.

    ``MediaAsset`` rows are canonical for asset facts -- bytes, dimensions,
    processing state, storage location, provenance. ``Listing.media`` is a
    denormalised projection rebuilt from the ``ready`` rows on every attach or
    detach; it holds the render-ready refs the public page and the
    ``listing.updated`` JSON Patch consume (spec §4.1, decisions.md §4.1).

    ``media.*`` events stay deferred (spec §3.4): a row moving ``pending →
    ready`` is internal state, and attaching or detaching emits
    ``listing.updated`` -- nothing else.
    """

    class Role(models.TextChoices):
        LOGO = "logo", "Logo"
        COVER = "cover", "Cover"
        GALLERY = "gallery", "Gallery"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        # Reserved for the abuse-scan pipeline (spec §15.2). No code path sets
        # it today and no scan hook exists -- the value only holds its place.
        QUARANTINED = "quarantined", "Quarantined"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="media_assets"
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="media_assets"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=media_id
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )

    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)  # sniffed, not trusted
    byte_size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True)
    # Tenant-relative key within the resolved backend -- never an absolute URL.
    storage_key = models.CharField(max_length=500, blank=True)
    # Worker-produced sizes land here once the media pipeline exists (spec
    # §15.2). Empty until then.
    derivatives = models.JSONField(default=dict, blank=True)
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="media_assets_uploaded",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "media_assets"
        indexes = [models.Index(fields=["tenant", "listing", "role"])]
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.public_id} ({self.role})"


class ImportBatchListing(models.Model):
    """One listing a batch created or updated, with the pre-image needed to
    undo it (spec §3.3, decisions.md "Rollback restores updated rows").

    Written inside ``upsert_listing``'s transaction whenever ``import_batch`` is
    set: ``action="created"`` for a create (``pre_image`` null -- rollback
    deletes the row), ``action="updated"`` for an update (``pre_image`` is the
    full §4.1 projection as the row stood *before* this batch touched it --
    rollback assigns it back). First touch wins, so a row this batch creates
    and then updates stays ``created``.

    ``Listing.import_batch`` only records the batch that *created* a row; this
    table is the only record of which rows a batch *updated*. ``pre_image`` is
    a second copy of personal data and is nulled at 90 days (spec §11.2) by
    ``directory.importing.null_import_pre_images``, run daily by the worker's
    ``import_pre_image_retention`` tick job (#193) -- a batch whose pre-images
    have been nulled can no longer be rolled back.
    """

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="import_batch_listings",
    )
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name="row_provenance"
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="import_provenance"
    )
    action = models.CharField(max_length=8, choices=Action.choices)
    pre_image = models.JSONField(null=True, blank=True)
    pre_image_nulled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "import_batch_listings"
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "listing"],
                name="uniq_importbatchlisting_batch_listing",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "batch"])]

    def __str__(self) -> str:
        return f"{self.batch_id}:{self.listing_id} ({self.action})"


class Claim(models.Model):
    """Acquiring a verified human owner for a listing (spec §9)."""

    class Status(models.TextChoices):
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ABANDONED = "abandoned", "Abandoned"
        DISPUTED = "disputed", "Disputed"
        # An approved claim whose ownership was transferred to a later,
        # approved dispute (decisions.md §4.6). Keeps "one approved claim per
        # listing" true without losing the history.
        SUPERSEDED = "superseded", "Superseded"

    class Method(models.TextChoices):
        MANUAL = "manual", "Manual review"
        PHONE_OTP = "phone_otp", "Phone OTP"
        DOMAIN_EMAIL = "domain_email", "Domain email"
        GBP_OAUTH = "gbp_oauth", "Google Business Profile"
        POSTCARD = "postcard", "Postcard"

    class ManualMethod(models.TextChoices):
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"
        POSTCARD = "postcard", "Postcard"
        WEBSITE = "website", "Website"
        SOCIAL = "social", "Social"
        IN_PERSON = "in_person", "In person"
        DOCUMENT = "document", "Document"
        OTHER = "other", "Other"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="claims"
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="claims"
    )
    claimant = models.ForeignKey(
        DirectoryUser, on_delete=models.PROTECT, related_name="claims"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=claim_id
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_VERIFICATION,
    )
    method = models.CharField(max_length=16, choices=Method.choices)
    role_claimed = models.CharField(max_length=40, default="owner")

    # Verification lifecycle. expires_at is computed by core, never the caller
    # (spec §9.5). The OTP mechanics are wired in block 3.
    verification_started_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Wrong-entry tally only (decisions.md §4.4) -- a correct code leaves it.
    # The per-(listing, claimant) cooldown/lock counters that reset are on
    # ClaimVerificationGuard; this is the reviewer-facing lifetime total.
    attempts = models.PositiveIntegerField(default=0)
    # Keyed hash of the live code, cleared on success, on the 5th wrong
    # entry, and on supersede (decisions.md §4.4). Never the plaintext.
    code_hash = models.CharField(max_length=64, blank=True)
    code_message = models.ForeignKey(
        "audit.OutboundMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claim_codes",
    )

    # Manual path (spec §9.3). notes are required for method=manual; the service
    # enforces that in block 3.
    manual_method_used = models.CharField(
        max_length=20, choices=ManualMethod.choices, blank=True
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims_verified",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_notes = models.TextField(blank=True)
    evidence_ref = models.CharField(max_length=500, blank=True)

    decided_at = models.DateTimeField(null=True, blank=True)
    # The operator who approved or rejected. Null for an approval by code
    # verification and for a system rejection (a losing claim).
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims_decided",
    )
    rejection_reason = models.TextField(blank=True)

    # What the claimant asked for, and why the claim is in front of a human
    # (decisions.md §4.6). ``requested_method`` otherwise survives only in the
    # command-log payload, which is nulled at 90 days.
    requested_method = models.CharField(max_length=16, blank=True)
    review_reason = models.CharField(max_length=24, blank=True)

    last_step = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "claims"
        constraints = [
            # One approved claim per listing, enforced by the database. A
            # transfer moves the old one to SUPERSEDED first (#48).
            models.UniqueConstraint(
                fields=["listing"],
                condition=models.Q(status="approved"),
                name="uniq_one_approved_claim_per_listing",
            ),
        ]

    def __str__(self) -> str:
        return self.public_id


class ClaimVerificationGuard(models.Model):
    """Per-``(listing, claimant)`` counters for ``domain_email`` verification
    (spec §9.6, decisions.md §4.4).

    Keyed on the ``DirectoryUser`` FK, not a second copy of the address.
    Taken ``select_for_update`` on every verify or start-verification entry.
    ``rejections``/``claim_blocked_at`` are populated starting PR 4 (repeated
    rejection, spec §9.6) -- the columns exist now so there is one migration,
    but this PR never writes them, only refuses a submit that finds
    ``claim_blocked_at`` already set.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="claim_verification_guards",
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="verification_guards"
    )
    claimant = models.ForeignKey(
        DirectoryUser, on_delete=models.CASCADE, related_name="verification_guards"
    )

    wrong_entries = models.PositiveSmallIntegerField(default=0)
    cooldowns = models.PositiveSmallIntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    verification_locked_at = models.DateTimeField(null=True, blank=True)

    # PR 4 (spec §9.6 "Repeated rejection").
    rejections = models.PositiveSmallIntegerField(default=0)
    claim_blocked_at = models.DateTimeField(null=True, blank=True)

    last_code_sent_at = models.DateTimeField(null=True, blank=True)
    codes_sent_window_start = models.DateTimeField(null=True, blank=True)
    codes_sent_in_window = models.PositiveSmallIntegerField(default=0)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "claim_verification_guards"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "listing", "claimant"],
                name="uniq_guard_listing_claimant",
            ),
        ]

    def __str__(self) -> str:
        return f"guard:{self.listing_id}:{self.claimant_id}"


class ModerationItem(models.Model):
    """A queued decision for a human (spec §3.3 ``moderation.*``, §9.4, §9.6;
    decisions.md §4.6). Three item types exist: a dispute on a claimed listing,
    the verification lock on a ``(listing, claimant)`` pair, and the
    three-rejection block on the same pair.
    """

    class ItemType(models.TextChoices):
        CLAIM_DISPUTE = "claim_dispute", "Claim dispute"
        VERIFICATION_LOCK = "verification_lock", "Verification lock"
        CLAIM_BLOCK = "claim_block", "Claim block"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DECIDED = "decided", "Decided"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="moderation_items"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=mod_id
    )
    item_type = models.CharField(max_length=20, choices=ItemType.choices)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.OPEN
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="moderation_items"
    )
    claimant = models.ForeignKey(
        DirectoryUser, on_delete=models.CASCADE, related_name="moderation_items"
    )
    # The claim that caused the item. Every dispute has one; a lock or a
    # block records the claim that tripped it.
    claim = models.ForeignKey(
        Claim,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="moderation_items",
    )
    rules_triggered = models.JSONField(default=list, blank=True)
    priority = models.CharField(max_length=10, default="normal")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="moderation_items_decided",
    )
    decision = models.CharField(max_length=20, blank=True)
    rationale = models.TextField(blank=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "moderation_items"
        constraints = [
            models.UniqueConstraint(
                fields=["claim"],
                condition=models.Q(status="open", item_type="claim_dispute"),
                name="uniq_open_dispute_item_per_claim",
            ),
            models.UniqueConstraint(
                fields=["tenant", "listing", "claimant", "item_type"],
                condition=models.Q(
                    status="open",
                    item_type__in=["verification_lock", "claim_block"],
                ),
                name="uniq_open_pair_item_per_type",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status", "item_type"])]

    def __str__(self) -> str:
        return self.public_id


class Lead(models.Model):
    """Consumer contact delivered to a business (spec §3.3, lead.captured).
    ``consent`` is required -- captured as Consent rows."""

    class Kind(models.TextChoices):
        CONTACT_FORM = "contact_form", "Contact form"
        PHONE_REVEAL = "phone_reveal", "Phone reveal"
        QUOTE_REQUEST = "quote_request", "Quote request"
        BOOKING = "booking", "Booking"
        MESSAGE = "message", "Message"

    class Status(models.TextChoices):
        CAPTURED = "captured", "Captured"
        DELIVERED = "delivered", "Delivered"
        DELIVERY_FAILED = "delivery_failed", "Delivery failed"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="leads"
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="leads"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=lead_id
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone_e164 = models.CharField(max_length=16, blank=True)
    message = models.TextField(blank=True)
    spam_score = models.DecimalField(
        max_digits=4, decimal_places=3, null=True, blank=True
    )
    marked_spam = models.BooleanField(default=False)
    spam_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="leads_marked_spam",
    )
    source_page = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CAPTURED
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "leads"

    def __str__(self) -> str:
        return self.public_id


class ConsentText(models.Model):
    """An immutable copy of the exact consent wording shown at a point in time
    (spec §9.0). ``Consent.text_version`` points here."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="consent_texts"
    )
    key = models.SlugField(max_length=50)  # e.g. "consent", "lead-consent"
    version = models.CharField(max_length=20)  # e.g. "v3"
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "consent_texts"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key", "version"],
                name="uniq_consenttext_tenant_key_version",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key}-{self.version}"

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ValueError("ConsentText rows are immutable once written.")
        super().save(*args, **kwargs)


class Consent(models.Model):
    """A single consent grant or refusal (decisions.md §2, spec §9.0). Required
    on claim submission and lead capture; exactly one subject."""

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="consents"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=cns_id
    )
    claim = models.ForeignKey(
        Claim,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="consents",
    )
    lead = models.ForeignKey(
        Lead,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="consents",
    )
    channel = models.CharField(max_length=40)
    granted = models.BooleanField()
    granted_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    text_version = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "consents"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(claim__isnull=False, lead__isnull=True)
                    | models.Q(claim__isnull=True, lead__isnull=False)
                ),
                name="consent_has_exactly_one_subject",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.channel}={self.granted}"


class SuppressionKey(models.Model):
    """A removed listing's fingerprint (spec §4.1.1). Outlives the deleted
    listing row; subsequent imports check against it so a removed business does
    not reappear."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="suppression_keys",
    )
    key_hash = models.CharField(max_length=64)  # normalised name + address + phone
    source_listing_public_id = models.CharField(max_length=40)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "suppression_keys"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key_hash"],
                name="uniq_suppressionkey_tenant_hash",
            ),
        ]

    def __str__(self) -> str:
        return self.key_hash


class SearchReindexJob(models.Model):
    """A marker that some listings' search vectors are stale -- a listing
    type's field schema changed (searchable flags), or a category was renamed
    (its name feeds weight B). Drained by the worker's ``search_reindex`` tick
    job (``directory.search.drain_reindex_markers``, ruling 7); the
    ``rebuild_search_index`` command runs the same drain by hand.

    ``cursor`` is the highest listing id already recomputed for this marker. A
    large marker is drained in bounded chunks across many ticks, and the cursor
    is what lets a crash or a restart resume instead of starting over.
    """

    class Scope(models.TextChoices):
        TENANT = "tenant", "Whole tenant"
        LISTING_TYPE = "listing_type", "Listing type"
        CATEGORY = "category", "Category"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="reindex_jobs"
    )
    scope = models.CharField(max_length=16, choices=Scope.choices)
    scope_ref = models.CharField(max_length=40, blank=True)  # public_id; "" for tenant
    reason = models.CharField(max_length=120, blank=True)
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    cursor = models.PositiveBigIntegerField(default=0)
    done_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "search_reindex_jobs"
        indexes = [models.Index(fields=["done_at", "id"])]

    def __str__(self) -> str:
        return f"{self.scope}:{self.scope_ref or '*'}"
