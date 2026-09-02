/**
 * POST /admin/login handler behaviour. The request-primitive adapter, the db
 * accessor, and `@osds/core/persist` are mocked; `@osds/api`'s cookie
 * serializer runs for real. No DB - `authenticateOperator`'s own DB behaviour
 * is covered in `@osds/core`'s session suite.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
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
import { getRequestContext } from "../../../lib/request-context.js";
import { authenticateOperator } from "@osds/core/persist";

const ctxMock = vi.mocked(getRequestContext);
const authMock = vi.mocked(authenticateOperator);

const TENANT_CTX = {
  kind: "tenant",
  host: "tenant.example",
  tenantId: "tnt_x",
  operator: null,
} as const;

function loginReq(opts: {
  origin?: string | null;
  host?: string;
  form?: Record<string, string>;
  rawBody?: string;
  contentType?: string;
}): Request {
  const headers = new Headers();
  if (opts.origin !== null) {
    headers.set("origin", opts.origin ?? "https://tenant.example");
  }
  headers.set("host", opts.host ?? "tenant.example");
  let body: string | null = null;
  if (opts.rawBody !== undefined) {
    body = opts.rawBody;
    headers.set("content-type", opts.contentType ?? "application/json");
  } else if (opts.form) {
    body = new URLSearchParams(opts.form).toString();
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  return new Request("https://tenant.example/admin/login", {
    method: "POST",
    headers,
    body,
  });
}

const goodForm = { email: "a@b.test", password: "hunter2" };
const session = () => ({ token: "TOKENVALUE", expiresAt: new Date(Date.now() + 1_000) });

beforeEach(() => {
  vi.clearAllMocks();
  ctxMock.mockResolvedValue(TENANT_CTX);
});

describe("origin guard", () => {
  it("403 when Origin is absent, without touching credentials", async () => {
    const res = await POST(loginReq({ origin: null, form: goodForm }));
    expect(res.status).toBe(403);
    expect(authMock).not.toHaveBeenCalled();
  });

  it("403 when the Origin host does not match Host", async () => {
    const res = await POST(
      loginReq({ origin: "https://evil.example", host: "tenant.example", form: goodForm }),
    );
    expect(res.status).toBe(403);
    expect(authMock).not.toHaveBeenCalled();
  });
});

describe("host resolution", () => {
  it("404 on an unknown host, before any credential work", async () => {
    ctxMock.mockResolvedValue({ kind: "unknown", host: "nope.example" });
    const res = await POST(
      loginReq({ origin: "https://nope.example", host: "nope.example", form: goodForm }),
    );
    expect(res.status).toBe(404);
    expect(authMock).not.toHaveBeenCalled();
  });
});

describe("request body", () => {
  it("400 when the body is not a form", async () => {
    const res = await POST(loginReq({ rawBody: "{}", contentType: "application/json" }));
    expect(res.status).toBe(400);
    expect(authMock).not.toHaveBeenCalled();
  });

  it.each([{}, { email: "a@b.test" }, { password: "x" }, { email: "  ", password: "x" }])(
    "400 when a field is missing or blank: %j",
    async (form) => {
      const res = await POST(loginReq({ form: form as Record<string, string> }));
      expect(res.status).toBe(400);
      expect(authMock).not.toHaveBeenCalled();
    },
  );
});

describe("credentials - spec §4.4 identical response", () => {
  it("401 with a fixed body and no cookie for both unknown email and wrong password", async () => {
    authMock.mockResolvedValue(null);
    const a = await POST(loginReq({ form: { email: "nobody@x.test", password: "wrong" } }));
    const b = await POST(loginReq({ form: { email: "real@x.test", password: "alsowrong" } }));
    expect([a.status, b.status]).toEqual([401, 401]);
    expect(await a.text()).toBe(await b.text());
    expect(a.headers.get("set-cookie")).toBeNull();
  });

  it("the two 401s are identical in status, full header map, and body - no oracle", async () => {
    authMock.mockResolvedValue(null);
    const unknownEmail = await POST(
      loginReq({ form: { email: "nobody@x.test", password: "wrong" } }),
    );
    const wrongPassword = await POST(
      loginReq({ form: { email: "real@x.test", password: "wrong" } }),
    );

    expect(unknownEmail.status).toBe(wrongPassword.status);

    const headerMap = (r: Response): [string, string][] =>
      [...r.headers].sort(([a], [b]) => a.localeCompare(b));
    expect(headerMap(unknownEmail)).toEqual(headerMap(wrongPassword));

    expect(await unknownEmail.text()).toBe(await wrongPassword.text());
  });

  it("hands authenticateOperator the normalized ctx.host, not the raw header", async () => {
    authMock.mockResolvedValue(null);
    await POST(
      loginReq({
        origin: "https://Tenant.Example:443",
        host: "Tenant.Example:443",
        form: goodForm,
      }),
    );
    expect(authMock).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      "a@b.test",
      "hunter2",
      "tenant.example",
    );
  });
});

describe("success", () => {
  it("303 with Location /admin and a __Host- session cookie", async () => {
    authMock.mockResolvedValue(session());
    const res = await POST(loginReq({ form: goodForm }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/admin");
    const cookie = res.headers.get("set-cookie") ?? "";
    expect(cookie.startsWith(`${SESSION_COOKIE_NAME}=TOKENVALUE;`)).toBe(true);
    expect(cookie).toContain("; Secure");
    expect(cookie).toContain("; HttpOnly");
    expect(cookie).toContain("; SameSite=Lax");
  });

  it("honours a local redirect_to and rejects an off-site one", async () => {
    authMock.mockResolvedValue(session());
    const ok = await POST(
      loginReq({ form: { ...goodForm, redirect_to: "/admin/listings" } }),
    );
    expect(ok.headers.get("location")).toBe("/admin/listings");

    const bad = await POST(
      loginReq({ form: { ...goodForm, redirect_to: "https://evil.example/x" } }),
    );
    expect(bad.headers.get("location")).toBe("/admin");
  });

  it("works on the console host", async () => {
    ctxMock.mockResolvedValue({ kind: "console", host: "console.example", operator: null });
    authMock.mockResolvedValue(session());
    const res = await POST(
      loginReq({
        origin: "https://console.example",
        host: "console.example",
        form: goodForm,
      }),
    );
    expect(res.status).toBe(303);
    expect(authMock).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      "a@b.test",
      "hunter2",
      "console.example",
    );
  });
});
