"""The CSV import row loop (spec §3.3 ``import.*``, §4.1.1, §7.1).

``import_once`` is one bounded pass: claim a ``pending`` or ``processing``
``ImportBatch``, push up to ``limit`` of its data rows through
``upsert_listing``, advance the cursor, and -- when the file is exhausted or a
row fails fatally -- finish the batch and emit ``import.completed``. The
worker's ``worker_pass`` calls it once per pass, right after ``drain_once``.

There is no ``while`` loop and no rollback here. Rollback and the per-row
provenance table are the next PR.

The whole pass runs inside ``tenant_context(batch.tenant)``, entered *after*
the cross-tenant claim and left before the function returns -- the worker is
one long-lived process with no request teardown, so a tenant left in scope
would leak into the next pass and the drain (#119).
"""

from __future__ import annotations

import csv
import io
import itertools
from dataclasses import dataclass

from django.db import transaction

from audit import events
from audit.outbox import emit
from directory.csv_import import ROWS_PER_PASS, build_payload
from directory.field_schema import SchemaError
from directory.models import ImportBatch
from directory.services import RejectedField, Suppressed, upsert_listing
from directory.storage import DeferredFeatureError, get_tenant_storage
from osds.tenancy import tenant_context

_OUTCOME_COUNTER = {
    "created": "created_count",
    "updated": "updated_count",
    "unchanged": "skipped_count",
    "replayed": "updated_count",  # a re-run row exists; count it as an update
}


@dataclass
class ImportStats:
    batch_id: "str | None" = None
    rows_processed: int = 0
    completed: bool = False


class ImportFileError(Exception):
    """The stored file cannot be read or parsed as CSV -- the batch fails
    whole, with no row number."""


def import_once(*, now, limit: int = ROWS_PER_PASS) -> ImportStats:
    batch, first_pass = _claim(now)
    if batch is None:
        return ImportStats()

    stats = ImportStats(batch_id=batch.public_id)
    with tenant_context(batch.tenant):
        try:
            rows = _read_rows(batch)
        except ImportFileError as exc:
            # Every batch that reaches `processing` emits import.started, even
            # one whose file will not parse -- then import.completed / failed.
            if first_pass:
                _begin(batch, now, [])
            _finish(batch, now, status="failed", extra_error={"message": str(exc)})
            stats.completed = True
            return stats

        if first_pass:
            _begin(batch, now, rows)

        stats.rows_processed, stats.completed = _process_chunk(
            batch, now, rows, limit
        )
    return stats


# --- claim --------------------------------------------------------------------


def _claim(now):
    """Take the oldest claimable batch in a short transaction and, if it is
    fresh, move it to ``processing``. Cross-tenant: no tenant in scope."""
    with transaction.atomic():
        batch = (
            ImportBatch.all_tenants.select_for_update(skip_locked=True)
            .filter(
                status__in=[
                    ImportBatch.Status.PENDING,
                    ImportBatch.Status.PROCESSING,
                ],
                listing_type__isnull=False,
            )
            .order_by("id")
            .first()
        )
        if batch is None:
            return None, False
        first_pass = batch.status == ImportBatch.Status.PENDING
        if first_pass:
            batch.status = ImportBatch.Status.PROCESSING
            batch.started_at = now
            batch.save(update_fields=["status", "started_at"])
    return batch, first_pass


# --- file -------------------------------------------------------------------


def _read_rows(batch) -> "list[list[str]]":
    """Every data row (header dropped), as lists of cell strings. Re-read in
    full each pass -- CSV fields can hold newlines, so a byte offset is not a
    safe cursor; the row count is small at this scale."""
    try:
        storage = get_tenant_storage(batch.tenant)
        with storage.open(batch.stored_path, "rb") as fh:
            raw = fh.read()
    except (OSError, DeferredFeatureError) as exc:
        raise ImportFileError(f"cannot read the uploaded file: {exc}") from exc

    text = raw.decode(batch.encoding, errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=batch.delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ImportFileError(f"cannot parse the CSV: {exc}") from exc
    if batch.has_header and rows:
        rows = rows[1:]
    return rows


# --- lifecycle events -----------------------------------------------------


def _actor(batch) -> dict:
    if batch.started_by_id:
        return {"type": "staff", "id": batch.started_by.public_id}
    return {"type": "system"}


def _begin(batch, now, rows) -> None:
    """First pass only: set ``row_count`` from the file, then emit
    ``import.started`` carrying it."""
    ImportBatch.all_tenants.filter(pk=batch.pk).update(row_count=len(rows))
    batch.row_count = len(rows)
    emit(
        events.IMPORT_STARTED,
        subject=batch.public_id,
        tenant=batch.tenant,
        actor=_actor(batch),
        data={
            "batch_id": batch.public_id,
            "source": batch.source,
            "row_count": batch.row_count,
            "started_by": (
                batch.started_by.public_id if batch.started_by_id else None
            ),
        },
    )


def _finish(batch, now, *, status: str, extra_error=None) -> None:
    """Terminal transition + ``import.completed``. ``status`` is ``"completed"``
    or ``"failed"``; a failed batch still emits ``import.completed`` (there is
    no ``import.failed``), with partial tallies and the failure in ``errors``."""
    if extra_error is not None:
        batch.errors = [*batch.errors, extra_error]
    batch.status = (
        ImportBatch.Status.COMPLETED
        if status == "completed"
        else ImportBatch.Status.FAILED
    )
    batch.completed_at = now
    with transaction.atomic():
        batch.save(update_fields=["status", "completed_at", "errors"])

    emit(
        events.IMPORT_COMPLETED,
        subject=batch.public_id,
        tenant=batch.tenant,
        actor=_actor(batch),
        data={
            "batch_id": batch.public_id,
            "status": status,
            "created": batch.created_count,
            "updated": batch.updated_count,
            "skipped": batch.skipped_count,
            "suppressed": batch.suppressed_count,
            "errors": batch.errors,
        },
    )


# --- the row loop --------------------------------------------------------


def _process_chunk(batch, now, rows, limit) -> "tuple[int, bool]":
    start = batch.processed_row_count
    chunk = list(itertools.islice(iter(rows), start, start + limit))
    headers = batch.detected_headers or []
    mapping = batch.column_mapping or {}

    processed = 0
    for i, cells in enumerate(chunk):
        n = start + i + 1
        try:
            _process_row(batch, cells, headers, mapping, n)
        except (SchemaError, RejectedField) as exc:
            _bump(batch, n, error_count=1, error=_row_error(n, exc))
        except Suppressed:
            _bump(batch, n, suppressed_count=1)
        except Exception as exc:  # unexpected -> the batch fails here
            _bump(
                batch,
                n,
                error_count=1,
                error={"row": n, "message": f"{type(exc).__name__}: {exc}"},
            )
            _finish(batch, now, status="failed")
            return processed + 1, True
        processed += 1

    done = batch.processed_row_count >= batch.row_count
    if done:
        _finish(batch, now, status="completed")
    return processed, done


def _process_row(batch, cells, headers, mapping, n) -> None:
    """One data row through ``upsert_listing``. Raises on any failure; the
    caller sorts expected (SchemaError / RejectedField / Suppressed) from
    fatal."""
    payload = build_payload(cells, headers, mapping)
    result = upsert_listing(
        batch.tenant,
        listing_type=batch.listing_type,
        payload=payload,
        actor=_actor(batch),
        source="csv_import",
        idempotency_key=f"csv:{batch.public_id}:row_{n}",
        import_batch=batch,
        suppression_check=True,
    )
    note = (
        {"row": n, "note": "reprocessed after worker restart"}
        if result.outcome == "replayed"
        else None
    )
    _bump(batch, n, note=note, **{_OUTCOME_COUNTER[result.outcome]: 1})


def _row_error(n, exc) -> dict:
    if isinstance(exc, RejectedField):
        return {"row": n, "field": exc.field}
    return {"row": n, "errors": exc.errors}  # SchemaError


def _bump(batch, n, *, note=None, error=None, **counters) -> None:
    """Advance the cursor to row ``n`` and fold this row's outcome into the
    batch, in one short transaction. Runs between ``upsert_listing`` calls,
    never inside one (``require_autocommit``)."""
    for name, delta in counters.items():
        setattr(batch, name, getattr(batch, name) + delta)
    batch.processed_row_count = n
    if error is not None:
        batch.errors = [*batch.errors, error]
    if note is not None:
        batch.notes = [*batch.notes, note]
    fields = [*counters, "processed_row_count", "errors", "notes"]
    with transaction.atomic():
        batch.save(update_fields=fields)
