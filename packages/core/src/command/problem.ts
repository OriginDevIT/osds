/**
 * RFC 7807 problem documents for rejected commands, and the internal error used
 * to abort a command transaction with one.
 *
 * `status` on the document is advisory for the HTTP surface: 422 for a
 * validation failure, 403 for a scope denial. The wire contract in §7 only
 * distinguishes 202 / 409 / 422, so both map to `CommandResult.status =
 * "rejected"`.
 */
import type { ProblemDocument } from "@osds/adapter-kit";

/** Thrown inside command handling to unwind with a specific problem document. */
export class CommandRejected extends Error {
  constructor(readonly problem: ProblemDocument) {
    super(problem.title);
    this.name = "CommandRejected";
  }
}

export function validationProblem(
  detail: string,
  errors?: readonly string[],
): ProblemDocument {
  return {
    type: "https://osds.dev/problems/command-validation",
    title: "command validation failed",
    status: 422,
    code: "validation_failed",
    detail,
    ...(errors && errors.length > 0 ? { errors: [...errors] } : {}),
  };
}

export function scopeProblem(adapterId: string, requiredScope: string): ProblemDocument {
  return {
    type: "https://osds.dev/problems/scope-denied",
    title: "adapter is not permitted to run this command",
    status: 403,
    code: "scope_denied",
    detail: `adapter "${adapterId}" lacks required scope "${requiredScope}"`,
  };
}
