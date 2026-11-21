"""Copy a tenant's stored files to a different storage backend.

A tenant's files are the keys named by its ``MediaAsset.storage_key`` and
``ImportBatch.stored_path`` rows, relative to the per-tenant root. ``run_copy``
copies each key from the tenant's current backend to a target, verifies it by
reading the object back from the target and comparing size and SHA-256, and
records every key in a :class:`StorageCopy` so an interrupted run resumes
without re-copying finished keys.

Rules this module holds:

* The tenant's backend setting switches only after every key is copied and
  verified, and a final scan finds no key that was added in the meantime. Until
  then the tenant keeps using its current backend.
* A key that is not verified is a failure, never a copy.
* An existing key on the target is never overwritten. Identical content is
  reported as already present; different content is a failure.
* Source files are never deleted.
* Target credentials are used in memory for the run and handed to the settings
  service on switch. They are not written to the copy records or to any log.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field

from django.core.files import File
from django.core.files.storage import Storage
from django.utils import timezone

from directory import storage as _storage
from directory.models import ImportBatch, MediaAsset, StorageCopy, StorageCopyFile

CHUNK = 1024 * 1024
MAX_ROUNDS = 5  # passes that pick up keys added while a copy is running


class CopyRefused(Exception):
    """The copy cannot start: same backend, or another copy is unfinished."""


@dataclass
class PlannedFile:
    kind: str
    ref: str
    key: str
    size: int


@dataclass
class CopyPlan:
    files: "list[PlannedFile]" = field(default_factory=list)
    missing: "list[str]" = field(default_factory=list)  # keys the source cannot find

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class Failure:
    key: str
    error: str


@dataclass
class CopyResult:
    copy: StorageCopy
    copied: int = 0
    already_present: "list[str]" = field(default_factory=list)
    failures: "list[Failure]" = field(default_factory=list)
    switched: bool = False


def _files(copy: StorageCopy):
    return StorageCopyFile.all_tenants.filter(tenant=copy.tenant, copy=copy)


# --- what a tenant stores ---------------------------------------------------


def tenant_file_refs(tenant) -> "list[tuple[str, str, str]]":
    """``(kind, ref, key)`` for every stored file the tenant's rows point at,
    one entry per distinct key."""
    seen: "set[str]" = set()
    out = []
    media = MediaAsset.all_tenants.filter(tenant=tenant).exclude(storage_key="")
    for ref, key in media.order_by("id").values_list("public_id", "storage_key"):
        if key not in seen:
            seen.add(key)
            out.append((StorageCopyFile.Kind.MEDIA, ref, key))
    batches = ImportBatch.all_tenants.filter(tenant=tenant).exclude(stored_path="")
    for ref, key in batches.order_by("id").values_list("public_id", "stored_path"):
        if key not in seen:
            seen.add(key)
            out.append((StorageCopyFile.Kind.IMPORT, ref, key))
    return out


def normalize_target(cfg: dict) -> dict:
    """The comparable, non-secret form of a storage config: blanks and the
    legacy cleartext access key dropped, ``backend`` always present."""
    out = {k: v for k, v in (cfg or {}).items() if v not in ("", None) and k not in ("access_key", "secrets")}
    out.setdefault("backend", "local")
    return out


def covered_by_verified_copy(tenant, target_cfg: dict) -> bool:
    """Whether a completed copy to ``target_cfg`` holds a verified copy of every
    file the tenant's rows point at now."""
    want = normalize_target(target_cfg)
    for copy in StorageCopy.all_tenants.filter(tenant=tenant, status=StorageCopy.Status.COMPLETED):
        if normalize_target(copy.target_config) != want:
            continue
        done = set(
            _files(copy).filter(status=StorageCopyFile.Status.COPIED).values_list("key", flat=True)
        )
        if all(key in done for _, _, key in tenant_file_refs(tenant)):
            return True
    return False


# --- dry run ----------------------------------------------------------------


def plan_copy(tenant) -> CopyPlan:
    """What a copy would move, from the current backend. Reads sizes only; writes
    nothing anywhere."""
    source = _storage.get_tenant_storage(tenant)
    plan = CopyPlan()
    for kind, ref, key in tenant_file_refs(tenant):
        try:
            if not source.exists(key):
                plan.missing.append(key)
                continue
            plan.files.append(PlannedFile(kind, ref, key, source.size(key)))
        except Exception as exc:  # a provider error: report the key, keep planning
            plan.missing.append(f"{key} ({type(exc).__name__})")
    return plan


# --- the copy ---------------------------------------------------------------


def _sync_rows(copy: StorageCopy) -> int:
    """Add a row for every key the tenant now references that the copy has not
    seen. Returns how many were added."""
    have = set(_files(copy).values_list("key", flat=True))
    new = [
        StorageCopyFile(tenant=copy.tenant, copy=copy, kind=kind, ref=ref, key=key)
        for kind, ref, key in tenant_file_refs(copy.tenant)
        if key not in have
    ]
    StorageCopyFile.all_tenants.bulk_create(new)
    return len(new)


def _hash_into(handle, sink=None) -> "tuple[int, str]":
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: handle.read(CHUNK), b""):
        digest.update(chunk)
        size += len(chunk)
        if sink is not None:
            sink.write(chunk)
    return size, digest.hexdigest()


def _copy_one(source: Storage, target: Storage, row: StorageCopyFile) -> None:
    """Copy and verify one key, recording the outcome on ``row``. Any ordinary
    error marks the row failed; it never escapes. (An interrupt does, leaving the
    row pending for the next run.)"""
    try:
        if not source.exists(row.key):
            raise FileNotFoundError("the source file does not exist")
        with tempfile.SpooledTemporaryFile(max_size=8 * CHUNK) as spool:
            with source.open(row.key, "rb") as fh:
                size, checksum = _hash_into(fh, spool)
            row.size, row.checksum_sha256 = size, checksum

            if target.exists(row.key):
                # Never overwrite. Identical content is fine; anything else is not.
                with target.open(row.key, "rb") as fh:
                    t_size, t_sum = _hash_into(fh)
                if (t_size, t_sum) != (size, checksum):
                    raise ValueError(
                        "a different file already exists at this key on the target; "
                        "it was not overwritten"
                    )
                row.already_present = True
            else:
                spool.seek(0)
                saved = target.save(row.key, File(spool))
                if saved != row.key:
                    target.delete(saved)  # a stray we just wrote under another name
                    raise ValueError(f"the target stored the file under a different key ({saved})")
                with target.open(row.key, "rb") as fh:
                    t_size, t_sum = _hash_into(fh)
                if (t_size, t_sum) != (size, checksum):
                    raise ValueError(
                        f"verification failed: source {size} bytes / {checksum[:12]}, "
                        f"target {t_size} bytes / {t_sum[:12]}"
                    )
        row.status, row.error = StorageCopyFile.Status.COPIED, ""
    except Exception as exc:
        row.status = StorageCopyFile.Status.FAILED
        row.error = f"{type(exc).__name__}: {exc}"[:500]
    row.save()


def _start_copy(tenant, target_cfg: dict) -> StorageCopy:
    current = normalize_target(_storage._stored_cfg(tenant))
    if normalize_target(target_cfg) == current:
        raise CopyRefused("the tenant already uses that storage backend")
    open_copy = StorageCopy.all_tenants.filter(
        tenant=tenant, status=StorageCopy.Status.IN_PROGRESS
    ).first()
    if open_copy is not None:
        if normalize_target(open_copy.target_config) != normalize_target(target_cfg):
            raise CopyRefused(
                f"an unfinished copy to a different target exists ({open_copy.public_id}); "
                "finish it with its own target settings"
            )
        return open_copy
    return StorageCopy.all_tenants.create(
        tenant=tenant,
        source_backend=_storage.tenant_storage_backend(tenant),
        target_backend=normalize_target(target_cfg)["backend"],
        target_config=normalize_target(target_cfg),
    )


def run_copy(tenant, target_cfg: dict, target_secrets: dict, *, operator) -> CopyResult:
    """Copy every file to the target and, only if all are verified, switch the
    tenant to it. Safe to re-run: finished keys are skipped.

    Call outside a transaction: the switch is an operator settings command.
    Raises :class:`CopyRefused` or :class:`~directory.storage.StorageConfigError`
    before anything is written; per-file problems are reported in the result.
    """
    from tenants import services

    cfg = normalize_target(target_cfg)
    source = _storage.get_tenant_storage(tenant)
    target = _storage.build_storage(tenant.public_id, cfg, dict(target_secrets))
    _storage.check_storage(target)

    copy = _start_copy(tenant, cfg)
    _files(copy).filter(status=StorageCopyFile.Status.FAILED).update(
        status=StorageCopyFile.Status.PENDING
    )

    settled = False
    for _ in range(MAX_ROUNDS):
        _sync_rows(copy)
        pending = list(_files(copy).filter(status=StorageCopyFile.Status.PENDING))
        if not pending:
            settled = True
            break
        for row in pending:
            _copy_one(source, target, row)

    rows = list(_files(copy).all())
    result = CopyResult(copy=copy)
    result.copied = sum(1 for r in rows if r.status == StorageCopyFile.Status.COPIED and not r.already_present)
    result.already_present = [r.key for r in rows if r.already_present]
    result.failures = [Failure(r.key, r.error) for r in rows if r.status == StorageCopyFile.Status.FAILED]

    verified = settled and all(r.status == StorageCopyFile.Status.COPIED for r in rows)
    if verified:
        services.save_settings_page(
            tenant, page="storage", block="storage", value=cfg,
            changed_by=operator, secrets=dict(target_secrets),
        )
        copy.status = StorageCopy.Status.COMPLETED
        copy.completed_at = timezone.now()
        copy.save(update_fields=["status", "completed_at"])
        result.switched = True
    return result
