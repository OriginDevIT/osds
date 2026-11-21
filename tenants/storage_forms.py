"""The storage settings form, shared by the first-run wizard and the tenant
admin Storage page.

Every backend's fieldset renders (no JavaScript is needed); ``clean`` validates
only the chosen backend's fields. Credentials are write-only: a secret field is
never re-rendered, and leaving it blank keeps the stored value. A cloud backend
is probed (write, read, list, delete) before the form validates, and a failed
probe refuses the save -- the operator retries or chooses local storage.
"""

from __future__ import annotations

from django import forms

from directory.storage import (
    SECRET_NAMES,
    StorageConfigError,
    build_storage,
    check_storage,
    load_secrets,
    parse_service_account_json,
    tenant_storage_backend,
)

BACKENDS = [
    ("local", "Local disk"),
    ("s3", "S3-compatible"),
    ("azure", "Azure Blob Storage"),
    ("gcp", "Google Cloud Storage"),
]

# backend -> (secret name -> form field name); the form field also carries the
# stored value's name so a blank submission keeps it.
_SECRET_FIELDS = {
    "s3": {"access_key": "s3_access_key", "secret_key": "s3_secret_key"},
    "azure": {"account_key": "azure_account_key"},
    "gcp": {"service_account_json": "gcp_service_account_json"},
}
_CONFIG_FIELDS = {
    "s3": {"bucket": "s3_bucket", "region": "s3_region", "endpoint": "s3_endpoint"},
    "azure": {"bucket": "azure_container", "account_name": "azure_account_name"},
    "gcp": {"bucket": "gcp_bucket"},
}
_REQUIRED = {
    "s3": {"s3_bucket": "Bucket", "s3_region": "Region"},
    "azure": {"azure_container": "Container", "azure_account_name": "Account name"},
    "gcp": {"gcp_bucket": "Bucket"},
}
_LEGENDS = {
    "s3": "S3-compatible storage",
    "azure": "Azure Blob Storage",
    "gcp": "Google Cloud Storage",
}


class _WriteOnlyTextarea(forms.Textarea):
    """A textarea that never renders a value, not even a just-submitted one."""

    def format_value(self, value):
        return None


def _secret(label, *, textarea=False):
    """A write-only credential field: never rendered back, blank keeps the stored value."""
    widget = (
        _WriteOnlyTextarea(attrs={"rows": 6, "autocomplete": "off"})
        if textarea else forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"})
    )
    return forms.CharField(label=label, required=False, widget=widget)


def storage_backend_locked(tenant) -> bool:
    """Whether media or import-batch rows exist, so the backend cannot change:
    their stored keys live in the current backend and there is no copy tool yet."""
    from directory.models import ImportBatch, MediaAsset

    return (
        MediaAsset.all_tenants.filter(tenant=tenant).exists()
        or ImportBatch.all_tenants.filter(tenant=tenant).exists()
    )


class StorageForm(forms.Form):
    BACKENDS = BACKENDS

    backend = forms.ChoiceField(label="Storage backend", choices=BACKENDS)

    s3_bucket = forms.CharField(label="Bucket", required=False)
    s3_region = forms.CharField(label="Region", required=False)
    s3_endpoint = forms.CharField(
        label="Endpoint URL", required=False,
        help_text="Leave blank for AWS S3; set it for an S3-compatible service.",
    )
    s3_access_key = _secret("Access key")
    s3_secret_key = _secret("Secret key")

    azure_container = forms.CharField(label="Container", required=False)
    azure_account_name = forms.CharField(label="Storage account name", required=False)
    azure_account_key = _secret("Account key")

    gcp_bucket = forms.CharField(label="Bucket", required=False)
    gcp_service_account_json = _secret("Service account JSON", textarea=True)

    def __init__(self, *args, tenant=None, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)
        stored = self._stored()
        if not self.is_bound:
            self.initial.setdefault("backend", stored.get("backend") or "local")
            for backend, mapping in _CONFIG_FIELDS.items():
                if (stored.get("backend") or "local") == backend:
                    for key, field in mapping.items():
                        self.initial.setdefault(field, stored.get(key, ""))
        self.current_backend = tenant_storage_backend(tenant) if tenant else "local"
        self.locked = bool(tenant and storage_backend_locked(tenant))

    def _stored(self) -> dict:
        return dict(((getattr(self.tenant, "settings", None) or {}).get("storage")) or {})

    def _has_stored_secret(self, backend: str, name: str) -> bool:
        if self.tenant is None:
            return False
        try:
            return name in load_secrets(self.tenant, backend, self._stored())
        except StorageConfigError:
            return True  # present but unreadable; the probe reports the cause

    def fieldsets(self):
        """``(backend, legend, [bound fields])`` for each cloud backend."""
        out = []
        for backend in ("s3", "azure", "gcp"):
            names = list(_CONFIG_FIELDS[backend].values()) + list(_SECRET_FIELDS[backend].values())
            out.append((backend, _LEGENDS[backend], [self[n] for n in names]))
        return out

    def stored_secret_names(self) -> "list[str]":
        """Form fields whose credential is already stored (never its value)."""
        return [
            field
            for backend, mapping in _SECRET_FIELDS.items()
            for name, field in mapping.items()
            if self._has_stored_secret(backend, name)
        ]

    def clean(self):
        cleaned = super().clean()
        backend = cleaned.get("backend")
        if not backend:
            return cleaned
        if self.tenant is not None and self.locked and backend != self.current_backend:
            self.add_error(
                "backend",
                "The storage backend cannot be changed while media or imports "
                "exist; their files live in the current backend.",
            )
            return cleaned
        if backend == "local":
            return cleaned
        for field, label in _REQUIRED[backend].items():
            if not (cleaned.get(field) or "").strip():
                self.add_error(field, f"{label} is required.")
        for name, field in _SECRET_FIELDS[backend].items():
            if not (cleaned.get(field) or "").strip() and not self._has_stored_secret(backend, name):
                self.add_error(field, "Required.")
        if backend == "gcp" and (cleaned.get("gcp_service_account_json") or "").strip():
            try:
                parse_service_account_json(cleaned["gcp_service_account_json"])
            except StorageConfigError as exc:
                self.add_error("gcp_service_account_json", str(exc))
        if self.errors:
            return cleaned
        try:
            self._probe(backend)
        except StorageConfigError as exc:
            self.add_error(None, f"{exc} Nothing was saved. Try again with "
                                 "different settings, or choose local disk storage.")
        return cleaned

    def _probe(self, backend: str) -> None:
        secrets = {}
        if self.tenant is not None:
            secrets = load_secrets(self.tenant, backend, self._stored())
        secrets.update(self.new_secrets(legacy=False))
        public_id = self.tenant.public_id if self.tenant is not None else "probe"
        check_storage(build_storage(public_id, self.config(), secrets))

    def config(self) -> dict:
        backend = self.cleaned_data["backend"]
        cfg = {"backend": backend}
        for key, field in _CONFIG_FIELDS.get(backend, {}).items():
            cfg[key] = (self.cleaned_data.get(field) or "").strip()
        return cfg

    def new_secrets(self, *, legacy: bool = True) -> "dict[str, str]":
        """Credentials typed into the form, by secret name. With ``legacy`` the
        old cleartext S3 access key is carried over so the settings block can
        drop it without losing the credential."""
        backend = self.cleaned_data["backend"]
        out = {}
        for name, field in _SECRET_FIELDS.get(backend, {}).items():
            value = (self.cleaned_data.get(field) or "").strip()
            if value:
                out[name] = value
        if legacy and backend == "s3" and "access_key" not in out:
            old = self._stored().get("access_key")
            if old and not (self.tenant and self._has_secret_row("access_key")):
                out["access_key"] = old
        return out

    def _has_secret_row(self, name: str) -> bool:
        from tenants.secrets import has_secret

        return has_secret(f"storage_{name}", tenant=self.tenant)
