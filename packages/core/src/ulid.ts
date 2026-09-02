/**
 * The production id factory - a hand-written Crockford base32 ULID.
 *
 * Lives on the `@osds/core` root entrypoint next to {@link ROLE_RANK}: ULIDs
 * with entity prefixes are a settled project convention (decisions.md, "ULID
 * text primary keys with entity prefixes"), not a schema detail, so this is
 * core's to own - not `@osds/db`, which owns schema and generated types. Pure,
 * with no I/O beyond `crypto.randomBytes`; no dependency (Crockford base32 is
 * ~20 lines, and `crypto.randomUUID` was rejected because the convention is
 * ULIDs with prefixes).
 *
 * Layout (canonical ULID): 128 bits = a 48-bit big-endian millisecond
 * timestamp, then 80 bits of randomness, rendered as 26 Crockford base32
 * characters (`0123456789ABCDEFGHJKMNPQRSTVWXYZ` - no I, L, O, U). 26 * 5 = 130
 * bits, so the value is encoded as 130 bits with the top two forced to zero;
 * that is why a valid ULID's first character is always `0`-`7`.
 *
 * {@link newUlid} returns the bare 26-char id. Entity prefixes (`op_`,
 * `listing_`, `ses_`, ...) are the caller's to prepend, exactly as the persist
 * layer already does.
 *
 * NOT monotonic within a millisecond. Two ids minted in the same millisecond
 * share their first 10 characters and have independent random tails, so their
 * relative sort order is random. Ids ARE lexicographically ordered ACROSS
 * milliseconds. Consequence, and issue #92: outbox event order within a single
 * command must not depend on id ordering - it needs an explicit sequence, not a
 * sort on `id`. Do not make this factory stateful to paper over that.
 */
import { randomBytes } from "node:crypto";
import type { IdFactory } from "./command/context.js";

/** Crockford base32 - the standard alphabet minus I, L, O, U. */
const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

const TIMESTAMP_BYTES = 6; // 48 bits
const RANDOM_BYTES = 10; // 80 bits
/** 2^48 - 1 ms since the Unix epoch: `+010889-08-02T05:31:50.655Z`. */
const MAX_TIMESTAMP = 0xffffffffffff;

/**
 * Encode a ULID from an explicit timestamp and 10 bytes of randomness. Pure and
 * deterministic - the unit the fixed-vector tests pin. {@link newUlid} is this
 * with `Date.now()` and `randomBytes(10)`.
 *
 * @param ms   integer milliseconds since the Unix epoch, `0 .. 2^48 - 1`
 * @param rand exactly {@link RANDOM_BYTES} bytes
 * @throws RangeError if `ms` is out of the 48-bit range or `rand` is the wrong length
 */
export function encodeUlid(ms: number, rand: Uint8Array): string {
  if (!Number.isInteger(ms) || ms < 0 || ms > MAX_TIMESTAMP) {
    throw new RangeError(
      `ULID timestamp must be an integer in [0, ${MAX_TIMESTAMP}] ms; got ${ms}`,
    );
  }
  if (rand.length !== RANDOM_BYTES) {
    throw new RangeError(
      `ULID randomness must be exactly ${RANDOM_BYTES} bytes; got ${rand.length}`,
    );
  }

  // 16 big-endian bytes: the 6-byte timestamp, then the 10 random bytes.
  const bytes = new Uint8Array(TIMESTAMP_BYTES + RANDOM_BYTES);
  for (let i = 0; i < TIMESTAMP_BYTES; i++) {
    const shift = 8 * (TIMESTAMP_BYTES - 1 - i); // most-significant byte first
    bytes[i] = Math.floor(ms / 2 ** shift) % 256;
  }
  bytes.set(rand, TIMESTAMP_BYTES);

  // Read 5 bits at a time, MSB first. Seed the accumulator with two zero bits
  // so 2 + 128 = 130 = 26 * 5 divides evenly; mask off consumed bits each round
  // so the accumulator never exceeds ~12 bits.
  let out = "";
  let acc = 0;
  let bits = 2;
  for (const b of bytes) {
    acc = (acc << 8) | b;
    bits += 8;
    while (bits >= 5) {
      bits -= 5;
      out += CROCKFORD[(acc >> bits) & 0x1f];
    }
    acc &= (1 << bits) - 1;
  }
  return out;
}

/**
 * A fresh ULID: `Date.now()` for the timestamp, {@link RANDOM_BYTES} CSPRNG
 * bytes for the rest. The only impurity is `randomBytes`.
 *
 * @throws RangeError past the year 10889 - see {@link MAX_TIMESTAMP}. A clock
 * that far ahead is a fault, not a case to absorb by wrapping (which would sort
 * the id before earlier ones).
 */
export function newUlid(): string {
  return encodeUlid(Date.now(), randomBytes(RANDOM_BYTES));
}

/**
 * {@link newUlid} typed as {@link IdFactory}, for injecting the default factory
 * by name (e.g. into `CommandContext` / `PersistDeps`). The annotation is a
 * compile-time conformance check; `newUlid` on its own is already an
 * `IdFactory`.
 */
export const ulidFactory: IdFactory = newUlid;
