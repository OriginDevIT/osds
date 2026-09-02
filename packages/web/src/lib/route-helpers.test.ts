/**
 * `sameOriginGuard` - the login-CSRF / cross-site-logout guard. Pure, no DB.
 */
import { describe, expect, it } from "vitest";
import { sameOriginGuard } from "./route-helpers.js";

function post(headers: Record<string, string | undefined>): Request {
  const h = new Headers();
  for (const [k, v] of Object.entries(headers)) if (v !== undefined) h.set(k, v);
  return new Request("https://tenant.example/admin/login", {
    method: "POST",
    headers: h,
  });
}

describe("sameOriginGuard", () => {
  it("allows a same-origin https POST", () => {
    expect(
      sameOriginGuard(
        post({ origin: "https://tenant.example", host: "tenant.example" }),
      ),
    ).toBeNull();
  });

  it("403 when Origin is absent", () => {
    expect(sameOriginGuard(post({ host: "tenant.example" }))?.status).toBe(403);
  });

  it("403 when the Origin host differs from Host", () => {
    expect(
      sameOriginGuard(
        post({ origin: "https://evil.example", host: "tenant.example" }),
      )?.status,
    ).toBe(403);
  });

  it("403 when Origin is not a URL", () => {
    expect(
      sameOriginGuard(post({ origin: "not a url", host: "tenant.example" }))
        ?.status,
    ).toBe(403);
  });

  // Recorded decision (see the Q3 review). `sameOriginGuard` compares the
  // normalized hostname only - scheme and port are NOT part of the check.
  //
  // scheme: an `http://` Origin whose host matches Host passes. Acceptable
  // because the session cookie is `__Host-; Secure`, so a downgraded `http`
  // context never carries it; producing an `http://tenant.example` document in
  // the victim's browser needs an active on-path attacker, who already has more
  // than login-CSRF buys.
  //
  // port: `normalizeHost` strips it, matching how `SameSite=Lax` treats a
  // same-host different-port peer as same-site.
  it("ALLOWS an http Origin when the host matches (scheme is not compared)", () => {
    expect(
      sameOriginGuard(
        post({ origin: "http://tenant.example", host: "tenant.example" }),
      ),
    ).toBeNull();
  });

  it("ALLOWS a differing port when the host matches (port is not compared)", () => {
    expect(
      sameOriginGuard(
        post({ origin: "http://tenant.example:8080", host: "tenant.example" }),
      ),
    ).toBeNull();
  });

  it("still rejects a cross-host http Origin", () => {
    expect(
      sameOriginGuard(
        post({ origin: "http://evil.example", host: "tenant.example" }),
      )?.status,
    ).toBe(403);
  });
});
