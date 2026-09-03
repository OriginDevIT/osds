/**
 * `sameOriginGuard`, `seeOther` and `commandResponse` - pure, no DB.
 */
import { describe, expect, it } from "vitest";
import type { DispatchOutcome } from "@osds/api";
import { commandResponse, sameOriginGuard, seeOther } from "./route-helpers.js";

function post(headers: Record<string, string | undefined>): Request {
  const h = new Headers();
  for (const [k, v] of Object.entries(headers))
    if (v !== undefined) h.set(k, v);
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

describe("seeOther", () => {
  it("303 with the Location, no body, no content-type", () => {
    const res = seeOther("/admin/login?error=credentials");
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/admin/login?error=credentials");
    expect(res.headers.get("content-type")).toBeNull();
    expect(res.body).toBeNull();
  });
});

describe("commandResponse", () => {
  const problem = (status: number) => ({
    type: "https://osds.dev/problems/x",
    title: "x",
    status,
    code: "x",
    detail: "x",
  });

  it("accepted -> 202 application/json with the event id", async () => {
    const res = commandResponse({ kind: "accepted", eventId: "evt_1" });
    expect(res.status).toBe(202);
    expect(res.headers.get("content-type")).toBe(
      "application/json; charset=utf-8",
    );
    expect(await res.json()).toEqual({ event_id: "evt_1" });
  });

  it("accepted with a null event id (unchanged) -> 202, event_id null", async () => {
    const res = commandResponse({ kind: "accepted", eventId: null });
    expect(res.status).toBe(202);
    expect(await res.json()).toEqual({ event_id: null });
  });

  it("duplicate -> 409 with the original event id", async () => {
    const res = commandResponse({ kind: "duplicate", eventId: "evt_orig" });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ event_id: "evt_orig" });
  });

  it("unauthorized -> 401 application/problem+json", async () => {
    const res = commandResponse({ kind: "unauthorized" });
    expect(res.status).toBe(401);
    expect(res.headers.get("content-type")).toBe(
      "application/problem+json; charset=utf-8",
    );
    expect(await res.json()).toMatchObject({
      status: 401,
      code: "unauthorized",
    });
  });

  it.each([
    ["rejected", 422],
    ["unsupported", 422],
    ["forbidden", 403],
    ["error", 500],
  ] as const)(
    "%s -> the problem's status as application/problem+json",
    async (kind, status) => {
      const res = commandResponse({
        kind,
        problem: problem(status),
      } as DispatchOutcome);
      expect(res.status).toBe(status);
      expect(res.headers.get("content-type")).toBe(
        "application/problem+json; charset=utf-8",
      );
      expect(await res.json()).toEqual(problem(status));
    },
  );

  it("falls back to 422 when a problem carries no numeric status", async () => {
    const res = commandResponse({
      kind: "rejected",
      problem: { title: "no status" },
    });
    expect(res.status).toBe(422);
  });
});
