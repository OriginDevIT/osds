import { ulidFactory } from "@osds/core";
import type { PersistDeps } from "@osds/core/persist";

/**
 * The injected effects `@osds/core/persist` needs - the real wall clock and the
 * production ULID factory (`@osds/core`'s `ulidFactory`). One instance, reused
 * across requests; both members are stateless.
 *
 * `serializeSessionCookie` also accepts this (it reads only `.now`).
 */
export const persistDeps: PersistDeps = {
  now: () => new Date(),
  newId: ulidFactory,
};
