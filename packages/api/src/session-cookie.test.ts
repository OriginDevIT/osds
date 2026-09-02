/**
 * Pure string builders - no Postgres, no network, always runs.
 */
import { randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";
import type { Session } from "@osds/core/persist";
import {
  SESSION_COOKIE_NAME,
  serializeClearedSessionCookie,
  serializeSessionCookie,
  type SessionCookieInput,
} from "./session-cookie.js";

/**
 * Compile-time guard: every `@osds/core/persist` `Session` is a valid
 * {@link SessionCookieInput}, so a `createSession` / `authenticateOperator`
 * result can be handed to {@link serializeSessionCookie} directly. If that
 * return shape drifts (a renamed field, `expiresAt` becoming a string), this
 * type resolves to `false` and the assignment below fails `pnpm typecheck` -
 * not the web adapter at runtime.
 */
type SessionIsCookieInput = Session extends SessionCookieInput ? true : false;
const SESSION_IS_COOKIE_INPUT: SessionIsCookieInput = true;

const T0 = "2026-09-01T12:00:00.000Z";
const at = (iso: string) => ({ now: () => new Date(iso) });
const plus = (iso: string, ms: number) =>
  new Date(new Date(iso).getTime() + ms);

const FOURTEEN_DAYS_MS = 14 * 24 * 60 * 60 * 1000;

describe("serializeSessionCookie", () => {
  it("core's Session is assignable to SessionCookieInput (compile-time)", () => {
    expect(SESSION_IS_COOKIE_INPUT).toBe(true);

    // And exercised for real: a value typed as core's Session goes straight in.
    const fromCore: Session = { token: "abc", expiresAt: plus(T0, 1000) };
    expect(serializeSessionCookie(fromCore, at(T0))).toBe(
      "__Host-osds_session=abc; Max-Age=1; Path=/; HttpOnly; Secure; SameSite=Lax",
    );
  });

  it("emits the attributes in a fixed order", () => {
    const cookie = serializeSessionCookie(
      { token: "abc", expiresAt: plus(T0, FOURTEEN_DAYS_MS) },
      at(T0),
    );
    expect(cookie.split("; ")).toEqual([
      "__Host-osds_session=abc",
      "Max-Age=1209600",
      "Path=/",
      "HttpOnly",
      "Secure",
      "SameSite=Lax",
    ]);
  });

  it("carries no Domain and the exact Path", () => {
    const cookie = serializeSessionCookie(
      { token: "abc", expiresAt: plus(T0, 1000) },
      at(T0),
    );
    expect(cookie).not.toMatch(/;\s*Domain=/i);
    expect(cookie).toMatch(/; Path=\/(;|$)/);
    expect(cookie).toContain("; Secure");
    expect(cookie).toContain("; HttpOnly");
    expect(cookie).toContain("; SameSite=Lax");
  });

  it("derives Max-Age from expiresAt and the injected clock, not a constant", () => {
    const expiresAt = plus(T0, FOURTEEN_DAYS_MS);

    const atT0 = serializeSessionCookie({ token: "t", expiresAt }, at(T0));
    const at100 = serializeSessionCookie(
      { token: "t", expiresAt },
      at(plus(T0, 100_000).toISOString()),
    );

    expect(atT0).toContain("Max-Age=1209600");
    // Same expiry, clock advanced 100s -> Max-Age falls by exactly 100.
    expect(at100).toContain("Max-Age=1209500");
  });

  it("floors sub-second remainders", () => {
    const cookie = serializeSessionCookie(
      { token: "t", expiresAt: plus(T0, 4500) },
      at(T0),
    );
    expect(cookie).toContain("Max-Age=4");
  });

  it("uses only the injected clock, never the system clock", () => {
    // An injected epoch clock with an epoch-relative expiry must yield that
    // relative value regardless of what the real Date.now() is.
    const cookie = serializeSessionCookie(
      { token: "t", expiresAt: new Date(3600_000) },
      { now: () => new Date(0) },
    );
    expect(cookie).toContain("Max-Age=3600");
  });

  it("clamps a past expiry to Max-Age=0 but stays a well-formed __Host- cookie", () => {
    const cookie = serializeSessionCookie(
      { token: "t", expiresAt: plus(T0, -5000) },
      at(T0),
    );
    expect(cookie).toBe(
      "__Host-osds_session=t; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax",
    );
  });

  it("writes a base64url token verbatim, unescaped", () => {
    const token = randomBytes(32).toString("base64url");
    const cookie = serializeSessionCookie(
      { token, expiresAt: plus(T0, 1000) },
      at(T0),
    );
    expect(cookie).not.toContain("%");
    // The value the resolver will hash round-trips exactly.
    const value = cookie.slice(`${SESSION_COOKIE_NAME}=`.length).split("; ")[0];
    expect(value).toBe(token);
  });

  it.each([
    ["semicolon", "a;b"],
    ["space", "a b"],
    ["comma", "a,b"],
    ["backslash", "a\\b"],
    ["double quote", 'a"b'],
    ["control char", "a\tb"],
  ])("throws on a token containing a %s", (_label, token) => {
    expect(() =>
      serializeSessionCookie({ token, expiresAt: plus(T0, 1000) }, at(T0)),
    ).toThrow(/not allowed in a cookie value/);
  });
});

describe("serializeClearedSessionCookie", () => {
  it("is an exact, constant deletion cookie", () => {
    expect(serializeClearedSessionCookie()).toBe(
      "__Host-osds_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax",
    );
    expect(serializeClearedSessionCookie()).toBe(serializeClearedSessionCookie());
  });

  it("has an empty value, Max-Age=0, and the attributes __Host- deletion needs", () => {
    const cookie = serializeClearedSessionCookie();
    expect(cookie).toMatch(/^__Host-osds_session=;/);
    expect(cookie).toContain("; Max-Age=0");
    expect(cookie).toContain("; Secure");
    expect(cookie).toMatch(/; Path=\/(;|$)/);
    expect(cookie).not.toMatch(/;\s*Domain=/i);
  });
});

describe("SESSION_COOKIE_NAME", () => {
  it("is the single __Host- name both builders emit", () => {
    expect(SESSION_COOKIE_NAME).toBe("__Host-osds_session");
    expect(
      serializeSessionCookie(
        { token: "t", expiresAt: plus(T0, 1000) },
        at(T0),
      ).startsWith(`${SESSION_COOKIE_NAME}=`),
    ).toBe(true);
    expect(
      serializeClearedSessionCookie().startsWith(`${SESSION_COOKIE_NAME}=`),
    ).toBe(true);
  });
});
