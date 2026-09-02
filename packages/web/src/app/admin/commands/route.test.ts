/**
 * POST /admin/commands wiring. `dispatchCommand` is mocked (its own logic is
 * covered in `@osds/api`); this checks the guard, the unknown-host 404, body
 * parsing, and that the outcome is rendered by `commandResponse`.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../lib/request-context", () => ({
  getRequestContext: vi.fn(),
}));
vi.mock("../../../lib/db", () => ({ getDb: () => ({}) }));
vi.mock("@osds/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@osds/api")>();
  return { ...actual, dispatchCommand: vi.fn() };
});

import { POST } from "./route.js";
import { getRequestContext } from "../../../lib/request-context.js";
import { dispatchCommand } from "@osds/api";

const ctxMock = vi.mocked(getRequestContext);
const dispatchMock = vi.mocked(dispatchCommand);

const TENANT_CTX = {
  kind: "tenant",
  host: "tenant.example",
  tenantId: "tnt_x",
  operator: {
    operatorId: "op_1",
    email: "op@example.test",
    isSuperadmin: false,
    role: "editor",
  },
} as const;

function req(opts: {
  origin?: string | null;
  host?: string;
  body?: unknown;
  rawBody?: string;
}): Request {
  const headers = new Headers();
  if (opts.origin !== null) {
    headers.set("origin", opts.origin ?? "https://tenant.example");
  }
  headers.set("host", opts.host ?? "tenant.example");
  headers.set("content-type", "application/json");
  const body =
    opts.rawBody !== undefined
      ? opts.rawBody
      : opts.body !== undefined
        ? JSON.stringify(opts.body)
        : null;
  return new Request("https://tenant.example/admin/commands", {
    method: "POST",
    headers,
    body,
  });
}

const goodBody = {
  command: "listing.upsert",
  idempotency_key: "k_1",
  tenant_id: "tnt_x",
  payload: { slug: "acme", name: "Acme" },
};

beforeEach(() => {
  vi.clearAllMocks();
  ctxMock.mockResolvedValue(TENANT_CTX);
});

describe("POST /admin/commands", () => {
  it("403 on a missing Origin, without dispatching", async () => {
    const res = await POST(req({ origin: null, body: goodBody }));
    expect(res.status).toBe(403);
    expect(dispatchMock).not.toHaveBeenCalled();
  });

  it("404 on an unknown host, without dispatching", async () => {
    ctxMock.mockResolvedValue({ kind: "unknown", host: "nope.example" });
    const res = await POST(
      req({
        origin: "https://nope.example",
        host: "nope.example",
        body: goodBody,
      }),
    );
    expect(res.status).toBe(404);
    expect(dispatchMock).not.toHaveBeenCalled();
  });

  it("400 when the body is not JSON", async () => {
    const res = await POST(req({ rawBody: "not json" }));
    expect(res.status).toBe(400);
    expect(dispatchMock).not.toHaveBeenCalled();
  });

  it("passes the resolved context and parsed body to dispatchCommand and renders the outcome", async () => {
    dispatchMock.mockResolvedValue({ kind: "accepted", eventId: "evt_x" });
    const res = await POST(req({ body: goodBody }));

    expect(dispatchMock).toHaveBeenCalledWith(
      TENANT_CTX,
      goodBody,
      expect.anything(),
      expect.anything(),
    );
    expect(res.status).toBe(202);
    expect(await res.json()).toEqual({ event_id: "evt_x" });
  });

  it("renders a forbidden outcome as its problem status", async () => {
    dispatchMock.mockResolvedValue({
      kind: "forbidden",
      problem: {
        type: "https://osds.dev/problems/tenant-mismatch",
        title: "tenant mismatch",
        status: 403,
        code: "tenant_mismatch",
        detail: "x",
      },
    });
    const res = await POST(
      req({ body: { ...goodBody, tenant_id: "tnt_other" } }),
    );
    expect(res.status).toBe(403);
    expect(res.headers.get("content-type")).toBe(
      "application/problem+json; charset=utf-8",
    );
  });
});
