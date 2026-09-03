/**
 * app/admin/login/page.tsx - the two guard branches only.
 *
 * `notFound()` and `redirect()` from `next/navigation` signal by throwing, and
 * both guards run before the component returns any JSX. So the page function is
 * invoked directly and the throw is asserted; the returned form, its fields and
 * the `?error=` message mapping are NOT rendered or inspected here - that needs
 * a DOM test environment the repo intentionally does not have, tracked in #105.
 *
 * Same mocking seam as the web handler suites (see logout/route.test.ts): the
 * `getRequestContext` adapter and `next/navigation` are `vi.mock`'d, hoisted
 * above the import of the page under test.
 */
import { beforeEach, expect, it, vi } from "vitest";
import type { RequestContext } from "@osds/api";

vi.mock("../../../lib/request-context", () => ({
  getRequestContext: vi.fn(),
  getSessionToken: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
  redirect: vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT:${url}`);
  }),
}));

import AdminLoginPage from "./page.js";
import { getRequestContext } from "../../../lib/request-context.js";
import { notFound, redirect } from "next/navigation";

const ctxMock = vi.mocked(getRequestContext);
const notFoundMock = vi.mocked(notFound);
const redirectMock = vi.mocked(redirect);

// Never awaited: both guards throw before the page reads it.
const searchParams: Promise<Record<string, string | string[] | undefined>> =
  Promise.resolve({});

beforeEach(() => {
  vi.clearAllMocks();
});

it("ctx.kind === 'unknown' -> notFound(), before any JSX", async () => {
  ctxMock.mockResolvedValue({ kind: "unknown", host: "nope.example" });

  await expect(AdminLoginPage({ searchParams })).rejects.toThrow(
    "NEXT_NOT_FOUND",
  );
  expect(notFoundMock).toHaveBeenCalledTimes(1);
  expect(redirectMock).not.toHaveBeenCalled();
});

it("an already-authenticated operator -> redirect('/admin'), before any JSX", async () => {
  const ctx: RequestContext = {
    kind: "console",
    host: "console.example",
    operator: { operatorId: "op_1", email: "a@b.test", isSuperadmin: false },
  };
  ctxMock.mockResolvedValue(ctx);

  await expect(AdminLoginPage({ searchParams })).rejects.toThrow(
    "NEXT_REDIRECT:/admin",
  );
  expect(redirectMock).toHaveBeenCalledWith("/admin");
  expect(notFoundMock).not.toHaveBeenCalled();
});
