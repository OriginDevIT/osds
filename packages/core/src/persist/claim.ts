/**
 * Persistence for `claim.submit` and `claim.approve` - spec §7 (commands),
 * §9 (claim verification), §11.1 (Postgres outbox).
 *
 * The DB counterparts of the pure {@link handleClaimSubmit} /
 * {@link handleClaimApprove} resolvers, same discipline as listing-upsert: one
 * transaction as `osds_app` with the tenant GUC set, load + apply + write the
 * outbox rows together or not at all; `deps` ({ now, newId }) injected so the
 * resolvers stay pure.
 *
 * Both commands emit more than one event on their happy paths (submit ->
 * `claim.submitted` [+ `claim.verification_started`]; approve ->
 * `claim.approved` + `listing.owner_assigned`). {@link writeOutboxEvents}
 * inserts them in emission order so per-subject ordering holds (§3.1) and
 * stamps `command.idempotency_key` on the FIRST row only - the unique index is
 * `(tenant_id, idempotency_key)` and a second copy would collide (follows
 * `writeOutbox` in command/handle.ts). A replay's key lookup returns that first
 * row's id and writes nothing; that is safe only because the original
 * transaction wrote every event of the command atomically.
 *
 * `claim.submit` resolves the claimant to a `users` row - upsert on
 * `(tenant_id, lower(email))`, minting `usr_<newId>` when absent, `role`
 * defaulted to `owner`. This is provisional pending issue #44 (proper account
 * linking / invite flow); it is the minimum `claim.approve` needs to have a
 * user to assign ownership to. The `disputed` path changes no state - it writes
 * only the `claim.disputed` event; the moderation flow (not built here) owns
 * the dispute record.
 *
 * `claim.approve` locks its claim and its listing row with `SELECT ... FOR
 * UPDATE` (cf. `lockListing` in command/handle.ts) so concurrent approvals of
 * different claims on one listing serialize. Under that lock it enforces §9.4 -
 * verification alone never moves ownership from a sitting owner: if an approved
 * claim already exists for the listing (or the listing is `claimed` with no
 * approved claim to explain it), the command is rejected and no
 * `listing.owner_assigned` is emitted. There is still no first-class owner
 * column on `listings` (schema, not this pass) - the approved `claims` row is
 * the record of ownership; `listing.status = 'claimed'` is its shadow.
 *
 * Claim events carry no JSON Patch, so there is no path->column mapping to fail
 * the way listing-upsert's does; the loud-failure discipline survives as the
 * empty-events guard in {@link writeOutboxEvents}.
 */
import { sql } from "@osds/db";
import type { OsdsCommand, ProblemDocument } from "@osds/adapter-kit";
import {
  handleClaimApprove,
  handleClaimSubmit,
  withClaimId,
  type ClaimantData,
  type ClaimListing,
  type ClaimMethod,
  type ClaimRecord,
  type ClaimStatus,
} from "../command/claim.js";
import { validationProblem } from "../command/problem.js";
import {
  findEventId,
  isUniqueViolation,
  withTenant,
  writeOutboxEvents,
  type Db,
  type OutboxEvent,
  type PersistDeps,
} from "./shared.js";

export type PersistClaimSubmitResult =
  | { readonly status: "submitted"; readonly event_id: string }
  | { readonly status: "disputed"; readonly event_id: string }
  | { readonly status: "duplicate"; readonly event_id: string }
  | { readonly status: "rejected"; readonly problem: ProblemDocument };

export type PersistClaimApproveResult =
  | { readonly status: "approved"; readonly event_id: string }
  | { readonly status: "duplicate"; readonly event_id: string }
  | { readonly status: "rejected"; readonly problem: ProblemDocument };

// --- claim.submit -------------------------------------------------

export async function persistClaimSubmit(
  db: Db,
  command: OsdsCommand,
  deps: PersistDeps,
  enabledMethods: readonly ClaimMethod[],
): Promise<PersistClaimSubmitResult> {
  try {
    return await withTenant(db, command.tenant_id, (trx) =>
      applySubmit(trx, command, deps, enabledMethods),
    );
  } catch (err) {
    if (isUniqueViolation(err)) {
      const original = await withTenant(db, command.tenant_id, (trx) =>
        findEventId(trx, command.tenant_id, command.idempotency_key),
      );
      if (original !== null) return { status: "duplicate", event_id: original };
    }
    throw err;
  }
}

async function applySubmit(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
  enabledMethods: readonly ClaimMethod[],
): Promise<PersistClaimSubmitResult> {
  const replayId = await findEventId(
    trx,
    command.tenant_id,
    command.idempotency_key,
  );
  if (replayId !== null) return { status: "duplicate", event_id: replayId };

  const listing = await loadClaimListing(
    trx,
    command.tenant_id,
    readPayloadId(command, "listing_id"),
  );
  const result = handleClaimSubmit(command, listing, enabledMethods);

  if (result.outcome === "rejected") {
    return { status: "rejected", problem: result.problem };
  }

  if (result.outcome === "disputed") {
    // §9.4: no state change here - the moderation flow owns the dispute record.
    const [draft] = result.events;
    const eventId = await writeOutboxEvents(trx, command, deps, [
      { type: draft.type, subject: draft.subject, data: draft.data },
    ]);
    return { status: "disputed", event_id: eventId };
  }

  const submitted = result.events[0];
  const verificationStarted = result.events[1];
  const method = submitted.data.claim.method;

  const claimantUserId = await upsertClaimant(
    trx,
    command.tenant_id,
    submitted.data.claimant,
    deps,
  );

  const claimId = `claim_${deps.newId()}`;
  await insertClaim(trx, {
    id: claimId,
    tenantId: command.tenant_id,
    listingId: submitted.data.claim.listing_id,
    claimantUserId,
    method,
    status: method === "manual" ? "pending" : "verifying",
    consent: submitted.data.consent,
    verification:
      method === "manual"
        ? {}
        : { method, expires_at: verificationStarted?.data.expires_at ?? null },
  });

  // Fold the minted claim id and the resolved claimant id back onto the events.
  const events: OutboxEvent[] = withClaimId(result, claimId).map(
    (event): OutboxEvent =>
      event.type === "claim.submitted"
        ? {
            type: event.type,
            subject: event.subject,
            data: {
              ...event.data,
              claimant: { ...event.data.claimant, id: claimantUserId },
            },
          }
        : { type: event.type, subject: event.subject, data: event.data },
  );

  const eventId = await writeOutboxEvents(trx, command, deps, events);
  return { status: "submitted", event_id: eventId };
}

// --- claim.approve ----------------------------------------------

export async function persistClaimApprove(
  db: Db,
  command: OsdsCommand,
  deps: PersistDeps,
): Promise<PersistClaimApproveResult> {
  try {
    return await withTenant(db, command.tenant_id, (trx) =>
      applyApprove(trx, command, deps),
    );
  } catch (err) {
    if (isUniqueViolation(err)) {
      const original = await withTenant(db, command.tenant_id, (trx) =>
        findEventId(trx, command.tenant_id, command.idempotency_key),
      );
      if (original !== null) return { status: "duplicate", event_id: original };
    }
    throw err;
  }
}

async function applyApprove(
  trx: Db,
  command: OsdsCommand,
  deps: PersistDeps,
): Promise<PersistClaimApproveResult> {
  const replayId = await findEventId(
    trx,
    command.tenant_id,
    command.idempotency_key,
  );
  if (replayId !== null) return { status: "duplicate", event_id: replayId };

  // Lock the claim, then its listing, for the rest of the transaction so
  // concurrent approvals of other claims on the same listing serialize here.
  const claim = await lockClaim(
    trx,
    command.tenant_id,
    readPayloadId(command, "claim_id"),
  );
  const listing =
    claim === null
      ? null
      : await lockClaimListing(trx, command.tenant_id, claim.listing_id);

  const result = handleClaimApprove(command, claim, listing);
  if (result.outcome === "rejected") {
    // A concurrent apply of the same command may have committed while we waited
    // on the lock; if the key now resolves, this is a replay, not a rejection.
    const raced = await findEventId(
      trx,
      command.tenant_id,
      command.idempotency_key,
    );
    if (raced !== null) return { status: "duplicate", event_id: raced };
    return { status: "rejected", problem: result.problem };
  }

  if (claim === null || listing === null) {
    // handleClaimApprove only accepts a non-null claim + listing.
    throw new Error(
      "claim.approve persistence: resolver accepted a null claim or listing",
    );
  }

  // §9.4: verification alone never moves ownership from a sitting owner. Under
  // the listing lock, an approved claim already on the listing wins.
  const priorOwnerClaim = await approvedClaimFor(
    trx,
    command.tenant_id,
    claim.listing_id,
  );
  if (priorOwnerClaim !== null && priorOwnerClaim !== claim.id) {
    return {
      status: "rejected",
      problem: validationProblem(
        `listing "${claim.listing_id}" already has an owner via approved ` +
          `claim "${priorOwnerClaim}" - verification alone never moves ` +
          `ownership from a sitting owner (§9.4)`,
      ),
    };
  }
  if (priorOwnerClaim === null && listing.status === "claimed") {
    return {
      status: "rejected",
      problem: validationProblem(
        `listing "${claim.listing_id}" is marked claimed but no approved ` +
          `claim explains it - refusing to reassign ownership`,
      ),
    };
  }

  const [approved, assigned] = result.events;
  const manualVerification =
    approved.data.manual_verification === null
      ? null
      : JSON.stringify(approved.data.manual_verification);

  await sql`
    update claims set
      status = 'approved',
      decided_by = ${approved.data.decided_by},
      decided_at = ${deps.now().toISOString()},
      manual_verification = ${manualVerification}::jsonb
    where tenant_id = ${command.tenant_id} and id = ${approved.data.claim.id}
  `.execute(trx);

  // listing.owner_assigned: the listing is now claimed. Owner identity is the
  // approved claim's claimant (carried on the event as owner_user_id).
  await sql`
    update listings set status = 'claimed'
    where tenant_id = ${command.tenant_id} and id = ${assigned.subject}
  `.execute(trx);

  const events: OutboxEvent[] = result.events.map((event): OutboxEvent => ({
    type: event.type,
    subject: event.subject,
    data: event.data,
  }));
  const eventId = await writeOutboxEvents(trx, command, deps, events);
  return { status: "approved", event_id: eventId };
}

// --- reads ---------------------------------------------------

function readPayloadId(command: OsdsCommand, key: string): string | null {
  const p = command.payload as Record<string, unknown>;
  const v = p[key];
  return typeof v === "string" && v ? v : null;
}

async function loadClaimListing(
  trx: Db,
  tenantId: string,
  listingId: string | null,
): Promise<ClaimListing | null> {
  if (listingId === null) return null;
  const res = await sql<{
    id: string;
    tenant_id: string;
    status: ClaimListing["status"];
  }>`
    select id, tenant_id, status from listings
    where tenant_id = ${tenantId} and id = ${listingId} limit 1
  `.execute(trx);
  const row = res.rows[0];
  if (row === undefined) return null;
  return { id: row.id, tenant_id: row.tenant_id, status: row.status };
}

/** {@link loadClaimListing} with `FOR UPDATE` - the approve serialization point. */
async function lockClaimListing(
  trx: Db,
  tenantId: string,
  listingId: string,
): Promise<ClaimListing | null> {
  const res = await sql<{
    id: string;
    tenant_id: string;
    status: ClaimListing["status"];
  }>`
    select id, tenant_id, status from listings
    where tenant_id = ${tenantId} and id = ${listingId}
    for update
  `.execute(trx);
  const row = res.rows[0];
  if (row === undefined) return null;
  return { id: row.id, tenant_id: row.tenant_id, status: row.status };
}

/** The id of an approved claim on the listing, if any (the sitting owner). */
async function approvedClaimFor(
  trx: Db,
  tenantId: string,
  listingId: string,
): Promise<string | null> {
  const res = await sql<{ id: string }>`
    select id from claims
    where tenant_id = ${tenantId} and listing_id = ${listingId}
      and status = 'approved'
    limit 1
  `.execute(trx);
  return res.rows[0]?.id ?? null;
}

interface ClaimRow {
  readonly id: string;
  readonly tenant_id: string;
  readonly listing_id: string;
  readonly status: ClaimStatus;
  readonly method: ClaimMethod | null;
  readonly claimant_user_id: string | null;
}

/** Row-locks the claim for the rest of the transaction (approve only). */
async function lockClaim(
  trx: Db,
  tenantId: string,
  claimId: string | null,
): Promise<ClaimRecord | null> {
  if (claimId === null) return null;
  const res = await sql<ClaimRow>`
    select id, tenant_id, listing_id, status, method, claimant_user_id
    from claims where tenant_id = ${tenantId} and id = ${claimId}
    for update
  `.execute(trx);
  const row = res.rows[0];
  // A row with no method never came from claim.submit; treat it as absent so
  // the resolver returns "the claim to approve does not exist".
  if (row === undefined || row.method === null) return null;
  return {
    id: row.id,
    tenant_id: row.tenant_id,
    listing_id: row.listing_id,
    status: row.status,
    method: row.method,
    claimant_user_id: row.claimant_user_id,
  };
}

// --- writes -------------------------------------------------

/**
 * Provisional claimant resolution (issue #44): key on `(tenant_id, email)`,
 * mint `usr_<newId>` when there is no row. `do update set email = excluded.email`
 * is a no-op that forces `returning` to yield the row on a conflict.
 */
async function upsertClaimant(
  trx: Db,
  tenantId: string,
  claimant: ClaimantData,
  deps: PersistDeps,
): Promise<string> {
  const res = await sql<{ id: string }>`
    insert into users (id, tenant_id, email, name, role)
    values (
      ${`usr_${deps.newId()}`}, ${tenantId}, ${claimant.email}, ${claimant.name}, 'owner'
    )
    on conflict (tenant_id, email) do update set email = excluded.email
    returning id
  `.execute(trx);
  const row = res.rows[0];
  if (row === undefined) {
    throw new Error(
      "claim.submit persistence: claimant upsert returned no row",
    );
  }
  return row.id;
}

interface ClaimInsert {
  readonly id: string;
  readonly tenantId: string;
  readonly listingId: string;
  readonly claimantUserId: string;
  readonly method: ClaimMethod;
  readonly status: "pending" | "verifying";
  readonly consent: unknown;
  readonly verification: unknown;
}

async function insertClaim(trx: Db, c: ClaimInsert): Promise<void> {
  await sql`
    insert into claims (
      id, tenant_id, listing_id, claimant_user_id, status, method,
      consent, verification
    ) values (
      ${c.id}, ${c.tenantId}, ${c.listingId}, ${c.claimantUserId}, ${c.status}, ${c.method},
      ${JSON.stringify(c.consent)}::jsonb, ${JSON.stringify(c.verification)}::jsonb
    )
  `.execute(trx);
}
