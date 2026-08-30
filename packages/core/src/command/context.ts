import type { Kysely } from "@osds/db";
import type { Scope } from "@osds/adapter-kit";

/** Mints a fresh ULID. Injected so core carries no ULID dependency of its own. */
export type IdFactory = () => string;

export interface CommandContext {
  /**
   * Database handle. The handler opens its own transaction and sets
   * `app.tenant_id` inside it, so this may be a pooled connection.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- generated schema types are not wired up yet
  readonly db: Kysely<any>;
  /** Scopes granted to `command.adapter_id` for `command.tenant_id`. */
  readonly scopes: readonly Scope[];
  /** ULID factory for event ids and new entitlement ids (the `ent_` prefix is added by the handler). */
  readonly newId: IdFactory;
  /** Wall clock. Defaults to `() => new Date()`. */
  readonly now?: () => Date;
}
