import { fileURLToPath } from "node:url";
import { defineConfig, configDefaults } from "vitest/config";

const fromRoot = (p: string): string => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  test: {
    // `tsc --build` also emits <name>.test.js into each package's dist/; only run
    // the TypeScript sources.
    exclude: [...configDefaults.exclude, "**/dist/**"],
    // DB-backed suites create a scratch database and run every migration in a hook.
    hookTimeout: 60_000,
    testTimeout: 20_000,
    // Run test files serially. Two suites migrating scratch databases at once
    // race on the shared cluster-global `osds_app` role that migration 0013
    // creates and grants ("tuple concurrently updated").
    fileParallelism: false,
  },
  // `packages/web/tsconfig.json` sets `jsx: "preserve"` for Next's own
  // compiler; without an override the test transformer inherits that and leaves
  // JSX in place, and Vite's import analysis rejects the file ("make sure to
  // not set jsx to preserve"). Setting `oxc.jsx` explicitly makes the transform
  // emit the automatic runtime regardless of tsconfig. Only one test imports a
  // `.tsx` (app/admin/login/page.test.ts, the login page's guard branches);
  // every other test imports `.ts`. `react/jsx-runtime` is a `@osds/web`
  // dependency already.
  oxc: { jsx: { runtime: "automatic", importSource: "react" } },
  resolve: {
    // Resolve workspace packages to their source, so tests run against current
    // code with no build step.
    //
    // Order is load-bearing: `@osds/core/persist` MUST precede `@osds/core`.
    // Aliases are matched in order, and the `@osds/core` entry also matches the
    // `@osds/core/persist` subpath, so listing `@osds/core` first shadows the
    // subpath alias. It does not then fall back to package resolution - the
    // whole test file fails to load with:
    //   Error: Cannot find package '@osds/core/persist' imported from
    //   packages/api/src/request-context.test.ts
    // and `vitest run packages/api/src/request-context.test.ts` reports
    // "1 failed (1) / no tests" (verified 2026-09-01 by swapping the two lines).
    alias: {
      "@osds/adapter-kit": fromRoot("./packages/adapter-kit/src/index.ts"),
      "@osds/api": fromRoot("./packages/api/src/index.ts"),
      "@osds/core/persist": fromRoot("./packages/core/src/persist/index.ts"),
      "@osds/core": fromRoot("./packages/core/src/index.ts"),
      "@osds/db": fromRoot("./packages/db/src/index.ts"),
    },
    // pnpm nests a `kysely` symlink under every package; without this the SSR
    // resolver can load two copies, and `sql` from one hands the other's Kysely
    // a no-op executor ("this query cannot be compiled to SQL").
    dedupe: ["kysely", "pg"],
  },
});
