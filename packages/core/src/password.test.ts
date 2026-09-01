import { describe, expect, it } from "vitest";
import {
  hash,
  verify,
  needsRehash,
  InvalidPasswordHashError,
} from "./password.js";

// Fixtures for the password below, generated once with the module's own
// parameters. CURRENT is ln=16 (what `hash` produces today); OLD is ln=14, a
// cheaper cost that must still verify and must report needsRehash. The password
// ends in a precomposed "e-acute" (U+00E9), already NFC.
const PASSWORD = "correct horse battery staple é";
const CURRENT_HASH =
  "$scrypt$ln=16,r=8,p=1$sQ9pAFiu1k1N4w/k9TgNZw$+wvb1Ep/sz7ZohuxCrgT+lLAeC3APTNn1S3TDwdnG+A";
const OLD_HASH =
  "$scrypt$ln=14,r=8,p=1$EdlrINa/rOKC8lAg4Zz3WQ$FONXYDzV5KtXXz5ZeIONOMGBthrt9/4YrnvszjLbpk4";

const PHC = /^\$scrypt\$ln=16,r=8,p=1\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$/;

describe("hash", () => {
  it("returns a PHC-style string at the current parameters", async () => {
    expect(await hash("hunter2")).toMatch(PHC);
  });

  it("uses a fresh salt each call", async () => {
    const [a, b] = await Promise.all([hash("hunter2"), hash("hunter2")]);
    expect(a).not.toBe(b);
  });

  it("rejects an empty password", async () => {
    await expect(hash("")).rejects.toBeInstanceOf(RangeError);
  });

  it("rejects a password over 1024 characters", async () => {
    await expect(hash("x".repeat(1025))).rejects.toBeInstanceOf(RangeError);
  });
});

describe("verify", () => {
  it("accepts the right password against a fresh hash", async () => {
    const stored = await hash("hunter2");
    expect(await verify("hunter2", stored)).toBe(true);
    expect(await verify("Hunter2", stored)).toBe(false);
  });

  it("verifies against an older, cheaper hash", async () => {
    expect(await verify(PASSWORD, OLD_HASH)).toBe(true);
    expect(await verify("wrong", OLD_HASH)).toBe(false);
  });

  it("verifies against a current-parameter hash", async () => {
    expect(await verify(PASSWORD, CURRENT_HASH)).toBe(true);
  });

  it("normalises the candidate password to NFC before comparing", async () => {
    // Same text, decomposed: base "e" + U+0301 combining acute, not "é".
    const decomposed = "correct horse battery staple é";
    expect(decomposed).not.toBe(PASSWORD);
    expect(decomposed.normalize("NFC")).toBe(PASSWORD);
    expect(await verify(decomposed, CURRENT_HASH)).toBe(true);
  });

  it("returns false for an over-long candidate without running scrypt", async () => {
    expect(await verify("x".repeat(2000), CURRENT_HASH)).toBe(false);
  });

  it.each([
    ["wrong scheme", "$argon2id$v=19$m=65536$abc$def"],
    ["too few segments", "$scrypt$ln=16,r=8,p=1$saltonly"],
    ["missing a parameter", "$scrypt$ln=16,r=8$c2FsdA$ZGlnZXN0"],
    ["extra parameter", "$scrypt$ln=16,r=8,p=1,x=2$c2FsdA$ZGlnZXN0"],
    ["non-integer parameter", "$scrypt$ln=16,r=eight,p=1$c2FsdA$ZGlnZXN0"],
    ["salt not base64", "$scrypt$ln=16,r=8,p=1$not base64!$ZGlnZXN0"],
    [
      "parameters over the memory ceiling",
      "$scrypt$ln=25,r=64,p=1$c2FsdA$ZGlnZXN0",
    ],
  ])("throws InvalidPasswordHashError: %s", async (_label, stored) => {
    await expect(verify("x", stored)).rejects.toBeInstanceOf(
      InvalidPasswordHashError,
    );
  });
});

describe("needsRehash", () => {
  it("is true for a hash below the current cost", () => {
    expect(needsRehash(OLD_HASH)).toBe(true);
  });

  it("is false for a hash at the current cost", () => {
    expect(needsRehash(CURRENT_HASH)).toBe(false);
  });

  it("is false for a hash above the current cost", () => {
    expect(needsRehash(OLD_HASH.replace("ln=14", "ln=18"))).toBe(false);
  });

  it("is true when only r is below current", () => {
    expect(needsRehash(CURRENT_HASH.replace("r=8", "r=4"))).toBe(true);
  });

  it("throws InvalidPasswordHashError on a malformed string", () => {
    expect(() => needsRehash("nonsense")).toThrow(InvalidPasswordHashError);
  });
});
