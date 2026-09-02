/**
 * The entitlement command handler - spec §7 (commands) and §11.1 (outbox).
 *
 * Scoped to the two commands that drive the §6 state machine:
 * `entitlement.reportPayment` and `entitlement.grant`. For each it:
 *
 *   1. rejects an unhandled command name (422)
 *   2. checks the adapter holds `command:entitlement` *before* any execution (403)
 *   3. validates the envelope and payload (422)
 *   4. returns the original event id on an `idempotency_key` replay (409)
 *   5. otherwise, in one transaction: sets `app.tenant_id`, loads the live
 *      entitlement, runs the §6.3 transition, writes the entitlement and the
 *      listing tier cache, and appends the emitted events to the outbox - the
 *      state change and the events commit together (§11.1).
 *
 * Money/time-derived transitions (dunning expiry, term end, cancellation) are
 * the scheduler's job and are not reachable from these two commands.
 */
import { sql } from "@osds/db";
import type { Kysely } from "@osds/db";
import type { CommandResult, OsdsCommand } from "@osds/adapter-kit";
import {
  transition,
  IllegalTransitionError,
  type EntitlementStatus,
  type EntitlementTrigger,
  type EmittedEventType,
  type TransitionResult,
} from "../entitlement.js";
import type { CommandContext } from "./context.js";
import { CommandRejected, scopeProblem, validationProblem } from "./problem.js";
import { parseCommand, type ParsedCommand } from "./validate.js";

const REQUIRED_SCOPE = "command:entitlement" as const;
const HANDLED: ReadonlySet<string> = new Set([
  "entitlement.reportPayment",
  "entitlement.grant",
]);

const DAY_MS = 86_400_000;

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- generated schema types are not wired up yet
type Db = Kysely<any>;

interface EntitlementRow {
  readonly id: string;
  readonly status: EntitlementStatus;
  readonly tier: string;
  readonly dunning_started_at: Date | string | null;
  readonly grace_ends_at: Date | string | null;
}

/** Internal signal: a concurrent apply won the idempotency race. */
class ReplayDetected {
  constructor(readonly eventId: string) {}
}

export async function handleCommand(
  command: OsdsCommand,
  ctx: CommandContext,
): Promise<CommandResult> {
  const now = ctx.now ?? ((): Date => new Date());

  if (!HANDLED.has(command.command)) {
    return {
      status: "rejected",
      problem: validationProblem(
        `command "${command.command}" is not handled by the entitlement layer`,
      ),
    };
  }

  if (!ctx.scopes.includes(REQUIRED_SCOPE)) {
    return { status: "rejected", problem: scopeProblem(command.adapter_id ?? "unknown", REQUIRED_SCOPE) };
  }

  let parsed: ParsedCommand;
  try {
    parsed = parseCommand(command);
  } catch (err) {
    if (err instanceof CommandRejected) return { status: "rejected", problem: err.problem };
    throw err;
  }

  const replay = await findEventByIdempotencyKey(
    ctx.db,
    command.tenant_id,
    command.idempotency_key,
  );
  if (replay !== null) {
    return { status: "duplicate", event_id: replay };
  }

  try {
    const eventId = await ctx.db.transaction().execute(async (trx) => {
      await sql`select set_config('app.tenant_id', ${command.tenant_id}, true)`.execute(trx);

      const raced = await findEventByIdempotencyKey(
        trx,
        command.tenant_id,
        command.idempotency_key,
      );
      if (raced !== null) throw new ReplayDetected(raced);

      return applyCommand(trx, command, parsed, ctx, now());
    });

    return { status: "accepted", event_id: eventId };
  } catch (err) {
    if (err instanceof ReplayDetected) {
      return { status: "duplicate", event_id: err.eventId };
    }
    if (err instanceof CommandRejected) {
      return { status: "rejected", problem: err.problem };
    }
    if (isUniqueViolation(err)) {
      // A concurrent apply of the same command won the race; return its event.
      const original = await findEventByIdempotencyKey(
        ctx.db,
        command.tenant_id,
        command.idempotency_key,
      );
      if (original !== null) return { status: "duplicate", event_id: original };
    }
    throw err;
  }
}

// ---------------------------------------------------------------------------

async function applyCommand(
  trx: Db,
  command: OsdsCommand,
  parsed: ParsedCommand,
  ctx: CommandContext,
  now: Date,
): Promise<string> {
  await assertTenantExists(trx, command.tenant_id);

  // Serialize concurrent commands for the same listing on its row. A second
  // identical command blocks here until the first commits, then the
  // idempotency re-check below sees the winner's event and returns it as a
  // replay rather than racing on the entitlement insert.
  await lockListing(trx, command.tenant_id, parsed.listingId);

  const raced = await findEventByIdempotencyKey(
    trx,
    command.tenant_id,
    command.idempotency_key,
  );
  if (raced !== null) throw new ReplayDetected(raced);

  const current = await loadLiveEntitlement(trx, command.tenant_id, parsed.listingId);
  const from: EntitlementStatus = current?.status ?? "none";

  const trigger: EntitlementTrigger =
    parsed.kind === "grant"
      ? { type: "admin_override", to: "comped", adminId: parsed.adminId, reason: parsed.reason }
      : reportPaymentTrigger(parsed, from);

  let result: TransitionResult;
  try {
    result = transition(from, trigger);
  } catch (err) {
    if (err instanceof IllegalTransitionError) {
      throw new CommandRejected(
        validationProblem(`no entitlement transition from "${err.from}" for this command`),
      );
    }
    throw err;
  }

  const fallbackTier = await loadFallbackTier(trx, command.tenant_id);
  const nextTier = computeNextTier(parsed, result, current, fallbackTier);

  if (parsed.kind === "grant") {
    await assertTierExists(trx, command.tenant_id, parsed.tier);
  } else if (parsed.outcome === "succeeded" && parsed.tier !== null) {
    await assertTierExists(trx, command.tenant_id, parsed.tier);
  }

  await writeEntitlement(trx, command, parsed, result, current, nextTier, now, ctx);

  const changesListingTier =
    parsed.kind === "grant" || result.emits.includes("listing.tier_changed");
  if (changesListingTier) {
    await sql`
      update listings set tier = ${nextTier}, updated_at = now()
      where tenant_id = ${command.tenant_id} and id = ${parsed.listingId}
    `.execute(trx);
  }

  const fromTier = current?.tier ?? fallbackTier;
  return writeOutbox(trx, command, parsed, result, ctx, {
    now,
    current,
    fromTier,
    toTier: nextTier,
  });
}

function reportPaymentTrigger(
  parsed: Extract<ParsedCommand, { kind: "reportPayment" }>,
  from: EntitlementStatus,
): EntitlementTrigger {
  switch (parsed.outcome) {
    case "succeeded":
      if (from === "none") return { type: "checkout_completed", trial: false };
      if (from === "trialing") return { type: "trial_ended", paymentSucceeded: true };
      if (from === "past_due" || from === "grace") return { type: "payment_succeeded" };
      break;
    case "failed":
      if (from === "active") return { type: "payment_failed" };
      if (from === "trialing") return { type: "trial_ended", paymentSucceeded: false };
      break;
    case "refunded":
      if (from === "active") return { type: "refund_issued" };
      break;
  }
  throw new CommandRejected(
    validationProblem(
      `payment outcome "${parsed.outcome}" has no effect on an entitlement in state "${from}"`,
    ),
  );
}

function computeNextTier(
  parsed: ParsedCommand,
  result: TransitionResult,
  current: EntitlementRow | null,
  fallbackTier: string | null,
): string | null {
  if (parsed.kind === "grant") return parsed.tier;
  switch (result.to) {
    case "active":
      return parsed.tier ?? current?.tier ?? null;
    case "expired":
      return fallbackTier;
    default:
      // past_due keeps full perks (§6.5); other states leave the cache as-is.
      return current?.tier ?? parsed.tier ?? null;
  }
}

// --- persistence -----------------------------------------------------------

async function writeEntitlement(
  trx: Db,
  command: OsdsCommand,
  parsed: ParsedCommand,
  result: TransitionResult,
  current: EntitlementRow | null,
  nextTier: string | null,
  now: Date,
  ctx: CommandContext,
): Promise<void> {
  const isGrant = parsed.kind === "grant";
  const comp = isGrant
    ? JSON.stringify({
        granted_by: parsed.adminId,
        reason: parsed.reason,
        expires_at: parsed.expiresAt,
      })
    : null;
  const paymentRef =
    parsed.kind === "reportPayment"
      ? JSON.stringify({ adapter: command.adapter_id, external_id: parsed.externalId })
      : null;
  const periodEnd = parsed.kind === "reportPayment" ? parsed.periodEnd : null;

  if (current === null) {
    await sql`
      insert into entitlements
        (id, tenant_id, listing_id, tier, status, billing_mode,
         current_period_end, started_at, payment_ref, comp, created_at, updated_at)
      values
        (${"ent_" + ctx.newId()}, ${command.tenant_id}, ${parsed.listingId},
         ${nextTier}, ${result.to}, ${isGrant ? "comp" : "recurring"},
         ${periodEnd}, ${now.toISOString()},
         ${paymentRef}::jsonb, ${comp}::jsonb, now(), now())
    `.execute(trx);
    return;
  }

  const dunningStartedAt = result.to === "past_due" ? now.toISOString() : null;
  const graceEndsAt =
    result.to === "grace" ? new Date(now.getTime() + 30 * DAY_MS).toISOString() : null;

  await sql`
    update entitlements set
      status = ${result.to},
      tier = ${nextTier},
      current_period_end = coalesce(${periodEnd}, current_period_end),
      dunning_started_at = coalesce(${dunningStartedAt}, dunning_started_at),
      grace_ends_at = coalesce(${graceEndsAt}, grace_ends_at),
      comp = coalesce(${comp}::jsonb, comp),
      payment_ref = coalesce(${paymentRef}::jsonb, payment_ref),
      updated_at = now()
    where id = ${current.id}
  `.execute(trx);
}

async function writeOutbox(
  trx: Db,
  command: OsdsCommand,
  parsed: ParsedCommand,
  result: TransitionResult,
  ctx: CommandContext,
  facts: {
    now: Date;
    current: EntitlementRow | null;
    fromTier: string | null;
    toTier: string | null;
  },
): Promise<string> {
  const actor =
    parsed.kind === "grant"
      ? { type: "admin", id: parsed.adminId }
      : { type: "adapter", id: command.adapter_id };
  const occurredAt = facts.now.toISOString();

  let primaryId = "";
  for (const [i, type] of result.emits.entries()) {
    const id = ctx.newId();
    if (i === 0) primaryId = id;

    const data = buildEventData(type, { parsed, result, facts });

    await sql`
      insert into outbox
        (id, tenant_id, type, version, occurred_at, subject, actor, origin,
         trace_id, data, idempotency_key)
      values
        (${id}, ${command.tenant_id}, ${type}, 1, ${occurredAt}, ${parsed.listingId},
         ${JSON.stringify(actor)}::jsonb, ${command.adapter_id}, ${command.trace_id},
         ${JSON.stringify(data)}::jsonb, ${i === 0 ? command.idempotency_key : null})
    `.execute(trx);
  }
  return primaryId;
}

function buildEventData(
  type: EmittedEventType,
  args: {
    parsed: ParsedCommand;
    result: TransitionResult;
    facts: {
      now: Date;
      current: EntitlementRow | null;
      fromTier: string | null;
      toTier: string | null;
    };
  },
): Record<string, unknown> {
  const { parsed, result, facts } = args;
  const periodEnd = parsed.kind === "reportPayment" ? parsed.periodEnd : null;

  switch (type) {
    case "entitlement.started":
      return {
        tier: facts.toTier,
        billing_mode: parsed.kind === "grant" ? "comp" : "recurring",
        period_end: periodEnd,
        trial_ends_at: null,
      };
    case "entitlement.trial_converted":
      return { tier: facts.toTier, period_end: periodEnd };
    case "entitlement.recovered":
      return { days_in_dunning: daysSince(facts.current?.dunning_started_at, facts.now) };
    case "entitlement.restored":
      return { tier: facts.toTier, days_in_grace: null };
    case "entitlement.dunning_started":
      return {
        attempt: 1,
        dunning_ends_at: new Date(facts.now.getTime() + 14 * DAY_MS).toISOString(),
      };
    case "entitlement.expired":
      return {
        from_tier: facts.fromTier,
        cause:
          parsed.kind === "reportPayment" && parsed.outcome === "refunded" ? "refund" : "other",
      };
    case "entitlement.overridden":
      return {
        admin_id: parsed.kind === "grant" ? parsed.adminId : null,
        reason: parsed.kind === "grant" ? parsed.reason : null,
        from: result.from,
        to: result.to,
      };
    case "listing.tier_changed":
      return {
        from_tier: facts.fromTier,
        to_tier: facts.toTier,
        effective_at: facts.now.toISOString(),
        cause: causeFor(parsed),
      };
    default:
      return {};
  }
}

function causeFor(parsed: ParsedCommand): string {
  if (parsed.kind === "grant") return "admin_grant";
  switch (parsed.outcome) {
    case "succeeded":
      return "payment";
    case "failed":
      return "payment_failed";
    case "refunded":
      return "refund";
  }
}

function daysSince(from: Date | string | null | undefined, to: Date): number | null {
  if (from === null || from === undefined) return null;
  const start = from instanceof Date ? from : new Date(from);
  return Math.max(0, Math.floor((to.getTime() - start.getTime()) / DAY_MS));
}

// --- reads ---------------------------------------------------------------

async function findEventByIdempotencyKey(
  db: Db,
  tenantId: string,
  key: string,
): Promise<string | null> {
  const res = await sql<{ id: string }>`
    select id from outbox
    where tenant_id = ${tenantId} and idempotency_key = ${key}
    limit 1
  `.execute(db);
  return res.rows[0]?.id ?? null;
}

async function assertTenantExists(trx: Db, tenantId: string): Promise<void> {
  const res = await sql<{ one: number }>`
    select 1 as one from tenants where id = ${tenantId} limit 1
  `.execute(trx);
  if (res.rows.length === 0) {
    throw new CommandRejected(validationProblem(`tenant "${tenantId}" does not exist`));
  }
}

/** Row-lock the listing for the rest of the transaction; 422 if it does not exist. */
async function lockListing(trx: Db, tenantId: string, listingId: string): Promise<void> {
  const res = await sql<{ one: number }>`
    select 1 as one from listings
    where tenant_id = ${tenantId} and id = ${listingId}
    for update
  `.execute(trx);
  if (res.rows.length === 0) {
    throw new CommandRejected(validationProblem(`listing "${listingId}" does not exist`));
  }
}

async function assertTierExists(trx: Db, tenantId: string, tierKey: string): Promise<void> {
  const res = await sql<{ one: number }>`
    select 1 as one from tiers
    where tenant_id = ${tenantId} and key = ${tierKey} limit 1
  `.execute(trx);
  if (res.rows.length === 0) {
    throw new CommandRejected(
      validationProblem(`tier "${tierKey}" is not defined for this tenant`),
    );
  }
}

async function loadLiveEntitlement(
  trx: Db,
  tenantId: string,
  listingId: string,
): Promise<EntitlementRow | null> {
  const res = await sql<EntitlementRow>`
    select id, status, tier, dunning_started_at, grace_ends_at
    from entitlements
    where tenant_id = ${tenantId} and listing_id = ${listingId}
      and status not in ('expired', 'canceled')
    order by created_at desc
    limit 1
    for update
  `.execute(trx);
  return res.rows[0] ?? null;
}

async function loadFallbackTier(trx: Db, tenantId: string): Promise<string | null> {
  const res = await sql<{ key: string }>`
    select key from tiers where tenant_id = ${tenantId} and rank = 0 limit 1
  `.execute(trx);
  return res.rows[0]?.key ?? null;
}

function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "23505"
  );
}
