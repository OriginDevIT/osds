/**
 * POST /admin/logout handler behaviour. Same mocking seam as the login suite.
 */
import { beforeEach, expect, it, vi } from "vitest";
import { SESSION_COOKIE_NAME } from "@osds/api";

vi.mock("../../../lib/request-context", () => ({
  getRequestContext: vi.fn(),
  getSessionToken: vi.fn(),
}));
vi.mock("../../../lib/db", () => ({ getDb: () => ({}) }));
vi.mock("@osds/core/persist", () => ({
  authenticateOperator: vi.fn(),
  revokeSession: vi.fn(),
}));

import { POST } from "./route.js";
import {
  getRequestContext,
  getSessionToken,
} from "../../../lib/request-context.js";
import { revokeSession } from "@osds/core/persist";

const ctxMock = vi.mocked(getRequestContext);
const tokenMock = vi.mocked(getSessionToken);
const revokeMock = vi.mocked(revokeSession);

function logoutReq(opts: { origin?: string | null; host?: string } = {}): Request {
  const headers = new Headers();
  if (opts.origin !== null) {
    headers.set("origin", opts.origin ?? "https://tenant.example");
  }
  headers.set("host", opts.host ?? "tenant.example");
  return new Request("https://tenant.example/admin/logout", {
    method: "POST",
    headers,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  ctxMock.mockResolvedValue({
    kind: "tenant",
    host: "tenant.example",
    tenantId: "tnt_x",
    operator: null,
  });
  tokenMock.mockResolvedValue("SESSIONTOKEN");
});

it("403 on a missing Origin, without revoking", async () => {
  const res = await POST(logoutReq({ origin: null }));
  expect(res.status).toBe(403);
  expect(revokeMock).not.toHaveBeenCalled();
});

it("403 on an Origin/Host mismatch", async () => {
  const res = await POST(logoutReq({ origin: "https://evil.example" }));
  expect(res.status).toBe(403);
  expect(revokeMock).not.toHaveBeenCalled();
});

it("404 on an unknown host, without revoking", async () => {
  ctxMock.mockResolvedValue({ kind: "unknown", host: "x.example" });
  const res = await POST(logoutReq({ origin: "https://x.example", host: "x.example" }));
  expect(res.status).toBe(404);
  expect(revokeMock).not.toHaveBeenCalled();
});

it("303, clears the cookie, and revokes the presented token at ctx.host", async () => {
  const res = await POST(logoutReq());
  expect(res.status).toBe(303);
  expect(res.headers.get("location")).toBe("/admin/login");
  const cookie = res.headers.get("set-cookie") ?? "";
  expect(cookie.startsWith(`${SESSION_COOKIE_NAME}=;`)).toBe(true);
  expect(cookie).toContain("Max-Age=0");
  expect(cookie).toContain("; Secure");
  expect(revokeMock).toHaveBeenCalledWith(
    expect.anything(),
    "SESSIONTOKEN",
    "tenant.example",
  );
});

it("303 and clears the cookie with no session, without calling revokeSession", async () => {
  tokenMock.mockResolvedValue(null);
  const res = await POST(logoutReq());
  expect(res.status).toBe(303);
  expect(res.headers.get("set-cookie") ?? "").toContain("Max-Age=0");
  expect(revokeMock).not.toHaveBeenCalled();
});
