/**
 * POST /admin/login/submit handler behaviour. The request-primitive adapter,
 * the db accessor, and `@osds/core/persist` are mocked; `@osds/api`'s cookie
 * serializer runs for real. No DB - `authenticateOperator`'s own DB behaviour
 * is covered in `@osds/core`'s session suite.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_COOKIE_NAME } from "@osds/api";

vi.mock("../../../../lib/request-context", () => ({
  getRequestContext: vi.fn(),
  getSessionToken: vi.fn(),
}));
vi.mock("../../../../lib/db", () => ({ getDb: () => ({}) }));
vi.mock("@osds/core/persist", () => ({
  authenticateOperator: vi.fn(),
  revokeSession: vi.fn(),
  // Faithful to the real guard in @osds/core/persist/session.ts - kept inline
  // so this unit test never loads the DB driver that module pulls in.
  isLoginThrottled: (r: unknown): boolean =>
    typeof r === "object" && r !== null && "throttled" in r,
}));

import { POST } from "./route.js";
import { getRequestContext } from "../../../../lib/request-context.js";
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
  /** `Accept` header. Set to `text/html...` to exercise the browser branch. */
  accept?: string;
}): Request {
  const headers = new Headers();
  if (opts.origin !== null) {
    headers.set("origin", opts.origin ?? "https://tenant.example");
  }
  headers.set("host", opts.host ?? "tenant.example");
  if (opts.accept !== undefined) headers.set("accept", opts.accept);
  let body: string | null = null;
  if (opts.rawBody !== undefined) {
    body = opts.rawBody;
    headers.set("content-type", opts.contentType ?? "application/json");
  } else if (opts.form) {
    body = new URLSearchParams(opts.form).toString();
    headers.set("content-type", "application/x-www-form-urlencoded");
  }
  return new Request("https://tenant.example/admin/login/submit", {
    method: "POST",
    headers,
    body,
  });
}

/** `Accept: text/html` - the header a browser sends on a form navigation. */
const ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8";

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

  it("browser form POST with a blank field: 303 to /admin/login?error=missing", async () => {
    const res = await POST(
      loginReq({ form: { email: "a@b.test" }, accept: ACCEPT_HTML }),
    );
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/admin/login?error=missing");
    expect(authMock).not.toHaveBeenCalled();
  });
});

// The failure shape is chosen by `Accept`. A browser (Accept: text/html) is
// redirected back to the form; every other caller keeps the bare status and,
// for a throttle, Retry-After - the machine contract (#86).
describe("failure responses - browser vs machine", () => {
  it("browser: null -> 303 to /admin/login?error=credentials, no cookie", async () => {
    authMock.mockResolvedValue(null);
    const res = await POST(loginReq({ form: goodForm, accept: ACCEPT_HTML }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/admin/login?error=credentials");
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("browser: throttled -> 303 to /admin/login?error=throttled, no Retry-After, no interval", async () => {
    authMock.mockResolvedValue({ throttled: true, retryAfterSeconds: 420 });
    const res = await POST(loginReq({ form: goodForm, accept: ACCEPT_HTML }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/admin/login?error=throttled");
    expect(res.headers.get("retry-after")).toBeNull();
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("machine (Accept: application/json): null -> bare 401, no redirect", async () => {
    authMock.mockResolvedValue(null);
    const res = await POST(
      loginReq({ form: goodForm, accept: "application/json" }),
    );
    expect(res.status).toBe(401);
    expect(res.headers.get("location")).toBeNull();
    expect(await res.text()).toBe("Email or password is incorrect.\n");
  });

  it("machine (Accept: */*): throttled -> 429 with Retry-After", async () => {
    authMock.mockResolvedValue({ throttled: true, retryAfterSeconds: 420 });
    const res = await POST(loginReq({ form: goodForm, accept: "*/*" }));
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("420");
    expect(res.headers.get("location")).toBeNull();
  });
});

describe("rate limit (#86) - machine caller (no Accept header)", () => {
  it("429 with Retry-After and no cookie when authenticateOperator throttles", async () => {
    authMock.mockResolvedValue({ throttled: true, retryAfterSeconds: 420 });
    const res = await POST(loginReq({ form: goodForm }));
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("420");
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(await res.text()).toBe("Too many sign-in attempts. Try again later.\n");
  });
});

describe("credentials - identical failed-login response (machine caller)", () => {
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
