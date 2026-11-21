"""Per-tenant media storage, resolved at runtime.

``django-storages`` reads ``settings.STORAGES`` at import time, but the backend
choice and its credentials are wizard-entered and live in the database
(decisions.md §4). ``get_tenant_storage`` builds a Django ``Storage`` for one
tenant from ``tenant.settings["storage"]`` plus the tenant's encrypted secrets.

Backends (``local``, ``s3``, ``azure``, ``gcp``) are a registry of factories:
``factory(cfg, secrets) -> Storage``. ``cfg`` is the stored config block plus a
``prefix`` (``"<tenant public id>/"``) that every backend applies to its keys,
so one tenant's keys can never resolve into another's tree. ``secrets`` maps a
secret name (``access_key``, ``secret_key``, ``account_key``,
``service_account_json``) to its value. Credentials are always stored keys --
never an ambient cloud identity -- so the app runs the same on any host.

Everything that can go wrong in configuration surfaces as
:class:`StorageConfigError`; callers catch that one type.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
import uuid
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage

CLOUD_BACKENDS = ("s3", "azure", "gcp")

# backend -> secret names it needs. Stored as tenant secrets "storage_<name>".
SECRET_NAMES = {
    "s3": ("access_key", "secret_key"),
    "azure": ("account_key",),
    "gcp": ("service_account_json",),
    "local": (),
}

_INSTALL_HINT = {
    "s3": "boto3",
    "azure": "azure-storage-blob",
    "gcp": "google-cloud-storage",
}


class StorageConfigError(RuntimeError):
    """The tenant's storage cannot be built or reached: an unknown backend,
    missing config, a missing SDK or a credential that will not load."""


def tenant_storage_backend(tenant) -> str:
    """The configured backend name for ``tenant``; ``"local"`` when unset."""
    return (_stored_cfg(tenant).get("backend")) or "local"


def _stored_cfg(tenant) -> dict:
    return dict((getattr(tenant, "settings", None) or {}).get("storage") or {})


def tenant_prefix(tenant_public_id: str) -> str:
    return f"{tenant_public_id}/"


# --- loading ---------------------------------------------------------------


def _import_class(path: str, backend: str):
    module, _, name = path.rpartition(".")
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, ImproperlyConfigured) as exc:
        pkg = _INSTALL_HINT.get(backend, "the storage SDK")
        raise StorageConfigError(
            f"the {backend!r} storage backend needs the {pkg} package, which is "
            f"not installed ({exc}). Install it, or choose local storage."
        ) from exc


def _require(cfg: dict, backend: str, *names: str) -> "list[str]":
    values = [(cfg.get(n) or "").strip() if isinstance(cfg.get(n), str) else cfg.get(n) for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise StorageConfigError(
            f"the {backend!r} storage backend is missing: {', '.join(missing)}"
        )
    return values


def _require_secrets(secrets: dict, backend: str) -> "list[str]":
    names = SECRET_NAMES[backend]
    missing = [n for n in names if not secrets.get(n)]
    if missing:
        raise StorageConfigError(
            f"the {backend!r} storage backend has no stored credential: "
            f"{', '.join(missing)}"
        )
    return [secrets[n] for n in names]


def _not_found(exc: BaseException) -> bool:
    """A provider "no such object" error, recognised without importing its SDK."""
    if type(exc).__name__ in {"ResourceNotFoundError", "NotFound", "NoSuchKey", "BlobNotFound"}:
        return True
    status = getattr(getattr(exc, "response", None), "get", lambda *_: None)("ResponseMetadata")
    return isinstance(status, dict) and status.get("HTTPStatusCode") == 404


_NORMALIZED: "dict[type, type]" = {}


def _normalized(base: type) -> type:
    """``base`` with a missing object on ``open`` always a ``FileNotFoundError``
    (``public_views`` turns that into a 404), whatever the provider raises."""
    cls = _NORMALIZED.get(base)
    if cls is None:

        class Normalized(base):  # type: ignore[valid-type, misc]
            def _open(self, name, mode="rb"):
                try:
                    handle = super()._open(name, mode)
                except FileNotFoundError:
                    raise
                except Exception as exc:
                    if _not_found(exc):
                        raise FileNotFoundError(f"File does not exist: {name}") from exc
                    raise
                if "r" in mode and getattr(self, "_probe_before_read", False):
                    # Lazy handles (Azure) only fail on first read; check now.
                    if not self.exists(name):
                        raise FileNotFoundError(f"File does not exist: {name}")
                return handle

        Normalized.__name__ = base.__name__
        Normalized.__qualname__ = base.__qualname__
        _NORMALIZED[base] = cls = Normalized
    return cls


# --- factories -------------------------------------------------------------


def _local(cfg: dict, secrets: dict) -> Storage:
    prefix = cfg["prefix"].strip("/")
    return FileSystemStorage(location=str(Path(settings.OSDS_MEDIA_ROOT) / prefix))


def _s3(cfg: dict, secrets: dict) -> Storage:
    (bucket,) = _require(cfg, "s3", "bucket")
    access_key, secret_key = _require_secrets(secrets, "s3")
    cls = _normalized(_import_class("storages.backends.s3.S3Storage", "s3"))
    return cls(
        bucket_name=bucket,
        region_name=(cfg.get("region") or "").strip() or None,
        endpoint_url=(cfg.get("endpoint") or "").strip() or None,
        access_key=access_key,
        secret_key=secret_key,
        location=cfg["prefix"],
        file_overwrite=False,
        default_acl=None,
        querystring_auth=True,
    )


_ONE_LEVEL: "dict[type, type]" = {}


def _one_level(base: type) -> type:
    """``base`` with ``listdir`` returning one directory level under the
    tenant prefix, as names relative to the listed directory.

    django-storages' Azure ``listdir`` returns every blob below the path as a
    full container-relative name and, for an empty path, lists the whole
    container -- other tenants' blobs included."""
    cls = _ONE_LEVEL.get(base)
    if cls is None:

        class OneLevel(base):  # type: ignore[valid-type, misc]
            def listdir(self, path=""):
                root = self._get_valid_path(path) if path.strip("./") else self.location.strip("/")
                prefix = f"{root}/" if root else ""
                dirs: "list[str]" = []
                files: "list[str]" = []
                for blob in self.client.list_blobs(name_starts_with=prefix, timeout=self.timeout):
                    rel = blob.name[len(prefix):]
                    head, sep, _ = rel.partition("/")
                    if not head:
                        continue
                    if sep:
                        if head not in dirs:
                            dirs.append(head)
                    else:
                        files.append(head)
                return dirs, files

        OneLevel.__name__ = base.__name__
        OneLevel.__qualname__ = base.__qualname__
        _ONE_LEVEL[base] = cls = OneLevel
    return cls


def _azure(cfg: dict, secrets: dict) -> Storage:
    container, account = _require(cfg, "azure", "bucket", "account_name")
    (account_key,) = _require_secrets(secrets, "azure")
    cls = _normalized(
        _one_level(_import_class("storages.backends.azure_storage.AzureStorage", "azure"))
    )
    cls._probe_before_read = True
    return cls(
        account_name=account,
        account_key=account_key,
        azure_container=container,
        location=cfg["prefix"],
        overwrite_files=False,
    )


def _gcp(cfg: dict, secrets: dict) -> Storage:
    (bucket,) = _require(cfg, "gcp", "bucket")
    (raw,) = _require_secrets(secrets, "gcp")
    info = parse_service_account_json(raw)
    cls = _normalized(_import_class("storages.backends.gcloud.GoogleCloudStorage", "gcp"))
    oauth = _import_class("google.oauth2.service_account.Credentials", "gcp")
    try:
        credentials = oauth.from_service_account_info(info)
    except Exception as exc:  # google-auth raises ValueError on a bad key
        raise StorageConfigError(
            f"the service account credentials could not be loaded: {exc}"
        ) from exc
    return cls(
        bucket_name=bucket,
        credentials=credentials,
        project_id=info.get("project_id"),
        location=cfg["prefix"],
        file_overwrite=False,
        default_acl=None,
        querystring_auth=True,
    )


def parse_service_account_json(raw: str) -> dict:
    try:
        info = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StorageConfigError("the service account JSON is not valid JSON") from exc
    if not isinstance(info, dict) or not info.get("private_key") or not info.get("client_email"):
        raise StorageConfigError(
            "the service account JSON must be an object with private_key and client_email"
        )
    return info


Factory = Callable[[dict, dict], Storage]
_REGISTRY: "dict[str, Factory]" = {
    "local": _local,
    "s3": _s3,
    "azure": _azure,
    "gcp": _gcp,
}


def register_backend(name: str, factory: Factory) -> None:
    _REGISTRY[name] = factory


# --- resolver --------------------------------------------------------------


def build_storage(tenant_public_id: str, cfg: dict, secrets: dict) -> Storage:
    """A fresh ``Storage`` for ``cfg`` + ``secrets``; nothing cached."""
    backend = (cfg or {}).get("backend") or "local"
    factory = _REGISTRY.get(backend)
    if factory is None:
        raise StorageConfigError(f"unknown storage backend: {backend!r}")
    return factory({**cfg, "prefix": tenant_prefix(tenant_public_id)}, dict(secrets))


def load_secrets(tenant, backend: str, cfg: "dict | None" = None) -> dict:
    """The tenant's stored credentials for ``backend``, by secret name. Absent
    ones are left out. ``storage.access_key`` in the settings block is the legacy
    cleartext copy of the S3 access key, read only when no secret exists."""
    from tenants.secrets import ConfigurationError, get_secret, has_secret

    cfg = _stored_cfg(tenant) if cfg is None else cfg
    out: "dict[str, str]" = {}
    for name in SECRET_NAMES.get(backend, ()):
        key = f"storage_{name}"
        try:
            if has_secret(key, tenant=tenant):
                out[name] = get_secret(key, tenant=tenant)
        except ConfigurationError as exc:
            raise StorageConfigError(
                f"the stored {name.replace('_', ' ')} could not be loaded: {exc}"
            ) from exc
    if backend == "s3" and "access_key" not in out and cfg.get("access_key"):
        out["access_key"] = cfg["access_key"]
    return out


_cache: "dict[str, tuple[str, Storage]]" = {}
_cache_lock = threading.Lock()


def _digest(cfg: dict, secrets: dict) -> str:
    blob = json.dumps([cfg, secrets], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get_tenant_storage(tenant) -> Storage:
    """A ``Storage`` rooted at this tenant's media area.

    Instances are cached per tenant and keyed by a hash of the config and
    credentials, so an edit takes effect on the next call. Raises
    :class:`StorageConfigError` for anything misconfigured.
    """
    cfg = _stored_cfg(tenant)
    backend = cfg.get("backend") or "local"
    secrets = load_secrets(tenant, backend, cfg)
    digest = _digest(cfg, secrets)
    with _cache_lock:
        hit = _cache.get(tenant.public_id)
        if hit and hit[0] == digest:
            return hit[1]
    storage = build_storage(tenant.public_id, cfg, secrets)
    with _cache_lock:
        _cache[tenant.public_id] = (digest, storage)
    return storage


def clear_storage_cache() -> None:
    with _cache_lock:
        _cache.clear()


def check_storage(storage: Storage) -> None:
    """Put, read, list and delete a probe object. Raises
    :class:`StorageConfigError` naming the step and the provider's error; never
    lets an SDK exception escape. The probe object is removed even on failure."""
    key = f".osds-probe/{uuid.uuid4().hex}"
    body = b"osds storage probe"
    step = "write"
    saved = None
    try:
        saved = storage.save(key, ContentFile(body))
        step = "read"
        with storage.open(saved, "rb") as fh:
            if fh.read() != body:
                raise StorageConfigError("storage check failed: the object read back differs")
        step = "list"
        _, files = storage.listdir(".osds-probe")
        if saved.rsplit("/", 1)[-1] not in files:
            raise StorageConfigError("storage check failed: the probe object is not listed")
        step = "delete"
        storage.delete(saved)
        saved = None
    except StorageConfigError:
        raise
    except Exception as exc:
        raise StorageConfigError(f"storage check failed at {step}: {exc}") from exc
    finally:
        if saved is not None:
            try:
                storage.delete(saved)
            except Exception:  # best effort; the primary error is what matters
                pass


def storage_problem(tenant) -> str:
    """Why the tenant's configured storage cannot be built, or ``""``. Builds the
    backend (no network call); it does not run the probe."""
    try:
        get_tenant_storage(tenant)
    except StorageConfigError as exc:
        return str(exc)
    return ""
