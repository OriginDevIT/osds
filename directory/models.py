"""Listings and everything that hangs off them.

Every model here is tenant-scoped: a ``tenant`` FK plus the scoped default
manager (``objects``) and the ``all_tenants`` escape hatch.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from osds.db import TenantScopedManager
from osds.ids import (
    cat_id,
    claim_id,
    cns_id,
    imp_id,
    lead_id,
    listing_id,
    lt_id,
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


class ImportBatch(models.Model):
    """A CSV upload and its outcome (spec §4.1.1, import.* events). The pipeline
    is block 3; the model exists now so provenance FKs have a target."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        ROLLED_BACK = "rolled_back", "Rolled back"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="import_batches"
    )
    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=imp_id
    )
    source = models.CharField(max_length=20, default="csv")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    original_filename = models.CharField(max_length=255, blank=True)
    stored_path = models.CharField(max_length=500, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
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

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "listings"
        indexes = [
            models.Index(fields=["tenant", "visibility", "status"]),
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


class Claim(models.Model):
    """Acquiring a verified human owner for a listing (spec §9)."""

    class Status(models.TextChoices):
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ABANDONED = "abandoned", "Abandoned"
        DISPUTED = "disputed", "Disputed"

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
    attempts = models.PositiveIntegerField(default=0)

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
    rejection_reason = models.TextField(blank=True)

    last_step = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        db_table = "claims"

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
