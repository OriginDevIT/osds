/**
 * app/admin/(app)/layout.tsx - the session guard's three branches, by direct
 * invocation (the same seam as login/page.test.ts).
 *
 * `notFound()` and `redirect()` from `next/navigation` signal by throwing;
 * `getRequestContext` and `next/navigation` are `vi.mock`'d, hoisted above the
 * import of the layout. The pass-through branch returns `<>{children}</>` - the
 * test checks the children survive, not how the fragment renders.
 */
import { beforeEach, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
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

import AdminLayout from "./layout.js";
import { getRequestContext } from "../../../lib/request-context.js";
import { notFound, redirect } from "next/navigation";

const ctxMock = vi.mocked(getRequestContext);
const notFoundMock = vi.mocked(notFound);
const redirectMock = vi.mocked(redirect);

const children = { marker: "admin-children" } as unknown as ReactNode;

beforeEach(() => {
  vi.clearAllMocks();
});

it("ctx.kind === 'unknown' -> notFound(), no children", async () => {
  ctxMock.mockResolvedValue({ kind: "unknown", host: "nope.example" });

  await expect(AdminLayout({ children })).rejects.toThrow("NEXT_NOT_FOUND");
  expect(notFoundMock).toHaveBeenCalledTimes(1);
  expect(redirectMock).not.toHaveBeenCalled();
});

it("operator === null -> redirect('/admin/login')", async () => {
  ctxMock.mockResolvedValue({
    kind: "tenant",
    host: "tenant.example",
    tenantId: "tnt_x",
    operator: null,
  });

  await expect(AdminLayout({ children })).rejects.toThrow(
    "NEXT_REDIRECT:/admin/login",
  );
  expect(redirectMock).toHaveBeenCalledWith("/admin/login");
  expect(notFoundMock).not.toHaveBeenCalled();
});

it("a resolved operator (even with role null) -> returns the children", async () => {
  const ctx: RequestContext = {
    kind: "tenant",
    host: "tenant.example",
    tenantId: "tnt_x",
    // role null: a valid operator with no active membership here. The guard is
    // authentication only, so this still passes.
    operator: {
      operatorId: "op_1",
      email: "a@b.test",
      isSuperadmin: false,
      role: null,
    },
  };
  ctxMock.mockResolvedValue(ctx);

  const out = await AdminLayout({ children });

  expect(isValidElement(out)).toBe(true);
  const props = (out as ReactElement).props as { children: unknown };
  expect(props.children).toBe(children);
  expect(notFoundMock).not.toHaveBeenCalled();
  expect(redirectMock).not.toHaveBeenCalled();
});
