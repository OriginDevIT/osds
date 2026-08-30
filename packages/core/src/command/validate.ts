/**
 * Structural validation of the two entitlement commands. Shape only - existence
 * of the tenant / listing / tier is checked later, inside the transaction.
 */
import type { OsdsCommand } from "@osds/adapter-kit";
import { CommandRejected, validationProblem } from "./problem.js";

export type PaymentOutcome = "succeeded" | "failed" | "refunded";

export type ParsedCommand =
  | {
      readonly kind: "reportPayment";
      readonly listingId: string;
      readonly outcome: PaymentOutcome;
      /** Present and required only when `outcome === "succeeded"`. */
      readonly tier: string | null;
      readonly periodEnd: string | null;
      readonly externalId: string | null;
    }
  | {
      readonly kind: "grant";
      readonly listingId: string;
      readonly tier: string;
      readonly adminId: string;
      readonly reason: string;
      readonly expiresAt: string | null;
    };

const OUTCOMES: ReadonlySet<string> = new Set(["succeeded", "failed", "refunded"]);

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === "string" && v.trim().length > 0;
}

function isIsoInstant(v: unknown): v is string {
  return typeof v === "string" && !Number.isNaN(Date.parse(v));
}

/** Validate the envelope and payload. Throws {@link CommandRejected} (422) on any problem. */
export function parseCommand(cmd: OsdsCommand): ParsedCommand {
  const errors: string[] = [];

  if (!nonEmptyString(cmd.idempotency_key)) errors.push("idempotency_key is required");
  if (!nonEmptyString(cmd.tenant_id)) errors.push("tenant_id is required");
  if (!nonEmptyString(cmd.adapter_id)) errors.push("adapter_id is required");
  if (!nonEmptyString(cmd.trace_id)) errors.push("trace_id is required");
  if (!isObject(cmd.payload)) errors.push("payload must be an object");

  if (errors.length > 0) {
    throw new CommandRejected(validationProblem("malformed command envelope", errors));
  }

  const p = cmd.payload as Record<string, unknown>;

  if (cmd.command === "entitlement.reportPayment") {
    if (!nonEmptyString(p["listing_id"])) errors.push("payload.listing_id is required");
    if (!OUTCOMES.has(p["outcome"] as string)) {
      errors.push('payload.outcome must be one of "succeeded", "failed", "refunded"');
    }
    const succeeded = p["outcome"] === "succeeded";
    if (succeeded && !nonEmptyString(p["tier"])) {
      errors.push("payload.tier is required when outcome is succeeded");
    }
    if (succeeded && !isIsoInstant(p["period_end"])) {
      errors.push("payload.period_end must be an RFC 3339 timestamp when outcome is succeeded");
    }
    if (p["external_id"] !== undefined && !nonEmptyString(p["external_id"])) {
      errors.push("payload.external_id, if present, must be a non-empty string");
    }
    if (errors.length > 0) {
      throw new CommandRejected(validationProblem("invalid entitlement.reportPayment payload", errors));
    }
    return {
      kind: "reportPayment",
      listingId: p["listing_id"] as string,
      outcome: p["outcome"] as PaymentOutcome,
      tier: succeeded ? (p["tier"] as string) : null,
      periodEnd: succeeded ? (p["period_end"] as string) : null,
      externalId: nonEmptyString(p["external_id"]) ? p["external_id"] : null,
    };
  }

  if (cmd.command === "entitlement.grant") {
    if (!nonEmptyString(p["listing_id"])) errors.push("payload.listing_id is required");
    if (!nonEmptyString(p["tier"])) errors.push("payload.tier is required");
    if (!nonEmptyString(p["admin_id"])) errors.push("payload.admin_id is required");
    if (!nonEmptyString(p["reason"])) errors.push("payload.reason is required");
    if (
      p["expires_at"] !== undefined &&
      p["expires_at"] !== null &&
      !isIsoInstant(p["expires_at"])
    ) {
      errors.push("payload.expires_at, if present, must be null or an RFC 3339 timestamp");
    }
    if (errors.length > 0) {
      throw new CommandRejected(validationProblem("invalid entitlement.grant payload", errors));
    }
    return {
      kind: "grant",
      listingId: p["listing_id"] as string,
      tier: p["tier"] as string,
      adminId: p["admin_id"] as string,
      reason: p["reason"] as string,
      expiresAt: isIsoInstant(p["expires_at"]) ? p["expires_at"] : null,
    };
  }

  // Unreachable: handleCommand checks the command name first.
  throw new CommandRejected(
    validationProblem(`command "${cmd.command}" is not an entitlement command`),
  );
}
