"""Principals and tenancy structure.

``Tenant``, ``Operator`` and ``StaffMembership`` sit outside tenant scoping:
``Tenant`` is the tenant, an ``Operator`` spans the whole installation, and a
``StaffMembership`` *is* the operator-tenant relationship (CLAUDE.md
invariant 3).
"""

from __future__ import annotations

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone

from osds.ids import op_id, tnt_id


class OperatorManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email: str, password: "str | None" = None, **extra):
        if not email:
            raise ValueError("Operators must have an email address.")
        email = self.normalize_email(email).lower()
        operator = self.model(email=email, **extra)
        operator.set_password(password)
        operator.save(using=self._db)
        return operator

    def create_superuser(self, email: str, password: "str | None" = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_superadmin", True)
        extra.setdefault("is_active", True)
        if not extra["is_superuser"]:
            raise ValueError("A superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra)


class Operator(AbstractBaseUser, PermissionsMixin):
    """A person who administers OSDS. Belongs to no tenant; one row is one
    login across the whole installation (spec §4.4). This is ``AUTH_USER_MODEL``.

    Two axes, not one ladder (decisions.md §3): ``is_superadmin`` is the OSDS
    installation-scope flag -- create, suspend and delete tenants, elevate
    another operator -- while ``is_superuser``/``is_staff`` come from Django and
    gate the console's admin surface. Related, but not the same axis.
    """

    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=op_id
    )
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200, blank=True)
    is_superadmin = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = OperatorManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "operators"

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)


class Tenant(models.Model):
    """One directory site. Not tenant-scoped -- it *is* the tenant.

    Single-directory mode is a UI toggle (``mode``), never a different data
    model (CLAUDE.md invariant 3).
    """

    class Mode(models.TextChoices):
        SINGLE = "single", "Single-directory"
        MULTI = "multi", "Multi-directory"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    public_id = models.CharField(
        max_length=40, unique=True, editable=False, default=tnt_id
    )
    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    # Set by the first-run wizard; the Host-resolution middleware (next PR)
    # matches an incoming host against this.
    primary_domain = models.CharField(
        max_length=253, unique=True, null=True, blank=True
    )
    domain_verified_at = models.DateTimeField(null=True, blank=True)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.SINGLE)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE
    )
    # Tenant config that has no model of its own: claim_verification, reviews,
    # etc. Tiers are Tier rows, not part of this document.
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        Operator,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tenants_created",
    )

    class Meta:
        db_table = "tenants"

    def __str__(self) -> str:
        return self.slug

    def save(self, *args, **kwargs):
        if self.primary_domain:
            self.primary_domain = self.primary_domain.lower()
        super().save(*args, **kwargs)


class StaffMembership(models.Model):
    """An operator's relationship to one tenant (spec §4.4).

    Carries ``tenant`` because it *is* that relationship, not tenant-owned data.
    Uses a plain manager on purpose (allowlisted in
    ``tenants/tests/test_scoped_manager.py``): the console lists an operator's
    memberships across every tenant, with none in scope.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"

    class Role(models.IntegerChoices):
        SUPPORT = 0, "Support"
        MODERATOR = 1, "Moderator"
        EDITOR = 2, "Editor"
        MANAGER = 3, "Manager"
        ADMIN = 4, "Admin"

    operator = models.ForeignKey(
        Operator, on_delete=models.CASCADE, related_name="memberships"
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.PositiveSmallIntegerField(choices=Role.choices)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.PENDING
    )
    invited_by = models.ForeignKey(
        Operator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="memberships_granted",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    accepted_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        db_table = "staff_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["operator", "tenant"],
                name="uniq_membership_operator_tenant",
            ),
            models.CheckConstraint(
                condition=models.Q(role__gte=0) & models.Q(role__lte=4),
                name="staff_role_within_rank_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.operator_id}@{self.tenant_id}:{self.get_role_display()}"


class OperatorInvite(models.Model):
    """A set-password invitation for an operator with no credential yet
    (spec §4.4). Only the token hash is stored."""

    operator = models.ForeignKey(
        Operator, on_delete=models.CASCADE, related_name="invites"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        Operator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invites_sent",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "operator_invites"

    def __str__(self) -> str:
        return f"invite:{self.operator_id}"


class InstallSetup(models.Model):
    """Installation-level first-run state. A single row (pk=1).

    Holds the hash of the setup token printed to the container logs, and the
    timestamp at which the first-run wizard finished. ``completed_at IS NULL``
    is the authoritative "setup is still running" gate -- host resolution and
    the wizard both read it, so the gate flips for every process the moment the
    row is written, with no restart.
    """

    token_hash = models.CharField(max_length=64)  # sha256 hex of the setup token
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "install_setup"

    def __str__(self) -> str:
        state = "complete" if self.completed_at else "in progress"
        return f"install setup ({state})"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce the singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "InstallSetup | None":
        return cls.objects.filter(pk=1).first()


class Secret(models.Model):
    """An encrypted configuration secret (spec §8.1).

    Resolution order is tenant override, then deployment-level, then
    ``ConfigurationError`` -- see ``tenants.secrets.get_secret``. Ciphertext is
    Fernet, keyed off ``OSDS_SECRET_KEY`` (separate from Django's
    ``SECRET_KEY``). Plain manager: resolution deliberately spans a tenant row
    and a deployment row (``tenant IS NULL``), so it is allowlisted in
    ``tenants/tests/test_scoped_manager.py``.
    """

    class Scope(models.TextChoices):
        DEPLOYMENT = "deployment", "Deployment"
        TENANT = "tenant", "Tenant"

    scope = models.CharField(max_length=12, choices=Scope.choices)
    tenant = models.ForeignKey(
        Tenant,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="secrets",
    )
    key = models.CharField(max_length=100)
    ciphertext = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        db_table = "secrets"
        constraints = [
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(tenant__isnull=True),
                name="uniq_secret_deployment_key",
            ),
            models.UniqueConstraint(
                fields=["tenant", "key"],
                condition=models.Q(tenant__isnull=False),
                name="uniq_secret_tenant_key",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(scope="deployment", tenant__isnull=True)
                    | models.Q(scope="tenant", tenant__isnull=False)
                ),
                name="secret_scope_matches_tenant",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key}"
