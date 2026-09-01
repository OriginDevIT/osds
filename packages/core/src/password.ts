/**
 * Operator password hashing - decisions.md, "Authentication" ("Password
 * hashing" row). Node's `crypto.scrypt` only: zero dependencies, no native
 * build, no toolchain. Weaker than argon2id against a GPU attacker but a
 * legitimate KDF at OWASP parameters.
 *
 * Pure and I/O-free (crypto is CPU work, not a database or a socket). Only
 * `hash` is non-deterministic, and only through its CSPRNG salt.
 *
 * The stored string is PHC-style and carries every parameter:
 *
 *   $scrypt$ln=<log2 N>,r=<r>,p=<p>$<base64 salt>$<base64 digest>
 *
 * so raising the cost later is a one-constant change to {@link CURRENT} plus a
 * rehash on next successful login ({@link needsRehash}), never a migration.
 *
 * Parameters (OWASP Password Storage Cheat Sheet, scrypt):
 *   - N = 2^16, r = 8, p = 1. The cheat sheet's floor is 2^17; 2^16 is still a
 *     legitimate KDF and is chosen deliberately - the §13 reference compose
 *     runs app, worker, Postgres and MinIO on one box, and ~128 MiB per
 *     concurrent hash at 2^17 risks an OOM during the first-run wizard, the
 *     worst moment to fail. 2^16 is ~64 MiB. Raise it when the deployment
 *     story allows.
 *   - `maxmem` must be passed explicitly or scrypt throws at this N (Node's
 *     default cap is 32 MiB). It is derived from the parameters in play, so a
 *     `verify` against an older, cheaper hash computes the right bound for
 *     *that* hash.
 *
 * Base64 is the standard alphabet with padding stripped (PHC convention;
 * Node's decoder tolerates the missing `=`).
 *
 * Not this module's job: a minimum password length (the wizard / account form
 * owns that), and equalising login timing for unknown vs known accounts (the
 * request handler feeds a dummy hash to `verify`). The 1024-character ceiling
 * here is only a denial-of-service guard.
 */
import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import type { ScryptOptions } from "node:crypto";

/** scrypt cost parameters. `ln` is log2(N) - PHC convention, and forces a power of two. */
interface ScryptParams {
  readonly ln: number;
  readonly r: number;
  readonly p: number;
}

/** Current cost. Bump this and every older stored hash reports {@link needsRehash}. */
const CURRENT: ScryptParams = { ln: 16, r: 8, p: 1 };

const SALT_BYTES = 16;
const DIGEST_BYTES = 32;

/** Longest password we will hash. Purely a DoS guard - scrypt cost is linear in input size. */
const MAX_PASSWORD_LENGTH = 1024;

/**
 * Hard ceiling on a stored hash's memory demand (`128 * N * r`). A corrupt or
 * hostile string claiming `ln=30` must not make {@link verify} ask scrypt for
 * a gigabyte.
 */
const MAX_STORED_MEM = 1024 * 1024 * 1024;

/** Thrown when a stored string is not a hash this module produced. */
export class InvalidPasswordHashError extends Error {
  constructor(detail: string) {
    super(`invalid password hash: ${detail}`);
    this.name = "InvalidPasswordHashError";
  }
}

// --- public API ----------------------------------------------------------

/**
 * Hash `password` at the {@link CURRENT} cost. Returns the PHC-style stored
 * string. Rejects an empty password or one over {@link MAX_PASSWORD_LENGTH}.
 */
export async function hash(password: string): Promise<string> {
  if (password.length === 0) {
    throw new RangeError("password must not be empty");
  }
  if (password.length > MAX_PASSWORD_LENGTH) {
    throw new RangeError(
      `password must be at most ${MAX_PASSWORD_LENGTH} characters`,
    );
  }

  const salt = await randomBytesAsync(SALT_BYTES);
  const digest = await deriveKey(
    password.normalize("NFC"),
    salt,
    DIGEST_BYTES,
    CURRENT,
  );
  return format(CURRENT, salt, digest);
}

/**
 * Whether `password` matches `stored`. Recomputes the digest at the parameters
 * recorded in `stored` - so an older, cheaper hash still verifies - and
 * compares in constant time. Returns `false` on mismatch; throws
 * {@link InvalidPasswordHashError} when `stored` is not a well-formed hash.
 */
export async function verify(
  password: string,
  stored: string,
): Promise<boolean> {
  const parsed = parse(stored);

  // Never a hash we produced (`hash` caps at MAX_PASSWORD_LENGTH), and running
  // scrypt over a huge input would be the DoS this cap exists to stop.
  if (password.length > MAX_PASSWORD_LENGTH) return false;

  const computed = await deriveKey(
    password.normalize("NFC"),
    parsed.salt,
    parsed.digest.length,
    parsed,
  );
  return timingSafeEqual(computed, parsed.digest);
}

/**
 * Whether `stored` was hashed below the {@link CURRENT} cost on any axis and
 * should be re-hashed on the next successful login. Throws
 * {@link InvalidPasswordHashError} on a malformed string.
 */
export function needsRehash(stored: string): boolean {
  const { ln, r, p } = parse(stored);
  return ln < CURRENT.ln || r < CURRENT.r || p < CURRENT.p;
}

// --- internals ---------------------------------------------------------

interface ParsedHash extends ScryptParams {
  readonly salt: Buffer;
  readonly digest: Buffer;
}

function parse(stored: string): ParsedHash {
  const parts = stored.split("$");
  if (parts.length !== 5 || parts[0] !== "" || parts[1] !== "scrypt") {
    throw new InvalidPasswordHashError("not a $scrypt$ string");
  }
  const [, , paramStr, saltB64, digestB64] = parts as [
    string,
    string,
    string,
    string,
    string,
  ];

  const params = parseParams(paramStr);
  const N = 2 ** params.ln;
  if (128 * N * params.r > MAX_STORED_MEM) {
    throw new InvalidPasswordHashError("parameters exceed the memory ceiling");
  }

  const salt = decodeB64(saltB64, "salt");
  const digest = decodeB64(digestB64, "digest");
  if (salt.length < 8 || digest.length < 16) {
    throw new InvalidPasswordHashError("salt or digest too short");
  }

  return { ...params, salt, digest };
}

function parseParams(paramStr: string): ScryptParams {
  const seen = new Map<string, number>();
  for (const pair of paramStr.split(",")) {
    const eq = pair.indexOf("=");
    const key = eq === -1 ? pair : pair.slice(0, eq);
    const raw = eq === -1 ? "" : pair.slice(eq + 1);
    if (!/^[1-9]\d*$/.test(raw)) {
      throw new InvalidPasswordHashError(`parameter "${key}" is not a positive integer`);
    }
    seen.set(key, Number(raw));
  }
  const ln = seen.get("ln");
  const r = seen.get("r");
  const p = seen.get("p");
  if (seen.size !== 3 || ln === undefined || r === undefined || p === undefined) {
    throw new InvalidPasswordHashError("parameters must be exactly ln, r, p");
  }
  if (ln < 1 || ln > 30) {
    throw new InvalidPasswordHashError("ln out of range");
  }
  return { ln, r, p };
}

function decodeB64(value: string, what: string): Buffer {
  if (!/^[A-Za-z0-9+/]+$/.test(value)) {
    throw new InvalidPasswordHashError(`${what} is not base64`);
  }
  return Buffer.from(value, "base64");
}

function format(params: ScryptParams, salt: Buffer, digest: Buffer): string {
  const b64 = (b: Buffer): string => b.toString("base64").replace(/=+$/, "");
  return `$scrypt$ln=${params.ln},r=${params.r},p=${params.p}$${b64(salt)}$${b64(digest)}`;
}

/** scrypt at the given parameters, with `maxmem` derived so Node does not reject high N. */
function deriveKey(
  password: string,
  salt: Buffer,
  keylen: number,
  params: ScryptParams,
): Promise<Buffer> {
  const N = 2 ** params.ln;
  const options: ScryptOptions = {
    N,
    r: params.r,
    p: params.p,
    maxmem: 128 * N * params.r * 2,
  };
  return new Promise((resolve, reject) => {
    scrypt(password, salt, keylen, options, (err, derived) => {
      if (err) reject(err);
      else resolve(derived);
    });
  });
}

function randomBytesAsync(size: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    randomBytes(size, (err, buf) => {
      if (err) reject(err);
      else resolve(buf);
    });
  });
}
