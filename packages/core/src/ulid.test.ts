/**
 * Pure - no DB, no network, always runs. `encodeUlid(ms, rand)` is the
 * deterministic seam the fixed vectors pin; `newUlid()` gets property tests
 * with a faked clock.
 */
import { randomBytes } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { encodeUlid, newUlid, ulidFactory } from "./index.js";
import type { IdFactory } from "./index.js";

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const ULID_RE = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const MAX_TS = 0xffffffffffff;

const zeros = (): Uint8Array => new Uint8Array(10);
const b = (...xs: number[]): Uint8Array => Uint8Array.from(xs);

/** Reference decoder: 26 Crockford chars -> 48-bit ms + 10 bytes. Test-only. */
function decodeUlid(s: string): { ms: number; rand: Uint8Array } {
  let acc = 0n;
  for (const ch of s) {
    const v = CROCKFORD.indexOf(ch);
    if (v < 0) throw new Error(`not a Crockford char: ${ch}`);
    acc = (acc << 5n) | BigInt(v);
  }
  acc &= (1n << 128n) - 1n; // drop the two leading zero bits
  const rand = new Uint8Array(10);
  for (let i = 9; i >= 0; i--) {
    rand[i] = Number(acc & 0xffn);
    acc >>= 8n;
  }
  return { ms: Number(acc & 0xffffffffffffn), rand };
}

describe("encodeUlid - fixed vectors", () => {
  it.each<[string, number, Uint8Array, string]>([
    ["all zero", 0, zeros(), "0".repeat(26)],
    [
      "all-ones randomness",
      0,
      Uint8Array.from({ length: 10 }, () => 0xff),
      "0000000000" + "Z".repeat(16),
    ],
    ["timestamp = 1 (LSB)", 1, zeros(), "0000000001" + "0".repeat(16)],
    ["timestamp = 2^48-1 (ceiling)", MAX_TS, zeros(), "7ZZZZZZZZZ" + "0".repeat(16)],
    [
      "canonical ULID time vector (1469918176385)",
      1469918176385,
      zeros(),
      "01ARYZ6S41" + "0".repeat(16),
    ],
    [
      "randomness MSB set - catches an endianness flip",
      0,
      b(0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0),
      "0000000000G" + "0".repeat(15),
    ],
    [
      "randomness LSB set",
      0,
      b(0, 0, 0, 0, 0, 0, 0, 0, 0, 0x01),
      "0".repeat(25) + "1",
    ],
  ])("%s", (_label, ms, rand, expected) => {
    expect(encodeUlid(ms, rand)).toBe(expected);
    expect(encodeUlid(ms, rand)).toHaveLength(26);
  });

  it("round-trips through a reference decoder for random inputs", () => {
    for (let i = 0; i < 2000; i++) {
      const ms = Math.floor(Math.random() * (MAX_TS + 1));
      const rand = randomBytes(10);
      const decoded = decodeUlid(encodeUlid(ms, rand));
      expect(decoded.ms).toBe(ms);
      expect([...decoded.rand]).toEqual([...rand]);
    }
  });
});

describe("encodeUlid - guards", () => {
  it("rejects a timestamp past 48 bits", () => {
    expect(() => encodeUlid(MAX_TS + 1, zeros())).toThrow(RangeError);
    expect(() => encodeUlid(2 ** 48, zeros())).toThrow(RangeError);
  });

  it("accepts the largest 48-bit timestamp, first char '7'", () => {
    expect(encodeUlid(MAX_TS, zeros())[0]).toBe("7");
  });

  it("rejects a negative, fractional, or NaN timestamp", () => {
    expect(() => encodeUlid(-1, zeros())).toThrow(RangeError);
    expect(() => encodeUlid(1.5, zeros())).toThrow(RangeError);
    expect(() => encodeUlid(Number.NaN, zeros())).toThrow(RangeError);
  });

  it("rejects randomness that is not exactly 10 bytes", () => {
    expect(() => encodeUlid(0, new Uint8Array(9))).toThrow(RangeError);
    expect(() => encodeUlid(0, new Uint8Array(11))).toThrow(RangeError);
  });
});

describe("newUlid", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("is 26 Crockford chars with a 0-7 first char", () => {
    for (let i = 0; i < 1000; i++) expect(newUlid()).toMatch(ULID_RE);
  });

  it("does not collide across 100k calls (80 bits of randomness per id)", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 100_000; i++) seen.add(newUlid());
    expect(seen.size).toBe(100_000);
  });

  it("always supplies encodeUlid exactly 10 random bytes", () => {
    // encodeUlid throws RangeError on any other length, so 5000 clean calls
    // is the assertion that newUlid never passes the wrong count.
    for (let i = 0; i < 5000; i++) expect(newUlid()).toMatch(ULID_RE);
  });

  it("takes its timestamp from Date.now()", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1469918176385));
    expect(newUlid().slice(0, 10)).toBe("01ARYZ6S41");
  });

  it("is lexicographically ordered across milliseconds", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_000_000));
    const a = newUlid();
    vi.setSystemTime(new Date(1_700_000_000_001));
    const c = newUlid();
    vi.setSystemTime(new Date(1_700_000_050_000));
    const d = newUlid();
    expect(a < c).toBe(true);
    expect(c < d).toBe(true);
  });

  it("shares the time prefix within one millisecond; the tail is not ordered", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_000_000));
    const ids = Array.from({ length: 50 }, () => newUlid());
    // Same ms -> identical first 10 chars.
    expect(new Set(ids.map((s) => s.slice(0, 10))).size).toBe(1);
    // Distinct tails, but deliberately no assertion about their order (#92).
    expect(new Set(ids).size).toBe(50);
  });
});

describe("ulidFactory", () => {
  it("is an IdFactory and is newUlid itself", () => {
    const f: IdFactory = ulidFactory;
    expect(f).toBe(newUlid);
    expect(f()).toMatch(ULID_RE);
  });
});
